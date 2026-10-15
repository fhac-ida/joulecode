"""Holdout tests for fleetlog – joule:code does not see them.

They check behaviour that an obvious optimisation easily changes.
"""

from datetime import datetime, timedelta

from fleetlog import GpsPoint, Vehicle, VehicleKind
from fleetlog.fuel import LEVEL_WINDOW, fuel_for_trip, fuel_report
from fleetlog.models import FuelEvent
from fleetlog.reports import percentile
from fleetlog.trips import FleetIndex, dedupe_points, split_trips

T0 = datetime(2026, 5, 4, 8, 0)


def gps(vid, minute, lat=50.0, speed=50.0, ign=True):
    return GpsPoint(vid, T0 + timedelta(minutes=minute), lat, 6.0, speed, ign)


def test_dedupe_key_is_vehicle_and_time_only():
    # A second report with different coordinates is still a duplicate – the first one wins.
    a = gps("V1", 0, lat=50.0)
    b = gps("V1", 0, lat=50.5)
    assert dedupe_points([a, b]) == [a]


def test_dedupe_keeps_input_order():
    pts = [gps("V2", 3), gps("V1", 1), gps("V2", 3), gps("V1", 0), gps("V1", 1)]
    assert dedupe_points(pts) == [pts[0], pts[1], pts[3]]


def test_dedupe_same_time_different_vehicles():
    a, b = gps("V1", 0), gps("V2", 0)
    assert dedupe_points([a, b]) == [a, b]


def test_dedupe_empty():
    assert dedupe_points([]) == []


def test_fleet_duplicate_id_first_wins():
    fleet = FleetIndex(
        [Vehicle("V1", "AC-1", "Aachen", VehicleKind.TRUCK, 400), Vehicle("V1", "AC-9", "Köln", VehicleKind.CAR, 50)]
    )
    assert fleet.vehicle("V1").plate == "AC-1"
    assert fleet.kind_of("V1") is VehicleKind.TRUCK
    assert fleet.depot_of("V1") == "Aachen"


def test_fleet_unknown_vehicle():
    fleet = FleetIndex([])
    assert fleet.vehicle("X") is None
    assert fleet.kind_of("X") is VehicleKind.CAR


def test_inactive_vehicle_has_no_trips():
    fleet = FleetIndex([Vehicle("V1", "AC-1", "Aachen", VehicleKind.VAN, 80, active=False)])
    assert split_trips([gps("V1", i, lat=50 + i * 0.01) for i in range(4)], fleet) == []


def test_unknown_vehicle_still_gets_trips():
    trips = split_trips([gps("V7", i, lat=50 + i * 0.01) for i in range(4)], FleetIndex([]))
    assert len(trips) == 1


def test_trip_order_ties_by_vehicle():
    fleet = FleetIndex([])
    pts = [gps(v, i, lat=50 + i * 0.01) for v in ("V3", "V1", "V2") for i in range(3)]
    assert [t.vehicle_id for t in split_trips(pts, fleet)] == ["V1", "V2", "V3"]


def _trip_and_fleet():
    fleet = FleetIndex([Vehicle("V1", "AC-1", "Aachen", VehicleKind.VAN, 100)])
    trip = split_trips([gps("V1", i, lat=50 + i * 0.05) for i in range(6)], fleet)[0]
    return trip, fleet


def test_fuel_level_ties_first_reading_wins():
    trip, fleet = _trip_and_fleet()
    before = T0 - timedelta(minutes=3)
    after = trip.end + timedelta(minutes=2)
    events = [
        FuelEvent("V1", before, "level", 50.0),
        FuelEvent("V1", before, "level", 52.0),  # same time: does not count
        FuelEvent("V1", after, "level", 44.0),
        FuelEvent("V1", after, "level", 40.0),  # same time: does not count
    ]
    assert fuel_for_trip(trip, events, fleet).liters == 6.0


def test_fuel_window_is_inclusive():
    trip, fleet = _trip_and_fleet()
    events = [
        FuelEvent("V1", trip.start - LEVEL_WINDOW, "level", 50.0),
        FuelEvent("V1", trip.end + LEVEL_WINDOW, "level", 45.0),
    ]
    assert fuel_for_trip(trip, events, fleet).liters == 5.0
    events.append(FuelEvent("V1", trip.start - LEVEL_WINDOW - timedelta(seconds=1), "level", 99.0))
    assert fuel_for_trip(trip, events, fleet).liters == 5.0


def test_fuel_events_unsorted_and_other_vehicles():
    trip, fleet = _trip_and_fleet()
    events = [
        FuelEvent("V1", trip.end + timedelta(minutes=5), "level", 40.0),
        FuelEvent("V2", trip.start - timedelta(minutes=1), "level", 99.0),
        FuelEvent("V1", trip.end + timedelta(minutes=1), "level", 42.0),
        FuelEvent("V1", trip.start - timedelta(minutes=20), "level", 49.0),
        FuelEvent("V1", trip.start - timedelta(minutes=1), "level", 50.0),
        FuelEvent("V1", trip.start + timedelta(minutes=2), "refuel", 10.0),
    ]
    res = fuel_for_trip(trip, events, fleet)
    assert res.liters == 18.0  # (50 - 42)% of 100 l + 10 l refuelled


def test_fuel_report_keeps_trip_order_and_length():
    trip, fleet = _trip_and_fleet()
    report = fuel_report([trip, trip], [], fleet)
    assert [r.note for r in report] == ["no fuel level", "no fuel level"]


def test_percentile_does_not_sort_input():
    values = [5.0, 1.0, 3.0]
    assert percentile(values, 50) == 3.0
    assert values == [5.0, 1.0, 3.0]
