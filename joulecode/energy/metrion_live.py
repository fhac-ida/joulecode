"""Measurement with METRION live attribution (back-end `metrion-live`).

Only for metrion versions with `live_attribution` and a Parquet sink (so far
not in the public GitHub version; for that see metrion_db.py).

metrion runs as a separate service on the measurement machine (root, eBPF, RAPL) with
live attribution switched on. It writes one row per time window and thread
with the cgroup name and the attributed energy as Parquet. joule:code
does not need root for this: it only reads these files and sums up all rows
whose cgroup belongs to the container of a run.

Because every run has its own container, the window boundary does not have to
coincide with the start of the run – the container's windows only contain
its own threads.

The primary metric is the *dynamic* energy (CPU + DRAM + GPU), i.e. the share
above idle. The idle shares depend on what else is running at the same
time and are unsuitable for an A/B comparison; they are, however, listed
in the components.
"""

from __future__ import annotations

import glob
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from joulecode.energy.base import EnergyBackend, Reading, RunHandle

DEFAULT_RESULTS = "/var/lib/metrion/energy*"

DYNAMIC = ("cpu_dynamic_j", "dram_dynamic_j", "gpu_dynamic_j")
OTHER = ("cpu_total_j", "cpu_idle_j", "dram_total_j", "dram_idle_j", "gpu_total_j", "gpu_idle_j")


def latest_results_dir(pattern: str) -> Path | None:
    dirs = [Path(p) for p in glob.glob(pattern) if Path(p).is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def _parquet_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.parquet"))


def _newest_mtime(directory: Path) -> float | None:
    files = _parquet_files(directory)
    return max(f.stat().st_mtime for f in files) if files else None


def _naive_utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


class MetrionLiveBackend(EnergyBackend):
    name = "metrion-live"
    quantity = "Dynamic energy CPU+DRAM+GPU (metrion live attribution, per process)"
    defer_cleanup = True

    def __init__(
        self,
        results: str = DEFAULT_RESULTS,
        window_s: float = 10.0,
        cutoff_s: float = 30.0,
        max_wait_s: float = 180.0,
        poll_s: float = 5.0,
        stale_after_s: float = 120.0,
    ) -> None:
        self.results = results
        self.window_s = window_s
        self.cutoff_s = cutoff_s
        self.max_wait_s = max_wait_s
        self.poll_s = poll_s
        self.stale_after_s = stale_after_s
        self.directory: Path | None = None
        self.session_start: datetime | None = None

    # -- Readiness ---------------------------------------------------------
    def check(self) -> list[str]:
        problems: list[str] = []
        try:
            import polars  # noqa: F401
        except ImportError:
            problems.append("polars is missing (pip install 'joulecode[metrion]').")
        directory = latest_results_dir(self.results)
        if directory is None:
            problems.append(
                f"No metrion result directory for '{self.results}'. Is the service running with live_attribution.enabled?"
            )
            return problems
        newest = _newest_mtime(directory)
        if newest is None:
            problems.append(f"{directory} does not contain any Parquet files yet.")
        elif time.time() - newest > self.stale_after_s:
            problems.append(
                f"Last metrion output in {directory} is {time.time() - newest:.0f} s old – service stopped?"
            )
        return problems

    def describe(self) -> dict:
        return super().describe() | {
            "results_dir": str(self.directory) if self.directory else None,
            "window_s": self.window_s,
        }

    def begin_session(self) -> None:
        self.directory = latest_results_dir(self.results)
        self.session_start = datetime.now(UTC)

    # -- Analysis ----------------------------------------------------------
    def _load(self):
        """Read only windows from the start of the measurement campaign (lazy, so that long agent runs stay cheap)."""
        import polars as pl

        assert self.directory is not None
        files = _parquet_files(self.directory)
        if not files:
            return pl.DataFrame()
        lazy = pl.concat([pl.scan_parquet(f) for f in files], how="diagonal_relaxed")
        schema = lazy.collect_schema()
        for col in ("window_start", "window_end"):
            dtype = schema.get(col)
            if isinstance(dtype, pl.Datetime) and dtype.time_zone:
                # Compare uniformly as naive UTC time.
                lazy = lazy.with_columns(pl.col(col).dt.convert_time_zone("UTC").dt.replace_time_zone(None))
        if self.session_start is not None and "window_end" in schema:
            since = _naive_utc(self.session_start) - timedelta(seconds=self.window_s)
            lazy = lazy.filter(pl.col("window_end").cast(pl.Datetime("us")) >= since)
        return lazy.collect()

    def _wait_until_covered(self, last_end: datetime) -> None:
        """Wait until metrion has written windows up to beyond the end of the last run."""
        import polars as pl

        target = _naive_utc(last_end) + timedelta(seconds=self.window_s)
        deadline = time.monotonic() + self.max_wait_s
        while True:
            df = self._load()
            if not df.is_empty() and "window_end" in df.columns:
                newest = df.select(pl.col("window_end").max()).item()
                if newest is not None and _naive_utc(newest) >= target:
                    return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"After {self.max_wait_s:.0f} s, metrion has still not written any windows up to {target} UTC."
                )
            time.sleep(self.poll_s)

    def finalize(self, runs: list[RunHandle]) -> dict[str, Reading]:
        import polars as pl

        if self.directory is None:
            self.begin_session()
        ends = [r.t_end for r in runs if r.t_end is not None]
        if ends:
            self._wait_until_covered(max(ends))
        df = self._load()

        out: dict[str, Reading] = {}
        columns = [c for c in (*DYNAMIC, *OTHER) if c in df.columns]
        for r in runs:
            if df.is_empty() or "cgroup_name" not in df.columns:
                out[r.run_id] = Reading(None, "J", note="no metrion data")
                continue
            rows = df.filter(pl.col("cgroup_name").str.contains(r.container_id, literal=True))
            if rows.is_empty():
                out[r.run_id] = Reading(None, "J", note="no rows for the container's cgroup")
                continue
            sums = {c: float(rows[c].sum()) for c in columns}
            value = sum(sums.get(c, 0.0) for c in DYNAMIC)
            out[r.run_id] = Reading(value, "J", components=sums)
        return out


def default_results_pattern() -> str:
    return os.environ.get("JOULECODE_METRION_RESULTS", DEFAULT_RESULTS)
