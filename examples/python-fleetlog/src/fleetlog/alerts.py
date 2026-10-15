"""Rule set for alerts to dispatch.

Every rule yields candidates; the `AlertEngine` sorts them, suppresses
repetitions of the same rule for the same vehicle within the cooldown period and
limits the number per vehicle and day.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from fleetlog.fuel import level_drops_while_parked, refuel_anomalies
from fleetlog.geo import FenceIndex
from fleetlog.models import Alert, FuelEvent, Severity, Trip
from fleetlog.trips import FleetIndex, harsh_braking, long_idles, night_trips, speeding_events

Rule = Callable[["AlertContext"], Iterable[Alert]]


@dataclass
class AlertContext:
    trips: Sequence[Trip]
    fuel: Sequence[FuelEvent]
    fleet: FleetIndex
    fences: FenceIndex


def rule_speeding(ctx: AlertContext) -> Iterable[Alert]:
    for ev in speeding_events(ctx.trips, ctx.fleet):
        over = ev.max_kmh - ev.limit_kmh
        severity = Severity.CRITICAL if over >= 30 else Severity.WARNING
        yield Alert(ev.vehicle_id, ev.start, "speeding", severity,
                    f"{ev.max_kmh:.0f} km/h instead of {ev.limit_kmh:.0f} km/h for {ev.seconds:.0f} s")  # fmt: skip


def rule_idle(ctx: AlertContext) -> Iterable[Alert]:
    for vid, start, length in long_idles(ctx.trips, ctx.fleet):
        minutes = length.total_seconds() / 60
        yield Alert(vid, start, "idle", Severity.INFO if minutes < 30 else Severity.WARNING,
                    f"engine idled for {minutes:.0f} min while stationary")  # fmt: skip


def rule_harsh_braking(ctx: AlertContext) -> Iterable[Alert]:
    for trip in ctx.trips:
        hits = harsh_braking(trip)
        if len(hits) >= 3:
            yield Alert(trip.vehicle_id, hits[0].ts, "harsh_braking", Severity.WARNING,
                        f"{len(hits)} hard braking events on one trip")  # fmt: skip


def rule_restricted_area(ctx: AlertContext) -> Iterable[Alert]:
    for trip in ctx.trips:
        for p in trip.points:
            fence = ctx.fences.first(p.lat, p.lon, kind="restricted")
            if fence is not None:
                yield Alert(trip.vehicle_id, p.ts, "restricted", Severity.CRITICAL, f"restricted area {fence.name}")
                break


def rule_night(ctx: AlertContext) -> Iterable[Alert]:
    for trip in night_trips(ctx.trips):
        if ctx.fleet.kind_of(trip.vehicle_id).value != "truck":
            yield Alert(trip.vehicle_id, trip.start, "night", Severity.INFO, "night trip with car/van")


def rule_fuel(ctx: AlertContext) -> Iterable[Alert]:
    for vid, ts, drop in level_drops_while_parked(ctx.fuel, ctx.trips):
        yield Alert(vid, ts, "fuel_drop", Severity.CRITICAL, f"fuel level −{drop}% while stationary")
    for text in refuel_anomalies(ctx.fuel, ctx.fleet):
        vid = text.split(" ", 1)[0]
        ts = next(e.ts for e in ctx.fuel if e.vehicle_id == vid and e.kind == "refuel")
        yield Alert(vid, ts, "refuel", Severity.WARNING, text)


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_speeding,
    rule_idle,
    rule_harsh_braking,
    rule_restricted_area,
    rule_night,
    rule_fuel,
)


@dataclass
class AlertEngine:
    rules: Sequence[Rule] = DEFAULT_RULES
    cooldown: timedelta = timedelta(minutes=20)
    max_per_vehicle_day: int = 10
    suppressed: int = field(default=0, init=False)

    def evaluate(self, ctx: AlertContext) -> list[Alert]:
        candidates: list[Alert] = []
        for rule in self.rules:
            candidates.extend(rule(ctx))
        candidates.sort(key=Alert.sort_key)
        accepted: list[Alert] = []
        last_by_key: dict[tuple[str, str], Alert] = {}
        per_day: dict[tuple[str, object], int] = defaultdict(int)
        self.suppressed = 0
        for a in candidates:
            last = last_by_key.get((a.vehicle_id, a.rule))
            if last is not None and a.ts - last.ts < self.cooldown and a.severity.rank <= last.severity.rank:
                self.suppressed += 1
                continue
            day = (a.vehicle_id, a.ts.date())
            if per_day[day] >= self.max_per_vehicle_day and a.severity is not Severity.CRITICAL:
                self.suppressed += 1
                continue
            per_day[day] += 1
            last_by_key[(a.vehicle_id, a.rule)] = a
            accepted.append(a)
        return accepted


def summarize(alerts: Iterable[Alert]) -> dict[str, dict[str, int]]:
    """Number of alerts per rule and severity."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for a in alerts:
        out[a.rule][a.severity.value] += 1
    return {rule: dict(sorted(v.items())) for rule, v in sorted(out.items())}
