"""Back-end `metrion-legacy` (GitHub version, DuckDB) with fakes for systemctl and metrion."""

from __future__ import annotations

import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from joulecode.energy.base import RunHandle
from joulecode.energy.metrion_db import MetrionDbBackend

pl = pytest.importorskip("polars")

CID_A = "a" * 64
CID_B = "b" * 64

STUB = f"""
import polars as pl

def connect_to_db(path, type):
    return object() if path.exists() else None

def execute_queries(engine, start, end, filter="", force=False):
    cpu = pl.DataFrame({{
        "resource_group_name": ["/system.slice/docker-{CID_A}.scope", "/system.slice/docker-{CID_A}.scope",
                                "/system.slice/docker-{CID_B}.scope", "/user.slice", None],
        "active_energy_j": [2.0, 3.0, 1.0, 50.0, 9.0],
        "idle_energy_j": [1.0, 1.0, 1.0, 10.0, 9.0],
        "total_energy_j": [3.0, 4.0, 2.0, 60.0, 18.0],
    }})
    dram = pl.DataFrame({{
        "resource_group_name": ["/system.slice/docker-{CID_A}.scope"],
        "active_energy_j": [0.5], "idle_energy_j": [0.1], "total_energy_j": [0.6],
    }})
    return cpu, dram, pl.DataFrame()
"""

CTL = """
import sys, pathlib
log = pathlib.Path(sys.argv[0]).with_suffix(".log")
action = sys.argv[1]
with log.open("a") as f:
    f.write(action + "\\n")
if action == "is-active":
    print("active" if "start" in log.read_text().split() and log.read_text().split()[-2:-1] != ["stop"] else "inactive")
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    stub = tmp_path / "stub" / "metrion" / "energy_attribution"
    stub.mkdir(parents=True)
    (tmp_path / "stub" / "metrion" / "__init__.py").write_text("")
    (stub / "__init__.py").write_text("")
    (stub / "energy_attribution_cli.py").write_text(textwrap.dedent(STUB))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stub"))
    ctl = tmp_path / "ctl.py"
    ctl.write_text(CTL)
    db = tmp_path / "monitor.db"
    db.write_text("duckdb")
    backend = MetrionDbBackend(
        service="joulecode-metrion.service",
        db_path=str(db),
        python=sys.executable,
        init_wait_s=0,
        exit_wait_s=0,
        archive_dir=tmp_path / "archive",
        systemctl=[sys.executable, str(ctl)],
    )
    return backend, ctl.with_suffix(".log"), tmp_path


def _run(run_id, cid):
    now = datetime.now(UTC)
    return RunHandle(run_id, "baseline", cid, False, t_start=now - timedelta(seconds=30), t_end=now)


def test_session_starts_and_stops_service_and_sums_per_cgroup(env):
    backend, log, tmp = env
    assert backend.check() == []
    backend.begin_session()
    readings = backend.finalize([_run("r1", CID_A), _run("r2", CID_B), _run("r3", "c" * 64)])
    actions = log.read_text().split()
    assert actions[actions.index("start") :].count("stop") == 1
    assert readings["r1"].value == pytest.approx(5.5)  # active energy CPU 2+3, DRAM 0.5
    assert readings["r1"].components["cpu_total_j"] == pytest.approx(7.0)
    assert readings["r2"].value == pytest.approx(1.0)
    assert readings["r3"].value is None and "no metrion data" in readings["r3"].note
    assert len(list((tmp / "archive").glob("metrion-*.db"))) == 1


def test_close_stops_service_after_abort(env):
    backend, log, _ = env
    backend.begin_session()
    backend.close()
    assert log.read_text().split()[-1] == "stop"


def test_check_reports_missing_python(env):
    backend, _, _ = env
    backend.python = "/not/there/python"
    assert any("metrion Python" in p for p in backend.check())


def test_copy_includes_write_ahead_log(tmp_path):
    src = tmp_path / "monitor.db"
    src.write_text("main")
    (tmp_path / "monitor.db.wal").write_text("wal")
    dst = tmp_path / "copy" / "x.db"
    MetrionDbBackend._copy_db(src, dst)
    assert dst.read_text() == "main"
    assert (tmp_path / "copy" / "x.db.wal").read_text() == "wal"


class _FailingBackend(MetrionDbBackend):
    def __init__(self):
        super().__init__(init_wait_s=0, exit_wait_s=0, systemctl=["true"])

    def begin_session(self):
        pass

    def finalize(self, runs):
        raise RuntimeError("table measurement missing")


def test_failed_evaluation_keeps_runs_and_reanalyze_recovers(env, tmp_path, monkeypatch):
    import json
    import os
    import time

    from joulecode.cli import main
    from joulecode.config import ProjectConfig
    from joulecode.protocol import measure
    from joulecode.report import write_reports
    from joulecode.variants import Variant
    from tests.test_core import FakeRunner

    class IdRunner(FakeRunner):
        def start(self, image, limits):
            box = super().start(image, limits)
            box.id = CID_A if image == "baseline" else CID_B
            return box

    cfg = ProjectConfig.model_validate(
        {
            "test": {"command": "pytest"},
            "benchmark": {"command": "bench", "min_duration_s": 0},
            "measurement": {"repetitions": 4, "cooldown_s": 0, "seed": 1},
        }
    )
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "x").write_text("1")
    variants = [Variant("baseline", tmp_path / "b"), Variant("candidate", tmp_path / "c")]
    result = measure(
        cfg, variants, IdRunner({"baseline": 2.0, "candidate": 1.0}), _FailingBackend(), log=lambda _: None
    )
    assert result.status == "evaluation_failed"
    assert "reanalyze" in result.error
    assert len(result.runs) == 10 and all(r["value"] is None for r in result.runs)
    assert result.summary["baseline"]["wall_s"]["median"] == pytest.approx(2.0)  # wall times preserved

    out = tmp_path / "out"
    write_reports(result, out)
    archive = out / "metrion" / "metrion-20261006-080000.db"
    archive.parent.mkdir()
    archive.write_text("duckdb")
    os.utime(archive, (time.time() + 5, time.time() + 5))

    backend, _, _ = env  # provides a stub metrion via PYTHONPATH
    monkeypatch.setenv("JOULECODE_METRION_PYTHON", backend.python)
    assert main(["reanalyze", str(out)]) == 0
    data = json.loads((out / "result.json").read_text())
    assert data["status"] == "ok"
    assert data["comparison"]["candidate"]["primary"]["rel_change"] == pytest.approx(1.0 / 5.5 - 1)
    assert "Comparison" in (out / "report.md").read_text()


def test_ctrl_c_during_evaluation_keeps_checkpoint(tmp_path):
    from joulecode.config import ProjectConfig
    from joulecode.protocol import measure
    from joulecode.variants import Variant
    from tests.test_core import FakeRunner

    class Interrupted(_FailingBackend):
        def finalize(self, runs):
            raise KeyboardInterrupt

    cfg = ProjectConfig.model_validate(
        {
            "test": {"command": "pytest"},
            "benchmark": {"command": "bench", "min_duration_s": 0},
            "measurement": {"repetitions": 2, "cooldown_s": 0, "seed": 1},
        }
    )
    (tmp_path / "b").mkdir()
    saved = []
    runner = FakeRunner({"baseline": 1.0})
    result = measure(
        cfg, [Variant("baseline", tmp_path / "b")], runner, Interrupted(), log=lambda _: None,
        checkpoint=lambda r: saved.append([dict(x) for x in r.runs]),
    )  # fmt: skip
    assert result.status == "evaluation_failed" and "Ctrl+C" in result.error
    assert len(saved) == 1 and len(saved[0]) == 3  # runs saved before the analysis
    assert all("container_id" in r for r in saved[0])


def test_reanalyze_explains_wrong_directory(tmp_path, capsys):
    from joulecode.cli import main

    assert main(["reanalyze", str(tmp_path)]) == 2
    assert "not a result directory" in capsys.readouterr().err


def test_begin_session_waits_while_metrion_is_still_stopping(tmp_path):
    """If the previous measurement is still stopping, start must not hang behind it."""
    states = iter(["deactivating", "deactivating", "inactive", "inactive", "active"])
    calls = []

    class B(MetrionDbBackend):
        def _state(self):
            return next(states)

        def _ctl(self, action, check=True):
            calls.append(action)

    logs = []
    b = B(init_wait_s=0, db_path=str(tmp_path / "x.db"), progress_every_s=0)
    b.log = logs.append
    import joulecode.energy.metrion_db as mdb

    orig = mdb.time.sleep
    mdb.time.sleep = lambda s: None
    try:
        b.begin_session()
    finally:
        mdb.time.sleep = orig
    assert calls == ["start"]
    assert any("previous measurement" in x for x in logs)


def test_normalize_bare_module_imports():
    from joulecode.pycode import normalize_imports

    code = "def f(t):\n    return Counter(t.split())\n"
    assert normalize_imports(["collections"], code) == ["from collections import Counter"]
    assert normalize_imports(["from collections import Counter"], code) == ["from collections import Counter"]
    assert normalize_imports(["re"], "def f(x):\n    return re.sub('a', 'b', x)\n") == ["import re"]
