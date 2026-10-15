"""Reports for dispatch and controlling: text, CSV, key figures.

Percentiles follow the nearest-rank definition (as laid down in the fleet
policy): the smallest element at or below which at least p% of the values lie.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from fleetlog.fuel import TripFuel
from fleetlog.models import Alert, Trip
from fleetlog.trips import FleetIndex, busiest_vehicles, trip_summary

CSV_COLUMNS = ("vehicle", "start", "end", "km", "minutes", "avg_kmh", "max_kmh", "idle_min", "points")


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in 0–100)."""
    if not values:
        raise ValueError("percentile() without values")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class VehicleStats:
    vehicle_id: str
    trips: int
    km: float
    hours: float
    idle_share: float
    speed_p50: float
    speed_p95: float


def vehicle_stats(trips: Iterable[Trip]) -> list[VehicleStats]:
    """Key figures per vehicle, sorted by ID."""
    by_vehicle: dict[str, list[Trip]] = defaultdict(list)
    for t in trips:
        by_vehicle[t.vehicle_id].append(t)
    out = []
    for vid in sorted(by_vehicle):
        ts = by_vehicle[vid]
        speeds = [p.speed_kmh for t in ts for p in t.points if p.speed_kmh > 0]
        seconds = sum(t.duration_seconds for t in ts)
        idle = sum(t.idle_seconds for t in ts)
        out.append(
            VehicleStats(
                vehicle_id=vid,
                trips=len(ts),
                km=round(sum(t.distance_km for t in ts), 3),
                hours=round(seconds / 3600, 2),
                idle_share=round(idle / seconds, 4) if seconds else 0.0,
                speed_p50=percentile(speeds, 50) if speeds else 0.0,
                speed_p95=percentile(speeds, 95) if speeds else 0.0,
            )
        )
    return out


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if any(c in text for c in ';"\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def trips_csv(trips: Iterable[Trip]) -> str:
    """Trips as CSV (semicolon, header line, line ending \\n)."""
    out = ";".join(CSV_COLUMNS) + "\n"
    for t in trips:
        row = trip_summary(t)
        out += ";".join(_csv_cell(row[c]) for c in CSV_COLUMNS) + "\n"
    return out


def depot_overview(trips: Iterable[Trip], fleet: FleetIndex) -> dict[str, dict[str, float]]:
    """Kilometres and trips per depot."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"km": 0.0, "trips": 0})
    for t in trips:
        depot = fleet.depot_of(t.vehicle_id) or "?"
        out[depot]["km"] += t.distance_km
        out[depot]["trips"] += 1
    return {d: {"km": round(v["km"], 3), "trips": v["trips"]} for d, v in sorted(out.items())}


def fleet_report(
    trips: Sequence[Trip],
    fuel: Sequence[TripFuel],
    alerts: Sequence[Alert],
    fleet: FleetIndex,
    title: str = "Fleet report",
) -> str:
    """Text report for dispatch."""
    lines = [title, "=" * len(title), ""]
    total_km = sum(t.distance_km for t in trips)
    lines.append(f"Trips: {len(trips)}  Kilometres: {total_km:.1f}  Vehicles: {len({t.vehicle_id for t in trips})}")
    measured = [f for f in fuel if f.per_100km is not None]
    if measured:
        avg = sum(f.per_100km for f in measured) / len(measured)
        lines.append(f"Consumption avg {avg:.2f} l/100 km ({len(measured)} of {len(fuel)} trips measurable)")
    lines += ["", "Most driven:"]
    for vid, km in busiest_vehicles(trips, 5):
        lines.append(f"  {vid:<8} {km:>10.1f} km")
    lines += ["", "Depots:"]
    for depot, v in depot_overview(trips, fleet).items():
        lines.append(f"  {depot:<12} {v['trips']:>5} trips {v['km']:>10.1f} km")
    lines += ["", f"Alerts: {len(alerts)}"]
    worst = sorted(alerts, key=lambda a: (-a.severity.rank, a.ts, a.vehicle_id))[:10]
    for a in worst:
        lines.append(f"  [{a.severity.value:<8}] {a.ts:%m-%d %H:%M} {a.vehicle_id:<8} {a.rule}: {a.message}")
    stats = vehicle_stats(trips)
    if stats:
        lines += ["", "Speed (median / 95%):"]
        for s in stats[:15]:
            lines.append(f"  {s.vehicle_id:<8} {s.speed_p50:>6.1f} / {s.speed_p95:>6.1f} km/h  idle share {s.idle_share:.0%}")
    return "\n".join(lines) + "\n"
