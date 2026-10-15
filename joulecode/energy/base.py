"""Common interface of the measurement back-ends.

Flow per measurement campaign:

    backend.begin_session()
    for every run:   backend.begin_run(run) … Benchmark … backend.end_run(run)
    readings = backend.finalize(runs)

`finalize` returns the values all at once because some back-ends (metrion)
only know after a delay how much energy a run cost.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RunHandle:
    run_id: str
    variant: str
    container_id: str
    warmup: bool
    t_start: datetime | None = None  # UTC
    t_end: datetime | None = None  # UTC
    wall_s: float | None = None
    cpu_s: float | None = None
    # Room for back-end-specific intermediate values (e.g. RAPL counter readings).
    scratch: dict = field(default_factory=dict)


@dataclass
class Reading:
    # Primary value of the back-end in `unit` (e.g. J). None = cannot be determined.
    value: float | None
    unit: str
    # Breakdown, e.g. {"cpu_dynamic_j": …, "dram_dynamic_j": …}.
    components: dict[str, float] = field(default_factory=dict)
    note: str | None = None


class EnergyBackend(ABC):
    #: Short name for CLI and report.
    name: str = "abstract"
    #: Name of the primary metric in the report.
    quantity: str = ""
    #: False if the back-end does not measure energy but only a proxy.
    is_energy: bool = True
    #: True: remove containers only after `finalize` (e.g. because the back-end resolves
    #: a container's cgroup with a delay).
    defer_cleanup: bool = False

    def check(self) -> list[str]:
        """Return problems that prevent a measurement (empty = ready)."""
        return []

    def describe(self) -> dict:
        return {"name": self.name, "quantity": self.quantity, "is_energy": self.is_energy}

    def begin_session(self) -> None:  # noqa: B027 - optional hook
        pass

    def begin_run(self, run: RunHandle) -> None:  # noqa: B027
        pass

    def end_run(self, run: RunHandle) -> None:  # noqa: B027
        pass

    def close(self) -> None:  # noqa: B027
        """Clean up, even if the measurement was aborted (always called)."""

    @abstractmethod
    def finalize(self, runs: list[RunHandle]) -> dict[str, Reading]:
        """Assign a measurement value to every run_id."""
