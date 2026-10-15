"""Fallback without energy counters: CPU time of the container cgroup.

CPU seconds are not energy. For CPU-heavy code, however, they correlate
well with dynamic consumption and work everywhere, including under
WSL2 or macOS. The report explicitly marks results of this back-end
as a proxy measurement.
"""

from __future__ import annotations

from joulecode.energy.base import EnergyBackend, Reading, RunHandle


class CgroupCpuBackend(EnergyBackend):
    name = "cgroup"
    quantity = "CPU time (proxy, not energy)"
    is_energy = False

    def finalize(self, runs: list[RunHandle]) -> dict[str, Reading]:
        return {
            r.run_id: Reading(
                value=r.cpu_s,
                unit="CPU-s",
                note=None if r.cpu_s is not None else "cgroup CPU time not readable",
            )
            for r in runs
        }
