"""Geographic value objects: point, distance, point/radius, polygon ring.

Coordinates: latitude/longitude in degrees. Distances in metres. Linear rings
use GeoJSON order ``[longitude, latitude]`` (RFC 7946).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude_deg: float
    longitude_deg: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError("latitude_deg must be in [-90, 90]")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError("longitude_deg must be in [-180, 180]")


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    p1, p2 = math.radians(a.latitude_deg), math.radians(b.latitude_deg)
    dphi = math.radians(b.latitude_deg - a.latitude_deg)
    dlmb = math.radians(b.longitude_deg - a.longitude_deg)
    h = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


@dataclass(frozen=True, slots=True)
class PointRadius:
    center: GeoPoint
    radius_m: float

    def __post_init__(self) -> None:
        if self.radius_m < 0.0:
            raise ValueError("radius_m must be non-negative")

    def contains(self, point: GeoPoint) -> bool:
        return haversine_m(self.center, point) <= self.radius_m


def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


@dataclass(frozen=True, slots=True)
class LinearRing:
    """Closed or open ring as ``[[lon, lat], ...]``. Containment is ray-casting."""

    coordinates: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.coordinates) < 3:
            raise ValueError("LinearRing requires at least 3 vertices")

    @classmethod
    def from_lon_lat(cls, coords: Sequence[Sequence[float]]) -> LinearRing:
        pairs = tuple((float(p[0]), float(p[1])) for p in coords)
        return cls(coordinates=pairs)

    def contains(self, point: GeoPoint) -> bool:
        return _point_in_ring(point.longitude_deg, point.latitude_deg, self.coordinates)


def representative_point(
    location: GeoPoint | PointRadius | LinearRing,
) -> GeoPoint | None:
    """Point used for containment tests; rings have no unique representative."""
    if isinstance(location, GeoPoint):
        return location
    if isinstance(location, PointRadius):
        return location.center
    return None
