"""Overall analysis: raw data → trips → consumption → alerts → report."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from fleetlog.alerts import AlertContext, AlertEngine
from fleetlog.fuel import TripFuel, consumption_by_vehicle, fuel_report
from fleetlog.geo import FenceIndex
from fleetlog.models import Alert, Geofence, ParseResult, Trip
from fleetlog.parsing import parse_lines, unknown_vehicle_errors
from fleetlog.reports import fleet_report, trips_csv
from fleetlog.trips import (
    FleetIndex,
    daily_distance,
    dedupe_points,
    depot_returns,
    drop_outliers,
    geofence_visits,
    merge_short_breaks,
    sort_points,
    split_trips,
)


@dataclass
class FleetAnalysis:
    parsed: ParseResult
    trips: list[Trip] = field(default_factory=list)
    fuel: list[TripFuel] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped_points: int = 0
    duplicate_points: int = 0

    def consumption(self) -> dict[str, float]:
        return consumption_by_vehicle(self.fuel)


def analyze(
    lines: Iterable[str],
    fences: Sequence[Geofence] = (),
    *,
    gap: timedelta = timedelta(minutes=10),
    merge_breaks: timedelta | None = None,
) -> FleetAnalysis:
    parsed = parse_lines(lines)
    result = FleetAnalysis(parsed=parsed)
    result.warnings += [f"line {n}: {msg}" for n, msg in parsed.errors]
    result.warnings += unknown_vehicle_errors(parsed)
    fleet = FleetIndex(parsed.vehicles)
    if fleet.duplicates():
        result.warnings.append("duplicate vehicle IDs: " + ", ".join(fleet.duplicates()))

    unique = dedupe_points(parsed.points)
    result.duplicate_points = len(parsed.points) - len(unique)
    clean = drop_outliers(sort_points(unique))
    result.dropped_points = len(unique) - len(clean)

    trips = split_trips(clean, fleet, gap)
    if merge_breaks is not None:
        trips = merge_short_breaks(trips, merge_breaks)
    result.trips = trips
    result.fuel = fuel_report(trips, parsed.fuel, fleet)
    index = FenceIndex(fences)
    result.alerts = AlertEngine().evaluate(AlertContext(trips, parsed.fuel, fleet, index))
    return result


def render(analysis: FleetAnalysis, fences: Sequence[Geofence] = ()) -> dict[str, object]:
    """All outputs of an analysis (report, CSV, key figures)."""
    fleet = FleetIndex(analysis.parsed.vehicles)
    index = FenceIndex(fences)
    return {
        "report": fleet_report(analysis.trips, analysis.fuel, analysis.alerts, fleet),
        "csv": trips_csv(analysis.trips),
        "daily": daily_distance(analysis.trips),
        "depot_returns": depot_returns(analysis.trips, fleet, index),
        "visits": len(geofence_visits(analysis.trips, index, kind="customer")),
        "consumption": analysis.consumption(),
    }
