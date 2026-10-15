"""Measurement with METRION < 0.2 from GitHub (https://github.com/ds-lab/Metrion), back-end `metrion-legacy`.

For metrion ≥ 0.2 see metrion_cli.py (back-end `metrion`).

The public version writes to a DuckDB file and only analyses afterwards
(no live attribution). DuckDB does not allow read access while
metrion is writing. That is why joule:code controls metrion per measurement campaign:

    begin_session:  sudo systemctl start joulecode-metrion   (fresh database)
    … runs …
    finalize:       wait, sudo systemctl stop joulecode-metrion (metrion writes
                    the last data and exits), copy the database,
                    analyse with metrion's Python + metrion_db_query.py

For `systemctl start/stop` of this one service, scripts/install.sh sets up a
password-less sudoers rule; joule:code itself runs without root.

The primary metric is the *active* (dynamic) energy CPU + DRAM + GPU of the
container cgroup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from joulecode.energy.base import EnergyBackend, Reading, RunHandle

HELPER = Path(__file__).with_name("metrion_db_query.py")
DEFAULT_SERVICE = "joulecode-metrion.service"
DEFAULT_DB = "/var/lib/metrion/monitor.db"
DEFAULT_PYTHON = "/opt/metrion/.venv/bin/python"


class MetrionDbBackend(EnergyBackend):
    name = "metrion-legacy"
    quantity = "Active energy CPU+DRAM+GPU (metrion < 0.2 from GitHub, per process)"
    # Remove containers only after stopping metrion: metrion resolves the
    # cgroup ID to a path with a delay; if the cgroup is already gone,
    # the run ends up under "unknown".
    defer_cleanup = True

    def __init__(
        self,
        *,
        service: str | None = None,
        db_path: str | None = None,
        python: str | None = None,
        init_wait_s: float = 20.0,
        exit_wait_s: float = 30.0,
        progress_every_s: float = 15.0,
        archive_dir: Path | None = None,
        systemctl: list[str] | None = None,
    ) -> None:
        self.service = service or os.environ.get("JOULECODE_METRION_SERVICE", DEFAULT_SERVICE)
        self.db_path = Path(db_path or os.environ.get("JOULECODE_METRION_DB", DEFAULT_DB))
        self.python = python or os.environ.get("JOULECODE_METRION_PYTHON", DEFAULT_PYTHON)
        self.init_wait_s = init_wait_s
        self.exit_wait_s = exit_wait_s
        self.progress_every_s = progress_every_s
        self.log = print
        self.archive_dir = archive_dir
        self.systemctl = systemctl or ["sudo", "-n", "systemctl"]
        self._running = False
        self.last_archive: Path | None = None

    # -- Service control ---------------------------------------------------------
    def _ctl(self, action: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.systemctl, action, self.service], capture_output=True, text=True, check=check
        )

    def _db_size_mb(self) -> float:
        files = [self.db_path, self.db_path.with_name(self.db_path.name + ".wal")]
        return sum(f.stat().st_size for f in files if f.is_file()) / 1e6

    def _wait(self, proc: subprocess.Popen, label: str, detail=None) -> int:
        """Wait for a process and regularly report that it is still going on."""
        start = time.monotonic()
        while True:
            try:
                return proc.wait(timeout=self.progress_every_s)
            except subprocess.TimeoutExpired:
                extra = f", {detail()}" if detail else ""
                self.log(f"  {label} … {time.monotonic() - start:.0f} s{extra}")

    def _stop_with_progress(self) -> None:
        """Stop metrion and show meanwhile that the database is still growing.

        metrion writes via a separate storage process with a queue. If
        it cannot keep up with writing, on shutdown it first works through the
        whole backlog – this can take minutes but is necessary so that no
        measurements are lost.
        """
        self.log("metrion is shutting down and writing the remaining measurements (may take a few minutes) …")
        self._running = False  # from here on, close() does not stop it again
        proc = subprocess.Popen(
            [*self.systemctl, "stop", self.service], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        code = self._wait(proc, "metrion is shutting down", lambda: f"database {self._db_size_mb():.0f} MB")
        if code != 0:
            raise RuntimeError(f"systemctl stop {self.service} failed: {proc.stderr.read().strip()}")

    def _is_active(self) -> bool:
        return self._ctl("is-active", check=False).stdout.strip() == "active"

    def check(self) -> list[str]:
        problems = []
        if not Path(self.python).is_file():
            problems.append(f"metrion Python not found: {self.python} (run scripts/install.sh).")
        else:
            res = subprocess.run(
                [self.python, "-c", "import metrion.energy_attribution.energy_attribution_cli"],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                problems.append(f"metrion cannot be imported: {res.stderr.strip()[-300:]}")
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

    def _state(self) -> str:
        """active | inactive | deactivating | activating | failed | …"""
        return self._ctl("is-active", check=False).stdout.strip() or "unknown"

    def _wait_until_stopped(self) -> None:
        """If metrion is still finishing an earlier measurement, `systemctl start` would hang behind it."""
        start = time.monotonic()
        last = 0.0
        announced = False
        while self._state() in ("deactivating", "activating", "reloading"):
            if not announced:
                self.log("metrion is still finishing the previous measurement – waiting until it is done …")
                announced = True
            if time.monotonic() - last >= self.progress_every_s:
                last = time.monotonic()
                self.log(
                    f"  metrion is still shutting down … {time.monotonic() - start:.0f} s, "
                    f"database {self._db_size_mb():.0f} MB"
                )
            time.sleep(2)

    def begin_session(self) -> None:
        self._wait_until_stopped()
        if self._state() == "active":
            raise RuntimeError(
                f"{self.service} is already running (another run or left over from an abort). "
                "Stop it with `joulecode cleanup` and start again."
            )
        self._ctl("start")
        self._running = True
        time.sleep(self.init_wait_s)
        if self._state() != "active":
            self._running = False
            raise RuntimeError(f"{self.service} is not running (check journalctl -u {self.service}).")

    def close(self) -> None:
        """Stop metrion if the measurement was aborted."""
        if self._running:
            self._ctl("stop", check=False)
            self._running = False

    def describe(self) -> dict:
        return super().describe() | {"service": self.service, "db": str(self.db_path)}

    # -- Analysis ------------------------------------------------------------------
    def _query(self, db_copy: Path, runs: list[RunHandle]) -> dict:
        payload = [
            {
                "run_id": r.run_id,
                "container_id": r.container_id,
                "t_start": r.t_start.isoformat(),
                "t_end": r.t_end.isoformat(),
            }
            for r in runs
            if r.t_start and r.t_end
        ]
        runs_file = db_copy.with_name("runs.json")
        runs_file.write_text(json.dumps(payload))
        # -P: script directory not in sys.path – otherwise joulecode/energy/ shadows
        # the real metrion modules.
        self.log(f"Analysing metrion database ({db_copy.stat().st_size / 1e6:.0f} MB) …")
        out_file, err_file = db_copy.with_name("query.out"), db_copy.with_name("query.err")
        with out_file.open("w") as out, err_file.open("w") as err:
            proc = subprocess.Popen(
                [self.python, "-P", str(HELPER), "--db", str(db_copy), "--runs", str(runs_file)],
                stdout=out,
                stderr=err,
                text=True,
            )
            returncode = self._wait(proc, "metrion analysis running")
        res = subprocess.CompletedProcess(proc.args, returncode, out_file.read_text(), err_file.read_text())
        for line in res.stdout.splitlines():
            if line.startswith("JOULECODE_JSON"):
                return json.loads(line[len("JOULECODE_JSON") :])
        hint = next((line for line in res.stderr.splitlines() if "JOULECODE_ERROR" in line), "")
        if hint:
            raise RuntimeError(hint.split("JOULECODE_ERROR:", 1)[1].strip())
        raise RuntimeError(f"metrion analysis failed (exit {res.returncode}):\n{res.stderr[-3000:]}")

    @staticmethod
    def _copy_db(src: Path, dst: Path) -> None:
        """Copy the DuckDB file including the write-ahead log.

        DuckDB first writes new tables and rows to `<file>.wal` and
        transfers them to the main file only on a clean close. If the
        .wal file is missing in the copy, the data is missing there too (up to the table
        `measurement`).
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        wal = src.with_name(src.name + ".wal")
        if wal.is_file():
            shutil.copy2(wal, dst.with_name(dst.name + ".wal"))

    def read_db(self, db_file: Path, runs: list[RunHandle]) -> dict[str, Reading]:
        """Analyse an (archived) metrion database for the given runs."""
        with tempfile.TemporaryDirectory(prefix="joulecode-metrion-") as tmp:
            # Analyse a copy: execute_queries creates helper tables,
            # and the original file belongs to root.
            db_copy = Path(tmp) / "monitor.db"
            self._copy_db(db_file, db_copy)
            sums = self._query(db_copy, runs)

        out: dict[str, Reading] = {}
        for r in runs:
            s = sums.get(r.run_id)
            rows = sum(s.get(f"{c}_rows", 0) for c in ("cpu", "dram", "gpu")) if s else 0
            if not s or rows == 0:
                out[r.run_id] = Reading(None, "J", note="no metrion data for the container's cgroup")
                continue
            value = sum(s.get(f"{c}_active_j", 0.0) for c in ("cpu", "dram", "gpu"))
            components = {k: v for k, v in s.items() if k.endswith("_j")}
            out[r.run_id] = Reading(value, "J", components=components)
        return out

    def finalize(self, runs: list[RunHandle]) -> dict[str, Reading]:
        # Wait for at least two collection cycles of metrion (collection_interval 10 s)
        # so that the last runs are recorded completely.
        time.sleep(self.exit_wait_s)
        self._stop_with_progress()
        if not self.db_path.is_file():
            raise RuntimeError(f"metrion database {self.db_path} is missing after stopping.")
        source = self.db_path
        if self.archive_dir:
            # Archive first: if the analysis fails, it can be repeated with
            # `joulecode reanalyze` without measuring again.
            source = self.archive_dir / f"metrion-{time.strftime('%Y%m%d-%H%M%S')}.db"
            self._copy_db(self.db_path, source)
            self.last_archive = source
        return self.read_db(source, runs)
