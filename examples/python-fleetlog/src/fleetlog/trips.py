"""Build trips from GPS points and analyse them.

Flow: clean the points (`dedupe_points`, `drop_outliers`) → sort by time per
vehicle → split at ignition off or gaps (`split_trips`) → key figures per trip
(distance, idle time with the engine running, top speed) → analyses over
all trips (daily distances, geofence visits, speeding,
hard braking, utilisation).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fleetlog.geo import FenceIndex, point_distance_km, turn_angle_deg
from fleetlog.models import GpsPoint, Trip, Vehicle, VehicleKind

DEFAULT_GAP = timedelta(minutes=10)
MAX_PLAUSIBLE_KMH = 250.0
IDLE_SPEED_KMH = 2.0


# -- Vehicle master data ----------------------------------------------------------------
class FleetIndex:
    """Access to the vehicles of the fleet."""

    def __init__(self, vehicles: Iterable[Vehicle]) -> None:
        self.vehicles: list[Vehicle] = list(vehicles)

    def __len__(self) -> int:
        return len(self.vehicles)

    def vehicle(self, vehicle_id: str) -> Vehicle | None:
        """Vehicle for the ID or None. With duplicate IDs the first entry applies."""
        for v in self.vehicles:
            if v.vehicle_id == vehicle_id:
                return v
        return None

    def kind_of(self, vehicle_id: str) -> VehicleKind:
        """Vehicle kind; unknown vehicles are treated like cars."""
        v = self.vehicle(vehicle_id)
        return v.kind if v is not None else VehicleKind.CAR

    def depot_of(self, vehicle_id: str) -> str | None:
        v = self.vehicle(vehicle_id)
        return v.depot if v is not None else None

    def active_ids(self) -> list[str]:
        return [v.vehicle_id for v in self.vehicles if v.active]

    def by_depot(self) -> dict[str, list[Vehicle]]:
        groups: dict[str, list[Vehicle]] = defaultdict(list)
        for v in self.vehicles:
            groups[v.depot].append(v)
        return dict(sorted(groups.items()))

    def duplicates(self) -> list[str]:
        counts: dict[str, int] = defaultdict(int)
        for v in self.vehicles:
            counts[v.vehicle_id] += 1
        return sorted(vid for vid, n in counts.items() if n > 1)


# -- Cleaning ---------------------------------------------------------------------------
def dedupe_points(points: Sequence[GpsPoint]) -> list[GpsPoint]:
    """Remove duplicate reports.

    Two points are duplicates if vehicle and timestamp are equal (the boxes send again
    after dead zones). The first report is kept in each case, and the order of the
    input is preserved.
    """
    seen = []
    out = []
    for p in points:
        key = (p.vehicle_id, p.ts)
        if key not in seen:
            seen.append(key)
            out.append(p)
    return out


def sort_points(points: Iterable[GpsPoint]) -> list[GpsPoint]:
    """By vehicle and time; stable, i.e. equal keys keep their order."""
    return sorted(points, key=lambda p: (p.vehicle_id, p.ts))


def drop_outliers(points: Sequence[GpsPoint], max_kmh: float = MAX_PLAUSIBLE_KMH) -> list[GpsPoint]:
    """Remove jumps that would result in an impossible speed.

    Expects points sorted by vehicle and time. The comparison is always with the
    last *kept* point of the same vehicle.
    """
    out: list[GpsPoint] = []
    last: dict[str, GpsPoint] = {}
    for p in points:
        prev = last.get(p.vehicle_id)
        if prev is not None:
            hours = (p.ts - prev.ts).total_seconds() / 3600
            if hours > 0 and point_distance_km(prev, p) / hours > max_kmh:
                continue
        out.append(p)
        last[p.vehicle_id] = p
    return out


def group_by_vehicle(points: Iterable[GpsPoint]) -> dict[str, list[GpsPoint]]:
    groups: dict[str, list[GpsPoint]] = defaultdict(list)
    for p in points:
        groups[p.vehicle_id].append(p)
    return dict(groups)


# -- Building trips ---------------------------------------------------------------------------
def _finish_trip(trip: Trip, fleet: FleetIndex) -> Trip:
    kind = fleet.kind_of(trip.vehicle_id)
    idle = 0.0
    dist = 0.0
    vmax = 0.0
    pts = trip.points
    for a, b in zip(pts, pts[1:], strict=False):
        dt = (b.ts - a.ts).total_seconds()
        dist += point_distance_km(a, b)
        if a.speed_kmh <= IDLE_SPEED_KMH and b.speed_kmh <= IDLE_SPEED_KMH and a.ignition:
            idle += dt
    for p in pts:
        if p.speed_kmh > vmax:
            vmax = p.speed_kmh
    trip.distance_km = round(dist, 3)
    trip.idle_seconds = idle
    trip.max_speed_kmh = vmax
    if kind is VehicleKind.TRUCK:
        # Trucks only report every 30 s – short stops at traffic lights do not count as idle time.
        trip.idle_seconds = max(0.0, idle - 30 * sum(1 for p in pts if p.speed_kmh <= IDLE_SPEED_KMH) / 4)
    return trip


def split_trips(points: Iterable[GpsPoint], fleet: FleetIndex, gap: timedelta = DEFAULT_GAP) -> list[Trip]:
    """Build trips: a trip ends at ignition off or a reporting gap > `gap`.

    Points with ignition off end the current trip and do not belong to it. Trips with
    fewer than two points are discarded. The result is sorted by start, on a
    tie by vehicle.
    """
    trips: list[Trip] = []
    for vid, pts in group_by_vehicle(sort_points(points)).items():
        current: Trip | None = None
        last_ts: datetime | None = None
        for p in pts:
            # Vehicles no longer active (sold, decommissioned) deliver old data – skip them.
            vehicle = fleet.vehicle(vid)
            if vehicle is not None and not vehicle.active:
                continue
            if not p.ignition:
                if current and len(current.points) >= 2:
                    trips.append(_finish_trip(current, fleet))
                current, last_ts = None, None
                continue
            if current is not None and last_ts is not None and p.ts - last_ts > gap:
                if len(current.points) >= 2:
                    trips.append(_finish_trip(current, fleet))
                current = None
            if current is None:
                current = Trip(vehicle_id=vid)
            current.points.append(p)
            last_ts = p.ts
        if current and len(current.points) >= 2:
            trips.append(_finish_trip(current, fleet))
    trips.sort(key=Trip.key)
    return trips


def merge_short_breaks(trips: Sequence[Trip], max_break: timedelta) -> list[Trip]:
    """Merge trips of the same vehicle if the break is shorter than `max_break`."""
    by_vehicle: dict[str, list[Trip]] = defaultdict(list)
    for t in sorted(trips, key=lambda t: (t.vehicle_id, t.start)):
        by_vehicle[t.vehicle_id].append(t)
    merged: list[Trip] = []
    for vid, ts in by_vehicle.items():
        acc = Trip(vehicle_id=vid, points=list(ts[0].points), distance_km=ts[0].distance_km,
                   idle_seconds=ts[0].idle_seconds, max_speed_kmh=ts[0].max_speed_kmh)  # fmt: skip
        for t in ts[1:]:
            if t.start - acc.end <= max_break:
                acc.distance_km = round(acc.distance_km + t.distance_km + point_distance_km(acc.points[-1], t.points[0]), 3)
                acc.idle_seconds += t.idle_seconds + (t.start - acc.end).total_seconds()
                acc.max_speed_kmh = max(acc.max_speed_kmh, t.max_speed_kmh)
                acc.points.extend(t.points)
            else:
                merged.append(acc)
                acc = Trip(vehicle_id=vid, points=list(t.points), distance_km=t.distance_km,
                           idle_seconds=t.idle_seconds, max_speed_kmh=t.max_speed_kmh)  # fmt: skip
        merged.append(acc)
    merged.sort(key=Trip.key)
    return merged


# -- Analyses ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SpeedingEvent:
    vehicle_id: str
    start: datetime
    end: datetime
    max_kmh: float
    limit_kmh: float

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def speeding_events(trips: Iterable[Trip], fleet: FleetIndex, min_seconds: float = 30) -> list[SpeedingEvent]:
    """Contiguous sections above the reference speed of the vehicle kind."""
    events = []
    for trip in trips:
        limit = fleet.kind_of(trip.vehicle_id).speed_limit_kmh
        start: GpsPoint | None = None
        peak = 0.0
        prev: GpsPoint | None = None
        for p in trip.points:
            if p.speed_kmh > limit:
                if start is None:
                    start, peak = p, p.speed_kmh
                peak = max(peak, p.speed_kmh)
            elif start is not None and prev is not None:
                if (prev.ts - start.ts).total_seconds() >= min_seconds:
                    events.append(SpeedingEvent(trip.vehicle_id, start.ts, prev.ts, peak, limit))
                start = None
            prev = p
        if start is not None and prev is not None and (prev.ts - start.ts).total_seconds() >= min_seconds:
            events.append(SpeedingEvent(trip.vehicle_id, start.ts, prev.ts, peak, limit))
    return events


def harsh_braking(trip: Trip, threshold_kmh_per_s: float = 12.0) -> list[GpsPoint]:
    """Points at which the speed fell faster than `threshold` km/h per second."""
    found = []
    for a, b in zip(trip.points, trip.points[1:], strict=False):
        dt = (b.ts - a.ts).total_seconds()
        if dt > 0 and (a.speed_kmh - b.speed_kmh) / dt >= threshold_kmh_per_s:
            found.append(b)
    return found


def sharp_turns(trip: Trip, min_angle: float = 100.0, min_speed: float = 40.0) -> int:
    """Number of sharp changes of direction at higher speed."""
    count = 0
    pts = trip.points
    for a, b, c in zip(pts, pts[1:], pts[2:], strict=False):
        if b.speed_kmh >= min_speed and point_distance_km(a, b) > 0.005 and point_distance_km(b, c) > 0.005:
            if turn_angle_deg(a, b, c) >= min_angle:
                count += 1
    return count


def long_idles(trips: Iterable[Trip], fleet: FleetIndex) -> list[tuple[str, datetime, timedelta]]:
    """Idle times with the engine running that are longer than allowed for the vehicle kind."""
    out = []
    for trip in trips:
        allowed = fleet.kind_of(trip.vehicle_id).max_idle
        start: GpsPoint | None = None
        last: GpsPoint | None = None
        for p in trip.points:
            if p.speed_kmh <= IDLE_SPEED_KMH:
                start = start or p
                last = p
            else:
                if start is not None and last is not None and last.ts - start.ts > allowed:
                    out.append((trip.vehicle_id, start.ts, last.ts - start.ts))
                start = last = None
        if start is not None and last is not None and last.ts - start.ts > allowed:
            out.append((trip.vehicle_id, start.ts, last.ts - start.ts))
    return out


def daily_distance(trips: Iterable[Trip]) -> dict[tuple[str, date], float]:
    """Kilometres per vehicle and calendar day (by the start date of the trip)."""
    out: dict[tuple[str, date], float] = defaultdict(float)
    for t in trips:
        out[(t.vehicle_id, t.start.date())] += t.distance_km
    return {k: round(v, 3) for k, v in sorted(out.items())}


def utilization(trips: Iterable[Trip], fleet: FleetIndex, day: date) -> dict[str, float]:
    """Share of the day (0–1) that every active vehicle was on the road."""
    busy: dict[str, float] = {vid: 0.0 for vid in fleet.active_ids()}
    start_of_day = datetime.combine(day, datetime.min.time())
    end_of_day = start_of_day + timedelta(days=1)
    for t in trips:
        if t.vehicle_id not in busy:
            continue
        s = max(t.start, start_of_day)
        e = min(t.end, end_of_day)
        if e > s:
            busy[t.vehicle_id] += (e - s).total_seconds()
    return {vid: round(sec / 86400, 4) for vid, sec in busy.items()}


@dataclass(frozen=True)
class FenceVisit:
    vehicle_id: str
    fence: str
    enter: datetime
    leave: datetime


def geofence_visits(trips: Iterable[Trip], fences: FenceIndex, kind: str | None = None) -> list[FenceVisit]:
    """Entries and exits per trip and geofence (only points within the trip)."""
    visits = []
    for trip in trips:
        inside: dict[str, datetime] = {}
        last_ts = trip.start
        for p in trip.points:
            now = {f.name for f in fences.containing(p.lat, p.lon) if kind is None or f.kind == kind}
            for name in list(inside):
                if name not in now:
                    visits.append(FenceVisit(trip.vehicle_id, name, inside.pop(name), last_ts))
            for name in sorted(now):
                inside.setdefault(name, p.ts)
            last_ts = p.ts
        for name, enter in sorted(inside.items()):
            visits.append(FenceVisit(trip.vehicle_id, name, enter, trip.end))
    visits.sort(key=lambda v: (v.enter, v.vehicle_id, v.fence))
    return visits


def depot_returns(trips: Iterable[Trip], fleet: FleetIndex, fences: FenceIndex) -> dict[str, int]:
    """How often does a trip end at the vehicle's own depot? (The depot geofence is named like the depot.)"""
    out: dict[str, int] = defaultdict(int)
    for t in trips:
        depot = fleet.depot_of(t.vehicle_id)
        if depot is None:
            continue
        end = t.points[-1]
        fence = fences.first(end.lat, end.lon, kind="depot")
        if fence is not None and fence.name == depot:
            out[t.vehicle_id] += 1
    return dict(sorted(out.items()))


def overlapping_trips(trips: Sequence[Trip]) -> list[tuple[Trip, Trip]]:
    """Trips of the same vehicle that overlap in time (data error)."""
    found = []
    by_vehicle: dict[str, list[Trip]] = defaultdict(list)
    for t in trips:
        by_vehicle[t.vehicle_id].append(t)
    for ts in by_vehicle.values():
        ts.sort(key=lambda t: t.start)
        for a, b in zip(ts, ts[1:], strict=False):
            if b.start < a.end:
                found.append((a, b))
    return found


def trip_summary(trip: Trip) -> dict[str, object]:
    return {
        "vehicle": trip.vehicle_id,
        "start": trip.start.isoformat(),
        "end": trip.end.isoformat(),
        "km": trip.distance_km,
        "minutes": round(trip.duration_seconds / 60, 1),
        "avg_kmh": round(trip.avg_speed_kmh, 1),
        "max_kmh": trip.max_speed_kmh,
        "idle_min": round(trip.idle_seconds / 60, 1),
        "points": len(trip.points),
    }


def busiest_vehicles(trips: Iterable[Trip], top: int = 5) -> list[tuple[str, float]]:
    """Vehicles with the most kilometres; ties alphabetically by ID."""
    km: dict[str, float] = defaultdict(float)
    for t in trips:
        km[t.vehicle_id] += t.distance_km
    return sorted(((v, round(k, 3)) for v, k in km.items()), key=lambda vk: (-vk[1], vk[0]))[:top]


def night_trips(trips: Iterable[Trip], start_hour: int = 22, end_hour: int = 5) -> list[Trip]:
    """Trips that start at night (time of day in the interval across midnight)."""
    return [t for t in trips if t.start.hour >= start_hour or t.start.hour < end_hour]


# -- Stops, driving times, planned vs. actual ---------------------------------------------------
@dataclass(frozen=True)
class Stop:
    vehicle_id: str
    arrive: datetime
    depart: datetime
    lat: float
    lon: float

    @property
    def minutes(self) -> float:
        return (self.depart - self.arrive).total_seconds() / 60


def stops(trip: Trip, min_minutes: float = 3.0, radius_km: float = 0.05) -> list[Stop]:
    """Stops within a trip: the vehicle stays within the radius for at least `min_minutes`."""
    out = []
    pts = trip.points
    i = 0
    while i < len(pts):
        j = i
        while j + 1 < len(pts) and point_distance_km(pts[i], pts[j + 1]) <= radius_km:
            j += 1
        if j > i and (pts[j].ts - pts[i].ts).total_seconds() / 60 >= min_minutes:
            out.append(Stop(trip.vehicle_id, pts[i].ts, pts[j].ts, pts[i].lat, pts[i].lon))
            i = j + 1
        else:
            i += 1
    return out


def hourly_profile(trips: Iterable[Trip]) -> list[float]:
    """Kilometres driven per hour of the day (0–23), assigned by the start time of the section."""
    hours = [0.0] * 24
    for t in trips:
        for a, b in zip(t.points, t.points[1:], strict=False):
            hours[a.ts.hour] += point_distance_km(a, b)
    return [round(h, 3) for h in hours]


@dataclass(frozen=True)
class DrivingTimeViolation:
    vehicle_id: str
    since: datetime
    driven: timedelta


def driving_time_violations(
    trips: Iterable[Trip],
    fleet: FleetIndex,
    max_block: timedelta = timedelta(hours=4, minutes=30),
    min_break: timedelta = timedelta(minutes=45),
) -> list[DrivingTimeViolation]:
    """Simplified driving time rule for trucks: at most 4.5 h of driving without a 45 min break.

    Breaks are the gaps between the trips. Other vehicle kinds are not checked.
    """
    by_vehicle: dict[str, list[Trip]] = defaultdict(list)
    for t in trips:
        if fleet.kind_of(t.vehicle_id) is VehicleKind.TRUCK:
            by_vehicle[t.vehicle_id].append(t)
    out = []
    for vid, ts in sorted(by_vehicle.items()):
        ts.sort(key=lambda t: t.start)
        block_start = ts[0].start
        driven = timedelta()
        prev_end: datetime | None = None
        reported = False
        for t in ts:
            if prev_end is not None and t.start - prev_end >= min_break:
                block_start, driven, reported = t.start, timedelta(), False
            driven += t.end - t.start
            if driven > max_block and not reported:
                out.append(DrivingTimeViolation(vid, block_start, driven))
                reported = True
            prev_end = t.end
    return out


def route_deviation_km(trip: Trip, planned: Sequence[tuple[float, float]]) -> float:
    """Largest distance of a trip point to the nearest waypoint of the planned route."""
    if not planned:
        raise ValueError("planned route is empty")
    worst = 0.0
    for p in trip.points:
        nearest = min(
            point_distance_km(p, GpsPoint(p.vehicle_id, p.ts, lat, lon, 0.0, True)) for lat, lon in planned
        )
        worst = max(worst, nearest)
    return round(worst, 3)


def first_and_last_positions(trips: Iterable[Trip]) -> dict[str, tuple[GpsPoint, GpsPoint]]:
    """First and last known position per vehicle over all trips."""
    out: dict[str, tuple[GpsPoint, GpsPoint]] = {}
    for t in trips:
        first, last = t.points[0], t.points[-1]
        if t.vehicle_id in out:
            f, la = out[t.vehicle_id]
            first = first if first.ts < f.ts else f
            last = last if last.ts > la.ts else la
        out[t.vehicle_id] = (first, last)
    return dict(sorted(out.items()))
