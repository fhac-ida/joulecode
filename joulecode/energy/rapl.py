"""Direct RAPL measurement via /sys/class/powercap (Intel, without metrion).

Measures the consumption of the whole socket (package + DRAM) and subtracts the
idle power measured beforehand. Everything else running on the machine
ends up in the measurement too – hence less accurate than metrion, but without
eBPF and with less setup. Needs read access to `energy_uj`
(root only by default since 2020, see docs/installation.md).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from joulecode.energy.base import EnergyBackend, Reading, RunHandle

DEFAULT_ROOT = Path("/sys/class/powercap")


@dataclass(frozen=True)
class Domain:
    key: str  # e.g. "package-0", "dram-0"
    path: Path

    def read_uj(self) -> int:
        return int((self.path / "energy_uj").read_text().strip())

    def max_uj(self) -> int:
        try:
            return int((self.path / "max_energy_range_uj").read_text().strip())
        except (OSError, ValueError):
            return 2**32


def discover_domains(root: Path = DEFAULT_ROOT) -> list[Domain]:
    """Package and DRAM domains (core/uncore are contained in the package)."""
    domains: list[Domain] = []
    for zone in sorted(root.glob("intel-rapl:*")):
        if zone.name.count(":") != 1:
            continue
        pkg_idx = zone.name.split(":")[1]
        domains.append(Domain(f"package-{pkg_idx}", zone))
        for sub in sorted(zone.glob(f"intel-rapl:{pkg_idx}:*")):
            try:
                name = (sub / "name").read_text().strip()
            except OSError:
                continue
            if name == "dram":
                domains.append(Domain(f"dram-{pkg_idx}", sub))
    return domains


def _delta(start: int, end: int, max_uj: int) -> int:
    return end - start if end >= start else end + max_uj - start


class RaplBackend(EnergyBackend):
    name = "rapl"
    quantity = "Energy of the socket (package+DRAM) minus idle"

    def __init__(self, root: Path = DEFAULT_ROOT, idle_seconds: float = 10.0) -> None:
        self.root = root
        self.idle_seconds = idle_seconds
        self.domains = discover_domains(root)
        self.idle_power_w: dict[str, float] = {}

    def check(self) -> list[str]:
        if not self.domains:
            return [f"No RAPL domains under {self.root} (no Intel/AMD RAPL or a VM such as WSL2)."]
        try:
            for d in self.domains:
                d.read_uj()
        except PermissionError:
            return ["RAPL counters not readable (no permission for energy_uj, see docs/installation.md)."]
        return []

    def _snapshot(self) -> dict[str, int]:
        return {d.key: d.read_uj() for d in self.domains}

    def _joules(self, start: dict[str, int], end: dict[str, int]) -> dict[str, float]:
        return {d.key: _delta(start[d.key], end[d.key], d.max_uj()) / 1e6 for d in self.domains}

    def begin_session(self) -> None:
        if self.idle_seconds <= 0:
            self.idle_power_w = {d.key: 0.0 for d in self.domains}
            return
        s0, t0 = self._snapshot(), time.perf_counter()
        time.sleep(self.idle_seconds)
        s1, t1 = self._snapshot(), time.perf_counter()
        self.idle_power_w = {k: j / (t1 - t0) for k, j in self._joules(s0, s1).items()}

    def describe(self) -> dict:
        return super().describe() | {
            "idle_power_w": self.idle_power_w,
            "domains": [d.key for d in self.domains],
        }

    def begin_run(self, run: RunHandle) -> None:
        run.scratch["rapl_start"] = (self._snapshot(), time.perf_counter())

    def end_run(self, run: RunHandle) -> None:
        run.scratch["rapl_end"] = (self._snapshot(), time.perf_counter())

    def finalize(self, runs: list[RunHandle]) -> dict[str, Reading]:
        out: dict[str, Reading] = {}
        for r in runs:
            (s0, t0), (s1, t1) = r.scratch["rapl_start"], r.scratch["rapl_end"]
            gross = self._joules(s0, s1)
            net = {f"{k}_j": j - self.idle_power_w.get(k, 0.0) * (t1 - t0) for k, j in gross.items()}
            out[r.run_id] = Reading(value=sum(net.values()), unit="J", components=net)
        return out
