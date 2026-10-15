from datetime import datetime, timedelta

import pytest

from fleetlog import Geofence, GpsPoint, Vehicle, VehicleKind, analyze, render
from fleetlog.alerts import AlertContext, AlertEngine
from fleetlog.fuel import fuel_for_trip
from fleetlog.geo import FenceIndex, haversine_km, point_in_polygon, simplify
from fleetlog.models import FuelEvent
from fleetlog.parsing import parse_lines
from fleetlog.reports import percentile, trips_csv
from fleetlog.trips import FleetIndex, dedupe_points, drop_outliers, sort_points, split_trips

T0 = datetime(2026, 5, 4, 8, 0)


def gps(vid, minute, lat=50.0, lon=6.0, speed=50.0, ign=True):
    return GpsPoint(vid, T0 + timedelta(minutes=minute), lat, lon, speed, ign)


FLEET = FleetIndex([Vehicle("V1", "AC-1", "Aachen", VehicleKind.VAN, 80.0), Vehicle("V2", "AC-2", "Köln", VehicleKind.TRUCK, 400.0)])


def test_parse_lines_collects_errors_and_records():
    res = parse_lines(
        [
            "VEH;V1;ac-1;Aachen;van;80",
            "GPS;V1;2026-05-04T08:00:00;50.1;6.1;42,5;1",
            "FUEL;V1;2026-05-04T08:00:00Z;level;55",
            "GPS;V1;broken;50;6;1;1",
            "# comment",
            "XYZ;1",
        ]
    )
    assert [v.plate for v in res.vehicles] == ["AC-1"]
    assert res.points[0].speed_kmh == 42.5
    assert res.fuel[0].value == 55
    assert [n for n, _ in res.errors] == [4, 6]


def test_haversine_aachen_cologne():
    assert haversine_km(50.776, 6.084, 50.937, 6.960) == pytest.approx(64.3, abs=0.5)


def test_point_in_polygon():
    square = ((0, 0), (0, 1), (1, 1), (1, 0))
    assert point_in_polygon(0.5, 0.5, square)
    assert not point_in_polygon(1.5, 0.5, square)


def test_simplify_keeps_endpoints():
    pts = [gps("V1", i, lat=50 + i * 0.001, lon=6.0) for i in range(10)]
    out = simplify(pts, 0.01)
    assert out[0] == pts[0] and out[-1] == pts[-1] and len(out) == 2


def test_dedupe_removes_resent_points():
    a, b = gps("V1", 0), gps("V1", 1)
    assert dedupe_points([a, b, a]) == [a, b]


def test_drop_outliers():
    pts = sort_points([gps("V1", 0), gps("V1", 1, lat=51.0), gps("V1", 2, lat=50.001)])
    assert [p.lat for p in drop_outliers(pts)] == [50.0, 50.001]


def test_split_trips_on_ignition_and_gap():
    pts = [gps("V1", i, lat=50 + i * 0.01) for i in range(5)]
    pts += [gps("V1", 5, ign=False)]
    pts += [gps("V1", 30 + i, lat=50.1 + i * 0.01) for i in range(3)]
    trips = split_trips(pts, FLEET)
    assert [len(t.points) for t in trips] == [5, 3]
    assert trips[0].distance_km == pytest.approx(4.448, abs=0.01)


def test_fuel_for_trip():
    pts = [gps("V1", i, lat=50 + i * 0.05) for i in range(6)]
    trip = split_trips(pts, FLEET)[0]
    events = [FuelEvent("V1", T0 - timedelta(minutes=2), "level", 50.0), FuelEvent("V1", T0 + timedelta(minutes=7), "level", 45.0)]
    res = fuel_for_trip(trip, events, FLEET)
    assert res.liters == 4.0
    assert res.per_100km == pytest.approx(14.39, abs=0.01)


def test_percentile_nearest_rank():
    assert percentile([15, 20, 35, 40, 50], 40) == 20
    assert percentile([3, 1, 2], 100) == 3


def test_alert_cooldown_suppresses_repeats():
    pts = [gps("V1", i, speed=150) for i in range(3)] + [gps("V1", 3, speed=50)]
    pts += [gps("V1", 6 + i, speed=150) for i in range(3)] + [gps("V1", 9, speed=50)]
    trips = split_trips(pts, FLEET)
    engine = AlertEngine(cooldown=timedelta(minutes=20))
    alerts = engine.evaluate(AlertContext(trips, [], FLEET, FenceIndex([])))
    assert [a.rule for a in alerts] == ["speeding"]
    assert engine.suppressed == 1


def test_trips_csv_header_and_rows():
    trips = split_trips([gps("V1", 0), gps("V1", 1, lat=50.01)], FLEET)
    lines = trips_csv(trips).splitlines()
    assert lines[0].startswith("vehicle;start;end;km")
    assert lines[1].startswith("V1;2026-05-04T08:00:00;")


def test_pipeline_end_to_end():
    lines = ["VEH;V1;AC-1;Aachen;van;80"]
    for i in range(6):
        lines.append(f"GPS;V1;2026-05-04T08:0{i}:00;{50 + i * 0.01};6.0;60;1")
    lines.append("GPS;V1;2026-05-04T08:01:00;50.01;6.0;60;1")  # duplicate
    lines.append("GPS;V9;2026-05-04T08:01:00;50.01;6.0;60;1")  # without master data
    fences = [Geofence("Aachen", "depot", ((49.9, 5.9), (49.9, 6.1), (50.1, 6.1), (50.1, 5.9)))]
    result = analyze(lines, fences)
    assert result.duplicate_points == 1
    assert len(result.trips) == 1  # V9 has only one point – no trip
    assert any("V9" in w for w in result.warnings)
    out = render(result, fences)
    assert "Trips: 1" in out["report"]
