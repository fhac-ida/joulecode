"""Back-end `metrion` (≥ 0.2, command line) with a fake for `metrion` and `systemctl`."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from joulecode.energy.base import RunHandle
from joulecode.energy.metrion_cli import MetrionCliBackend, run_stamp, sum_rows

CID_A, CID_B = "a" * 64, "b" * 64

HEADER = (
    "window_start,window_end,logical_entity_id,native_pid,native_thread_id,native_parent_pid,executable_name,"
    "cmdline,cgroup_name,user_id,group_id,cpu_total_j,cpu_idle_j,cpu_dynamic_j,dram_total_j,dram_idle_j,"
    "dram_dynamic_j,gpu_total_j,gpu_idle_j,gpu_dynamic_j"
)
ROWS = [
    f"2026-10-06 12:00:00,2026-10-06 12:00:10,t1,10,10,1,python,python bench,/system.slice/docker-{CID_A}.scope,0,0,5,1,4,1,0.5,0.5,0,0,0",
    f"2026-10-06 12:00:10,2026-10-06 12:00:20,t1,10,10,1,python,python bench,/system.slice/docker-{CID_A}.scope,0,0,3,1,2,1,0.5,0.5,0,0,0",
    f"2026-10-06 12:00:20,2026-10-06 12:00:30,t2,11,11,1,python,python bench,/system.slice/docker-{CID_B}.scope,0,0,2,1,1,0,0,0,0,0,0",
]

FAKE_METRION = f"""#!{sys.executable}
import sys, pathlib
args = sys.argv[1:]
log = pathlib.Path(__file__).with_suffix(".log")
log.open("a").write(" ".join(args) + "\\n")
if args == ["--help"]:
    print("Commands: init calibrate monitor analyze")
    sys.exit(0)
if args[0] == "analyze":
    if not pathlib.Path(args[1]).exists():
        print("Path does not exist: " + args[1], file=sys.stderr); sys.exit(2)
    if "nothing" in args[1]:
        print("Nothing matched cgroup", file=sys.stderr); sys.exit(1)
    out = pathlib.Path(args[args.index("--csv") + 1]); out.mkdir(parents=True, exist_ok=True)
    (out / "rows.csv").write_text({(HEADER + chr(10) + chr(10).join(ROWS) + chr(10))!r})
    (out / "table.csv").write_text("x\\n")
    print("report")
"""


@pytest.fixture
def setup(tmp_path):
    binary = tmp_path / "metrion"
    binary.write_text(FAKE_METRION)
    binary.chmod(0o755)
    workdir = tmp_path / "var"
    workdir.mkdir()
    (workdir / "metrion.yaml").write_text("debug: false\n")
    (workdir / "calibration.json").write_text("{}")
    ctl = tmp_path / "ctl.sh"
    state = tmp_path / "state"
    ctl.write_text(
        "#!/bin/sh\n"
        f'case "$1" in start) echo active > {state};; stop) echo inactive > {state};;\n'
        f"is-active) cat {state} 2>/dev/null || echo inactive;; esac\nexit 0\n"
    )
    ctl.chmod(0o755)
    backend = MetrionCliBackend(
        binary=str(binary),
        workdir=str(workdir),
        init_wait_s=0,
        exit_wait_s=0,
        systemctl=[str(ctl)],
        archive_dir=tmp_path / "archive",
    )
    backend.log = lambda _: None
    return backend, workdir, tmp_path


def _run(run_id, cid):
    now = datetime.now(UTC)
    return RunHandle(run_id, "baseline", cid, False, t_start=now - timedelta(seconds=20), t_end=now)


def test_run_stamp():
    assert run_stamp(Path("energy_2026_10_06_12_00_05")) == datetime(2026, 10, 6, 12, 0, 5, tzinfo=UTC)
    assert run_stamp(Path("monitor_2026_10_06_12_00_05.db")) == datetime(2026, 10, 6, 12, 0, 5, tzinfo=UTC)
    assert run_stamp(Path("calibration.json")) is None


def test_sum_rows_per_container(tmp_path):
    f = tmp_path / "rows.csv"
    f.write_text(HEADER + "\n" + "\n".join(ROWS) + "\n")
    sums = sum_rows(f, [_run("r1", CID_A), _run("r2", CID_B), _run("r3", "c" * 64)])
    assert sums["r1"]["cpu_dynamic_j"] == pytest.approx(6.0)
    assert sums["r1"]["dram_dynamic_j"] == pytest.approx(1.0)
    assert sums["r1"]["rows"] == 2
    assert sums["r2"]["cpu_total_j"] == pytest.approx(2.0)
    assert sums["r3"] == {}


def test_check_ready_and_missing_calibration(setup):
    backend, workdir, _ = setup
    assert backend.check() == []
    (workdir / "calibration.json").unlink()
    assert any("calibrate" in p for p in backend.check())


def test_finalize_picks_run_after_session_start_and_archives_live_result(setup):
    backend, workdir, tmp = setup
    old = workdir / "energy_2020_01_01_00_00_00"
    old.mkdir()
    backend.begin_session()
    stamp = backend.session_start.strftime("%Y_%m_%d_%H_%M_%S")
    live = workdir / f"energy_{stamp}"
    live.mkdir()
    (live / "part-0.parquet").write_bytes(b"x")
    (workdir / f"monitor_{stamp}").mkdir()

    readings = backend.finalize([_run("r1", CID_A), _run("r2", CID_B), _run("r3", "c" * 64)])
    assert readings["r1"].value == pytest.approx(7.0)  # CPU 6 + DRAM 1 dynamic
    assert readings["r2"].value == pytest.approx(1.0)
    assert readings["r3"].value is None
    assert (tmp / "archive" / live.name / "part-0.parquet").exists()
    calls = (tmp / "metrion.log").read_text()
    assert (
        f"analyze {tmp / 'archive' / live.name} --cgroup docker- --csv" in calls
    )  # live result, not the old run
    assert backend.describe()["live_result"] == str(live)


def test_no_docker_rows_gives_clear_error(setup):
    backend, workdir, _ = setup
    target = workdir / "energy_nothing"
    target.mkdir()
    (target / "part-0.parquet").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="any Docker containers"):
        backend.read_output(target, [_run("r1", CID_A)])


def test_reanalyze_picks_metrion_run_before_first_measurement(tmp_path):
    from joulecode.cli import _pick_metrion_run
    from joulecode.protocol import MeasurementResult

    m = MeasurementResult(status="ok", started_at="")
    m.runs = [{"t_start": "2026-10-06T12:05:00+00:00"}, {"t_start": "2026-10-06T12:10:00+00:00"}]
    paths = [
        tmp_path / "energy_2026_10_06_11_00_00",
        tmp_path / "monitor_2026_10_06_12_04_30",
        tmp_path / "energy_2026_10_06_12_04_30",
        tmp_path / "energy_2026_10_06_12_20_00",  # later run
    ]
    assert _pick_metrion_run(paths, m).name == "energy_2026_10_06_12_04_30"


def test_relative_archive_dir_is_resolved(setup, monkeypatch):
    """Regression: archive_dir relative to the working directory (joulecode-runs/…), analyze runs in a temp directory."""
    backend, workdir, tmp = setup
    monkeypatch.chdir(tmp)
    backend.archive_dir = Path("joulecode-runs/opt/metrion")
    backend.begin_session()
    live = workdir / f"energy_{backend.session_start:%Y_%m_%d_%H_%M_%S}"
    live.mkdir()
    (live / "part-0.parquet").write_bytes(b"x")
    readings = backend.finalize([_run("r1", CID_A)])
    assert readings["r1"].value == pytest.approx(7.0)
