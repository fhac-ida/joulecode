"""Fuel: consumption per trip, refuelling events, anomalies.

The consumption of a trip results from the difference of the fuel level reports
(`level`, per cent of the tank) immediately before the start and after the end of the trip.
If there is a refuelling event in between, the refuelled amount is added.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from fleetlog.models import FuelEvent, Trip
from fleetlog.trips import FleetIndex

LEVEL_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class TripFuel:
    vehicle_id: str
    start: datetime
    liters: float | None
    per_100km: float | None
    note: str = ""


def _level_before(events: Sequence[FuelEvent], vehicle_id: str, ts: datetime) -> FuelEvent | None:
    """Last fuel level report at most LEVEL_WINDOW before `ts`."""
    best = None
    for e in events:
        if e.vehicle_id != vehicle_id or e.kind != "level":
            continue
        if ts - LEVEL_WINDOW <= e.ts <= ts and (best is None or e.ts > best.ts):
            best = e
    return best


def _level_after(events: Sequence[FuelEvent], vehicle_id: str, ts: datetime) -> FuelEvent | None:
    """First fuel level report at most LEVEL_WINDOW after `ts`."""
    best = None
    for e in events:
        if e.vehicle_id != vehicle_id or e.kind != "level":
            continue
        if ts <= e.ts <= ts + LEVEL_WINDOW and (best is None or e.ts < best.ts):
            best = e
    return best


def _refuels_between(events: Sequence[FuelEvent], vehicle_id: str, a: datetime, b: datetime) -> float:
    return sum(e.value for e in events if e.vehicle_id == vehicle_id and e.kind == "refuel" and a <= e.ts <= b)


def fuel_for_trip(trip: Trip, events: Sequence[FuelEvent], fleet: FleetIndex) -> TripFuel:
    vehicle = fleet.vehicle(trip.vehicle_id)
    if vehicle is None:
        return TripFuel(trip.vehicle_id, trip.start, None, None, "unknown vehicle")
    before = _level_before(events, trip.vehicle_id, trip.start)
    after = _level_after(events, trip.vehicle_id, trip.end)
    if before is None or after is None:
        return TripFuel(trip.vehicle_id, trip.start, None, None, "no fuel level")
    refuel = _refuels_between(events, trip.vehicle_id, before.ts, after.ts)
    liters = (before.value - after.value) / 100 * vehicle.tank_liters + refuel
    if liters < 0:
        return TripFuel(trip.vehicle_id, trip.start, None, None, "fuel level rose without refuelling")
    per_100 = liters / trip.distance_km * 100 if trip.distance_km >= 1 else None
    return TripFuel(
        trip.vehicle_id,
        trip.start,
        round(liters, 2),
        round(per_100, 2) if per_100 is not None else None,
    )


def fuel_report(trips: Iterable[Trip], events: Sequence[FuelEvent], fleet: FleetIndex) -> list[TripFuel]:
    """Consumption per trip in the order of the trips."""
    return [fuel_for_trip(t, events, fleet) for t in trips]


def consumption_by_vehicle(report: Iterable[TripFuel]) -> dict[str, float]:
    """Average consumption l/100 km per vehicle (only trips with a value)."""
    values: dict[str, list[float]] = defaultdict(list)
    for r in report:
        if r.per_100km is not None:
            values[r.vehicle_id].append(r.per_100km)
    return {vid: round(sum(v) / len(v), 2) for vid, v in sorted(values.items())}


def refuel_anomalies(events: Iterable[FuelEvent], fleet: FleetIndex) -> list[str]:
    """Refuelling events larger than the tank (fuel card misuse?)."""
    out = []
    for e in events:
        if e.kind != "refuel":
            continue
        v = fleet.vehicle(e.vehicle_id)
        if v is not None and e.value > v.tank_liters * 1.05:
            out.append(f"{e.vehicle_id} {e.ts.isoformat()}: {e.value:.1f} l > Tank {v.tank_liters:.0f} l")
    return out


def level_drops_while_parked(
    events: Sequence[FuelEvent], trips: Sequence[Trip], min_drop_pct: float = 8.0
) -> list[tuple[str, datetime, float]]:
    """Fuel level drops clearly between two reports without any driving (theft?)."""
    driving: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for t in trips:
        driving[t.vehicle_id].append((t.start, t.end))
    levels: dict[str, list[FuelEvent]] = defaultdict(list)
    for e in events:
        if e.kind == "level":
            levels[e.vehicle_id].append(e)
    out = []
    for vid, evs in sorted(levels.items()):
        evs.sort(key=lambda e: e.ts)
        spans = driving.get(vid, [])
        for a, b in zip(evs, evs[1:], strict=False):
            drop = a.value - b.value
            if drop < min_drop_pct:
                continue
            if not any(s < b.ts and a.ts < e for s, e in spans):
                out.append((vid, b.ts, round(drop, 1)))
    return out


def monthly_liters(events: Iterable[FuelEvent]) -> dict[tuple[str, str], float]:
    """Refuelled litres per vehicle and month (YYYY-MM)."""
    out: dict[tuple[str, str], float] = defaultdict(float)
    for e in events:
        if e.kind == "refuel":
            out[(e.vehicle_id, e.ts.strftime("%Y-%m"))] += e.value
    return {k: round(v, 2) for k, v in sorted(out.items())}
