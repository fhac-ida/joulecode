"""fleetlog – telematics analysis of a vehicle fleet (joule:code example with a lot of code).

Deliberately contains a few inefficiencies among a lot of unremarkable code; see
examples/suite/README.md.
"""

from fleetlog.models import Alert, FuelEvent, Geofence, GpsPoint, Severity, Trip, Vehicle, VehicleKind
from fleetlog.pipeline import FleetAnalysis, analyze, render

__all__ = [
    "Alert",
    "FleetAnalysis",
    "FuelEvent",
    "Geofence",
    "GpsPoint",
    "Severity",
    "Trip",
    "Vehicle",
    "VehicleKind",
    "analyze",
    "render",
]
