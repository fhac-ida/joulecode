"""Reading the raw data from the telematics boxes.

Line format (semicolon-separated, the first column is the record type):

    VEH;<id>;<plate>;<depot>;<kind>;<tank_l>[;<active 0/1>]
    GPS;<id>;<time ISO>;<lat>;<lon>;<km/h>;<ignition 0/1>
    FUEL;<id>;<time ISO>;refuel|level;<value>

Empty lines and comments (`#`) are skipped. Faulty lines end up with their
line number in `ParseResult.errors`; the rest is read anyway.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from fleetlog.models import FuelEvent, GpsPoint, ParseResult, Vehicle, VehicleKind


class ParseError(ValueError):
    pass


def parse_timestamp(text: str) -> datetime:
    """ISO 8601 without a time zone or with `Z` (UTC)."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        ts = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ParseError(f"invalid timestamp {text!r}") from exc
    if ts.tzinfo is not None:
        raise ParseError(f"time zones are not supported: {text!r}")
    return ts


def parse_float(text: str, name: str, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError as exc:
        raise ParseError(f"{name}: not a number ({text!r})") from exc
    if value != value:  # NaN
        raise ParseError(f"{name}: NaN")
    if lo is not None and value < lo:
        raise ParseError(f"{name}: {value} < {lo}")
    if hi is not None and value > hi:
        raise ParseError(f"{name}: {value} > {hi}")
    return value


def parse_flag(text: str, name: str) -> bool:
    text = text.strip()
    if text in ("1", "true", "on"):
        return True
    if text in ("0", "false", "off"):
        return False
    raise ParseError(f"{name}: expected 0/1, not {text!r}")


def parse_vehicle(fields: list[str]) -> Vehicle:
    if len(fields) not in (6, 7):
        raise ParseError(f"VEH expects 6 or 7 fields, not {len(fields)}")
    _, vid, plate, depot, kind, tank, *rest = fields
    try:
        vkind = VehicleKind(kind.strip().lower())
    except ValueError as exc:
        raise ParseError(f"unknown vehicle kind {kind!r}") from exc
    active = parse_flag(rest[0], "active") if rest else True
    return Vehicle(
        vehicle_id=vid.strip(),
        plate=plate.strip().upper(),
        depot=depot.strip(),
        kind=vkind,
        tank_liters=parse_float(tank, "tank", 1, 2000),
        active=active,
    )


def parse_gps(fields: list[str]) -> GpsPoint:
    if len(fields) != 7:
        raise ParseError(f"GPS expects 7 fields, not {len(fields)}")
    _, vid, ts, lat, lon, speed, ignition = fields
    return GpsPoint(
        vehicle_id=vid.strip(),
        ts=parse_timestamp(ts),
        lat=parse_float(lat, "lat", -90, 90),
        lon=parse_float(lon, "lon", -180, 180),
        speed_kmh=parse_float(speed, "speed", 0, 300),
        ignition=parse_flag(ignition, "ignition"),
    )


def parse_fuel(fields: list[str]) -> FuelEvent:
    if len(fields) != 5:
        raise ParseError(f"FUEL expects 5 fields, not {len(fields)}")
    _, vid, ts, kind, value = fields
    kind = kind.strip().lower()
    if kind == "refuel":
        amount = parse_float(value, "liter", 0, 2000)
    elif kind == "level":
        amount = parse_float(value, "level", 0, 100)
    else:
        raise ParseError(f"unknown fuel report {kind!r}")
    return FuelEvent(vehicle_id=vid.strip(), ts=parse_timestamp(ts), kind=kind, value=amount)


PARSERS = {"VEH": parse_vehicle, "GPS": parse_gps, "FUEL": parse_fuel}


def parse_lines(lines: Iterable[str]) -> ParseResult:
    result = ParseResult()
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(";")
        parser = PARSERS.get(fields[0].strip().upper())
        if parser is None:
            result.errors.append((number, f"unknown record type {fields[0]!r}"))
            continue
        try:
            record = parser(fields)
        except ParseError as exc:
            result.errors.append((number, str(exc)))
            continue
        if isinstance(record, Vehicle):
            result.vehicles.append(record)
        elif isinstance(record, GpsPoint):
            result.points.append(record)
        else:
            result.fuel.append(record)
    return result


def unknown_vehicle_errors(result: ParseResult) -> list[str]:
    """Messages for GPS/fuel records of vehicles that are not in the master data."""
    known = {v.vehicle_id for v in result.vehicles}
    missing: dict[str, int] = {}
    for record in [*result.points, *result.fuel]:
        if record.vehicle_id not in known:
            missing[record.vehicle_id] = missing.get(record.vehicle_id, 0) + 1
    return [f"{vid}: {n} records without master data" for vid, n in sorted(missing.items())]


def format_gps(point: GpsPoint) -> str:
    """Inverse of `parse_gps` (for tests and exports)."""
    return ";".join(
        [
            "GPS",
            point.vehicle_id,
            point.ts.isoformat(),
            f"{point.lat:.6f}",
            f"{point.lon:.6f}",
            f"{point.speed_kmh:.1f}",
            "1" if point.ignition else "0",
        ]
    )
