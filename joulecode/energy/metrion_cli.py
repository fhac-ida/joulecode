"""Measurement with METRION from version 0.2 via its command line (back-end `metrion`).

Flow per measurement campaign (as with the older back-end `metrion-legacy`):

    begin_session:  sudo systemctl start joulecode-metrion  (metrion monitor in /var/lib/metrion)
    … runs …
    finalize:       wait until the live analysis has written the last runs,
                    sudo systemctl stop joulecode-metrion,
                    metrion analyze <result> --cgroup docker- --csv <tmp>,
                    sum up rows.csv per container ID

metrion writes every run with a UTC timestamp: `monitor_<ts>` (raw data) and, with
`live_attribution.enabled`, additionally `energy_<ts>` (Parquet, per time window and thread,
orders of magnitude smaller). The live result is preferred for the analysis; the archive
in the result directory contains a copy of it, so that `joulecode reanalyze` always works.

Only the official command line is used (`analyze --cgroup --csv`), no
internal functions – this stays stable across metrion versions.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from joulecode.energy.base import Reading, RunHandle
from joulecode.energy.metrion_db import MetrionDbBackend

DEFAULT_BIN = "/opt/metrion/.venv/bin/metrion"
DEFAULT_DIR = "/var/lib/metrion"
STAMP = re.compile(r"_(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})(\.db)?$")
COMPONENTS = ("cpu", "dram", "gpu")
# metrion stamps the second of the start; some leeway against rounding and clock skew.
_SLACK = timedelta(seconds=5)


def run_stamp(path: Path) -> datetime | None:
    """UTC start time of a metrion run from its name (`energy_2026_10_06_12_00_00`)."""
    m = STAMP.search(path.name)
    return datetime.strptime(m.group(1), "%Y_%m_%d_%H_%M_%S").replace(tzinfo=UTC) if m else None


def _size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / 1e6


def sum_rows(rows_csv: Path, runs: list[RunHandle]) -> dict[str, dict[str, float]]:
    """Sum up the energy columns of `rows.csv` per run via the container ID in the cgroup path."""
    sums: dict[str, dict[str, float]] = {r.run_id: {} for r in runs}
    with rows_csv.open(newline="") as f:
        for row in csv.DictReader(f):
            cgroup = row.get("cgroup_name") or ""
            for r in runs:
                if r.container_id and r.container_id in cgroup:
                    s = sums[r.run_id]
                    s["rows"] = s.get("rows", 0) + 1
                    for key, value in row.items():
                        if key.endswith("_j") and value not in ("", None):
                            s[key] = s.get(key, 0.0) + float(value)
                    break
    return sums


class MetrionCliBackend(MetrionDbBackend):
    name = "metrion"
    quantity = "Dynamic energy CPU+DRAM+GPU (metrion ≥ 0.2, per process)"

    def __init__(
        self,
        *,
        binary: str | None = None,
        workdir: str | None = None,
        exit_wait_s: float = 50.0,
        **kwargs,
    ) -> None:
        # Live analysis: cutoff 30 s + window 10 s, after that the last runs are written.
        super().__init__(exit_wait_s=exit_wait_s, **kwargs)
        self.binary = binary or os.environ.get("JOULECODE_METRION_BIN", DEFAULT_BIN)
        self.workdir = Path(workdir or os.environ.get("JOULECODE_METRION_DIR", DEFAULT_DIR))
        self.session_start: datetime | None = None
        self.last_outputs: dict[str, str] = {}

    # -- Readiness ------------------------------------------------------------------
    def supports_cli(self) -> bool:
        try:
            res = subprocess.run([self.binary, "--help"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode == 0 and "analyze" in res.stdout and "calibrate" in res.stdout

    def check(self) -> list[str]:
        if not Path(self.binary).is_file():
            return [f"metrion not found: {self.binary} (scripts/install.sh --metrion-source …)."]
        if not self.supports_cli():
            return [f"{self.binary} is not a metrion version ≥ 0.2 (no `analyze`/`calibrate`)."]
        problems = []
        if not (self.workdir / "metrion.yaml").is_file():
            problems.append(f"{self.workdir}/metrion.yaml is missing (scripts/install.sh creates it).")
        if not (self.workdir / "calibration.json").is_file():
            problems.append(
                f"{self.workdir}/calibration.json is missing – calibrate once: "
                f"cd {self.workdir} && sudo {self.binary} calibrate"
            )
        try:
            res = self._ctl("is-active", check=False)
            if "password" in res.stderr.lower() or "sudo:" in res.stderr:
                problems.append(f"No sudo permission for systemctl {self.service} (sudoers rule missing).")
            elif res.stdout.strip() == "active":
                problems.append(
                    f"{self.service} is already running – left over from an abort? `joulecode cleanup` stops it."
                )
        except FileNotFoundError as exc:
            problems.append(f"systemctl/sudo not available: {exc}")
        return problems

    def describe(self) -> dict:
        return super().describe() | {"binary": self.binary, "workdir": str(self.workdir), **self.last_outputs}

    # -- Find the outputs of a run ---------------------------------------------------------
    def outputs_since(self, since: datetime) -> tuple[Path | None, Path | None]:
        """(live result, raw data) of the first metrion run that started after `since`."""

        def newest(prefix: str) -> Path | None:
            found = []
            for p in self.workdir.glob(f"{prefix}_*"):
                stamp = run_stamp(p)
                if stamp and stamp >= since.replace(microsecond=0) - _SLACK:
                    found.append((stamp, p))
            return min(found)[1] if found else None

        return newest("energy"), newest("monitor")

    def _db_size_mb(self) -> float:  # progress display while shutting down
        if self.session_start is None:
            return 0.0
        return sum(_size_mb(p) for p in self.outputs_since(self.session_start) if p is not None)

    def begin_session(self) -> None:
        self.session_start = datetime.now(UTC)
        super().begin_session()

    # -- Analysis ----------------------------------------------------------------------------
    def read_output(self, path: Path, runs: list[RunHandle]) -> dict[str, Reading]:
        """Analyse a metrion result (live directory, Parquet or DuckDB run)."""
        # analyze runs in a temp directory – a relative path (e.g. joulecode-runs/…) would be wrong there.
        path = Path(path).resolve()
        with tempfile.TemporaryDirectory(prefix="joulecode-metrion-") as tmp:
            tmp_path = Path(tmp)
            target = path
            is_live = path.is_dir() and any(path.glob("part-*.parquet"))
            if path.is_dir() and not is_live:
                # A raw Parquet run: analyze creates a database in it the first time –
                # the original belongs to root, so work on a copy.
                target = tmp_path / path.name
                shutil.copytree(path, target)
            elif path.is_file():
                target = tmp_path / path.name
                self._copy_db(path, target)
            csv_dir = tmp_path / "csv"
            self.log(f"Analysing metrion result: {path.name} ({_size_mb(path):.0f} MB) …")
            out_file, err_file = tmp_path / "analyze.out", tmp_path / "analyze.err"
            with out_file.open("w") as out, err_file.open("w") as err:
                proc = subprocess.Popen(
                    [self.binary, "analyze", str(target), "--cgroup", "docker-", "--csv", str(csv_dir)],
                    stdout=out,
                    stderr=err,
                    cwd=tmp_path,
                    text=True,
                )
                code = self._wait(proc, "metrion analysis running")
            stderr = err_file.read_text()
            if code != 0 or not (csv_dir / "rows.csv").is_file():
                if "Nothing matched" in stderr or "Nothing matched" in out_file.read_text():
                    raise RuntimeError(
                        "metrion did not see any Docker containers in this run (cgroup 'docker-'). "
                        f"Check with: journalctl -u {self.service} -n 100"
                    )
                raise RuntimeError(f"metrion analyze failed (exit {code}):\n{stderr[-3000:]}")
            sums = sum_rows(csv_dir / "rows.csv", runs)

        out: dict[str, Reading] = {}
        for r in runs:
            s = sums.get(r.run_id) or {}
            if not s.get("rows"):
                out[r.run_id] = Reading(None, "J", note="no metrion data for the container's cgroup")
                continue
            value = sum(s.get(f"{c}_dynamic_j", 0.0) for c in COMPONENTS)
            out[r.run_id] = Reading(value, "J", components={k: v for k, v in s.items() if k.endswith("_j")})
        return out

    def finalize(self, runs: list[RunHandle]) -> dict[str, Reading]:
        self.log(f"Waiting {self.exit_wait_s:.0f} s until metrion has analysed the last runs …")
        time.sleep(self.exit_wait_s)
        self._stop_with_progress()
        live, raw = self.outputs_since(self.session_start or datetime.now(UTC))
        self.last_outputs = {"live_result": str(live) if live else None, "raw_run": str(raw) if raw else None}
        source = live or raw
        if source is None:
            raise RuntimeError(
                f"No metrion result in {self.workdir} since {self.session_start:%H:%M:%S} UTC "
                f"(journalctl -u {self.service} -n 100)."
            )
        if self.archive_dir and live is not None:
            # The live result is small and sufficient for a later re-analysis.
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            archived = self.archive_dir / live.name
            if not archived.exists():
                shutil.copytree(live, archived)
            self.last_archive = archived
            source = archived
        return self.read_output(source, runs)
