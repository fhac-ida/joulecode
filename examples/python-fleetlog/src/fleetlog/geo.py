"""Geometry on the sphere: distances, bearing, geofences, route simplification."""

from __future__ import annotations

import math
from collections.abc import Sequence

from fleetlog.models import Geofence, GpsPoint

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def point_distance_km(a: GpsPoint, b: GpsPoint) -> float:
    return haversine_km(a.lat, a.lon, b.lat, b.lon)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from 1 to 2 in degrees (0 = north, clockwise)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def turn_angle_deg(a: GpsPoint, b: GpsPoint, c: GpsPoint) -> float:
    """Change of direction at b (0 = straight on, 180 = U-turn)."""
    h1 = bearing_deg(a.lat, a.lon, b.lat, b.lon)
    h2 = bearing_deg(b.lat, b.lon, c.lat, c.lon)
    diff = abs(h2 - h1) % 360
    return 360 - diff if diff > 180 else diff


def in_bbox(lat: float, lon: float, box: tuple[float, float, float, float]) -> bool:
    return box[0] <= lat <= box[2] and box[1] <= lon <= box[3]


def point_in_polygon(lat: float, lon: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray casting; points on the boundary count as inside."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if (yi, xi) == (lat, lon):
            return True
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if abs(x_cross - lon) < 1e-12:
                return True
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


class FenceIndex:
    """Geofences with precomputed bounding boxes (fast pre-filter)."""

    def __init__(self, fences: Sequence[Geofence]) -> None:
        self.fences = list(fences)
        self._boxes = [f.bbox() for f in self.fences]

    def containing(self, lat: float, lon: float) -> list[Geofence]:
        return [
            fence
            for fence, box in zip(self.fences, self._boxes, strict=True)
            if in_bbox(lat, lon, box) and point_in_polygon(lat, lon, fence.polygon)
        ]

    def first(self, lat: float, lon: float, kind: str | None = None) -> Geofence | None:
        for fence, box in zip(self.fences, self._boxes, strict=True):
            if kind is not None and fence.kind != kind:
                continue
            if in_bbox(lat, lon, box) and point_in_polygon(lat, lon, fence.polygon):
                return fence
        return None


def _perpendicular_km(p: GpsPoint, a: GpsPoint, b: GpsPoint) -> float:
    """Distance from p to the segment a–b (locally approximated as flat)."""
    lat0 = math.radians((a.lat + b.lat) / 2)
    kx = 111.32 * math.cos(lat0)
    ky = 110.57
    ax, ay = a.lon * kx, a.lat * ky
    bx, by = b.lon * kx, b.lat * ky
    px, py = p.lon * kx, p.lat * ky
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points: Sequence[GpsPoint], tolerance_km: float) -> list[GpsPoint]:
    """Douglas-Peucker (iterative). The first and the last point are always kept."""
    n = len(points)
    if n <= 2:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        best, best_d = -1, tolerance_km
        for i in range(lo + 1, hi):
            d = _perpendicular_km(points[i], points[lo], points[hi])
            if d > best_d:
                best, best_d = i, d
        if best >= 0:
            keep[best] = True
            stack.append((lo, best))
            stack.append((best, hi))
    return [p for p, k in zip(points, keep, strict=True) if k]


def path_length_km(points: Sequence[GpsPoint]) -> float:
    return sum(point_distance_km(a, b) for a, b in zip(points, points[1:], strict=False))


def centroid(points: Sequence[GpsPoint]) -> tuple[float, float]:
    if not points:
        raise ValueError("centroid() without points")
    return (sum(p.lat for p in points) / len(points), sum(p.lon for p in points) / len(points))
