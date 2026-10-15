"""Interchangeable measurement back-ends.

* `metrion`        – METRION ≥ 0.2 via `metrion analyze` (on-demand service, live result)
* `metrion-legacy` – METRION < 0.2 from GitHub, DuckDB via internal functions
* `metrion-live`   – METRION as a permanent service with live attribution (reads Parquet)
* `rapl`         – energy of the whole socket minus idle
* `cgroup`       – CPU time as a proxy
"""

from __future__ import annotations

from joulecode.energy.base import EnergyBackend, Reading, RunHandle
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.energy.metrion_cli import MetrionCliBackend
from joulecode.energy.metrion_db import MetrionDbBackend
from joulecode.energy.metrion_live import MetrionLiveBackend, default_results_pattern
from joulecode.energy.rapl import RaplBackend

__all__ = ["EnergyBackend", "Reading", "RunHandle", "make_backend", "auto_backend", "all_backends"]

BACKEND_NAMES = ["auto", "metrion", "metrion-legacy", "metrion-live", "rapl", "cgroup"]


def all_backends(*, metrion_results: str | None = None, rapl_idle_s: float = 10.0) -> list[EnergyBackend]:
    """All back-ends, from the most accurate to the least accurate."""
    return [
        MetrionCliBackend(),
        MetrionLiveBackend(results=metrion_results or default_results_pattern()),
        MetrionDbBackend(),
        RaplBackend(idle_seconds=rapl_idle_s),
        CgroupCpuBackend(),
    ]


def make_backend(
    name: str, *, metrion_results: str | None = None, rapl_idle_s: float = 10.0
) -> EnergyBackend:
    if name == "auto":
        return auto_backend(metrion_results=metrion_results, rapl_idle_s=rapl_idle_s)
    for backend in all_backends(metrion_results=metrion_results, rapl_idle_s=rapl_idle_s):
        if backend.name == name:
            return backend
    raise ValueError(f"Unknown back-end: {name}")


def auto_backend(*, metrion_results: str | None = None, rapl_idle_s: float = 10.0) -> EnergyBackend:
    """Pick the most accurate available back-end: metrion → metrion-live → metrion-legacy → rapl → cgroup."""
    for backend in all_backends(metrion_results=metrion_results, rapl_idle_s=rapl_idle_s):
        if not backend.check():
            return backend
    return CgroupCpuBackend()
