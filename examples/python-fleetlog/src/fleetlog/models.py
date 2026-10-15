"""Data model of the fleet telematics.

All timestamps are naive `datetime` values in UTC. Distances in kilometres,
speeds in km/h, fuel in litres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class VehicleKind(str, Enum):
    CAR = "car"
    VAN = "van"
    TRUCK = "truck"

    @property
    def speed_limit_kmh(self) -> float:
        """Internal speed limit of the fleet policy (not the traffic regulations)."""
        return {VehicleKind.CAR: 130.0, VehicleKind.VAN: 120.0, VehicleKind.TRUCK: 90.0}[self]

    @property
    def max_idle(self) -> timedelta:
        """How long a vehicle may stand with the engine running before it is flagged."""
        minutes = {VehicleKind.CAR: 5, VehicleKind.VAN: 8, VehicleKind.TRUCK: 15}[self]
        return timedelta(minutes=minutes)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}[self]


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    plate: str
    depot: str
    kind: VehicleKind
    tank_liters: float
    active: bool = True


@dataclass(frozen=True)
class GpsPoint:
    vehicle_id: str
    ts: datetime
    lat: float
    lon: float
    speed_kmh: float
    ignition: bool


@dataclass(frozen=True)
class FuelEvent:
    """Refuelling event (`refuel`, litres refuelled) or fuel level report (`level`, per cent)."""

    vehicle_id: str
    ts: datetime
    kind: str
    value: float


@dataclass
class Trip:
    vehicle_id: str
    points: list[GpsPoint] = field(default_factory=list)
    distance_km: float = 0.0
    idle_seconds: float = 0.0
    max_speed_kmh: float = 0.0

    @property
    def start(self) -> datetime:
        return self.points[0].ts

    @property
    def end(self) -> datetime:
        return self.points[-1].ts

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def avg_speed_kmh(self) -> float:
        hours = self.duration_seconds / 3600
        return self.distance_km / hours if hours > 0 else 0.0

    def key(self) -> tuple[datetime, str]:
        """Sort key: start, on a tie the vehicle."""
        return (self.start, self.vehicle_id)


@dataclass(frozen=True)
class Alert:
    vehicle_id: str
    ts: datetime
    rule: str
    severity: Severity
    message: str

    def sort_key(self) -> tuple:
        return (self.ts, -self.severity.rank, self.vehicle_id, self.rule)


@dataclass(frozen=True)
class Geofence:
    """Polygon in (lat, lon); `kind` is `depot`, `customer` or `restricted`."""

    name: str
    kind: str
    polygon: tuple[tuple[float, float], ...]

    def bbox(self) -> tuple[float, float, float, float]:
        lats = [p[0] for p in self.polygon]
        lons = [p[1] for p in self.polygon]
        return (min(lats), min(lons), max(lats), max(lons))


@dataclass
class ParseResult:
    vehicles: list[Vehicle] = field(default_factory=list)
    points: list[GpsPoint] = field(default_factory=list)
    fuel: list[FuelEvent] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors
