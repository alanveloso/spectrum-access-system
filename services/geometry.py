"""Lightweight geospatial helpers (no GIS dependencies).

GeoJSON rings use [longitude, latitude] as required by RFC 7946.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

EARTH_RADIUS_M = 6_371_000.0


def is_point_in_polygon(
    lat: float, lng: float, polygon: Sequence[Sequence[float]]
) -> bool:
    """Ray-casting point-in-polygon.

 Args:
 lat: Point latitude (degrees).
 lng: Point longitude (degrees).
 polygon: Linear ring as [[lon, lat], ...] (GeoJSON order).
 """
    return _point_in_ring(lng, lat, polygon)


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


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _dist_point_segment_m(
    lat: float,
    lon: float,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Approximate distance from point to a great-circle segment (local projection)."""
    lat0 = math.radians(lat)

    def to_xy(la: float, lo: float) -> tuple[float, float]:
        return (
            (math.radians(lo) - math.radians(lon)) * math.cos(lat0) * EARTH_RADIUS_M,
            (math.radians(la) - math.radians(lat)) * EARTH_RADIUS_M,
        )

    ax, ay = to_xy(lat1, lon1)
    bx, by = to_xy(lat2, lon2)
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, (-ax * abx + -ay * aby) / denom))
    return math.hypot(ax + t * abx, ay + t * aby)


def distance_to_ring_m(
    lat: float, lon: float, ring: Sequence[Sequence[float]]
) -> float:
    """Minimum distance (meters) from point to polygon ring; 0 if inside."""
    if _point_in_ring(lon, lat, ring):
        return 0.0
    if len(ring) < 2:
        return float("inf")
    best = float("inf")
    for i in range(len(ring) - 1):
        a, b = ring[i], ring[i + 1]
        d = _dist_point_segment_m(
            lat, lon, float(a[1]), float(a[0]), float(b[1]), float(b[0])
        )
        if d < best:
            best = d
    return best


def _ring_bbox(
    ring: Sequence[Sequence[float]],
) -> tuple[float, float, float, float] | None:
    if not ring:
        return None
    lons = [float(p[0]) for p in ring]
    lats = [float(p[1]) for p in ring]
    return min(lons), min(lats), max(lons), max(lats)


def _expand_bbox_m(
    bbox: tuple[float, float, float, float],
    meters: float,
    lat_ref: float,
) -> tuple[float, float, float, float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    dlat = meters / 111_320.0
    cos_lat = max(0.2, abs(math.cos(math.radians(lat_ref))))
    dlon = meters / (111_320.0 * cos_lat)
    return min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat


def _in_bbox(
    lon: float, lat: float, bbox: tuple[float, float, float, float]
) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def iter_geojson_rings(zone: dict[str, Any] | None) -> list[list[list[float]]]:
    """Extract outer rings from a GeoJSON FeatureCollection / Geometry."""
    if not zone:
        return []
    rings: list[list[list[float]]] = []

    def _from_geom(geom: dict[str, Any]) -> None:
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        if gtype == "Polygon" and coords:
            rings.append(coords[0])
        elif gtype == "MultiPolygon":
            for poly in coords:
                if poly:
                    rings.append(poly[0])

    ztype = zone.get("type")
    if ztype == "FeatureCollection":
        for feature in zone.get("features") or []:
            geom = feature.get("geometry") or {}
            if isinstance(geom, dict):
                _from_geom(geom)
    elif ztype == "Feature":
        geom = zone.get("geometry") or {}
        if isinstance(geom, dict):
            _from_geom(geom)
    elif ztype in ("Polygon", "MultiPolygon"):
        _from_geom(zone)
    return rings


def point_in_geojson(lat: float, lon: float, zone: dict[str, Any] | None) -> bool:
    for ring in iter_geojson_rings(zone):
        if _point_in_ring(lon, lat, ring):
            return True
    return False


def distance_to_geojson_m(
    lat: float, lon: float, zone: dict[str, Any] | None
) -> float:
    """Minimum distance to any ring in the GeoJSON; 0 if inside."""
    best = float("inf")
    for ring in iter_geojson_rings(zone):
        bbox = _ring_bbox(ring)
        if bbox is not None:
            # Skip rings whose expanded bbox cannot be within ~1 km (speed).
            expanded = _expand_bbox_m(bbox, 1_000.0, lat)
            if not _in_bbox(lon, lat, expanded) and not _point_in_ring(lon, lat, ring):
                # Still need a lower bound — use corner distance as rough filter.
                corners = [
                    (bbox[1], bbox[0]),
                    (bbox[1], bbox[2]),
                    (bbox[3], bbox[0]),
                    (bbox[3], bbox[2]),
                ]
                rough = min(haversine_m(lat, lon, cla, clo) for cla, clo in corners)
                if rough > best:
                    continue
        d = distance_to_ring_m(lat, lon, ring)
        if d < best:
            best = d
            if best == 0.0:
                return 0.0
    return best


def geojson_geometry_usable(zone: dict[str, Any] | None) -> bool:
    """True when zone yields at least one valid closed outer ring."""
    if not zone or not isinstance(zone, dict):
        return False
    rings = iter_geojson_rings(zone)
    if not rings:
        return False
    return any(_ring_is_valid(ring) for ring in rings)


def within_geojson_buffer_m(
    lat: float,
    lon: float,
    zone: dict[str, Any] | None,
    buffer_m: float,
) -> bool:
    """True if point is inside zone or within buffer_m of its boundary."""
    if not zone:
        return False
    # Portal scheduled DPAs may be Point placemarks (e.g. coastal reference).
    if zone.get("type") == "Point":
        coords = zone.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                zlon = float(coords[0])
                zlat = float(coords[1])
            except (TypeError, ValueError):
                return False
            return haversine_m(lat, lon, zlat, zlon) <= buffer_m
        return False
    for ring in iter_geojson_rings(zone):
        if not _ring_is_valid(ring):
            continue
        bbox = _ring_bbox(ring)
        if bbox is not None:
            expanded = _expand_bbox_m(bbox, buffer_m + 5.0, lat)
            if not _in_bbox(lon, lat, expanded):
                continue
        if distance_to_ring_m(lat, lon, ring) <= buffer_m:
            return True
    return False


_EPS = 1e-12


def _ring_is_valid(ring: Sequence[Sequence[float]]) -> bool:
    """Closed ring with at least 3 distinct vertices (4 coords with closure)."""
    if len(ring) < 4:
        return False
    try:
        pts = [(float(p[0]), float(p[1])) for p in ring]
    except (TypeError, ValueError, IndexError):
        return False
    if pts[0] != pts[-1]:
        return False
    # At least 3 unique vertices excluding the closing duplicate.
    unique = {(round(x, 12), round(y, 12)) for x, y in pts[:-1]}
    return len(unique) >= 3


def _point_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > 1e-9:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -_EPS:
        return False
    len_sq = (bx - ax) * (bx - ax) + (by - ay) * (by - ay)
    return dot <= len_sq + _EPS


def _point_on_ring_boundary(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    for i in range(len(ring) - 1):
        ax, ay = float(ring[i][0]), float(ring[i][1])
        bx, by = float(ring[i + 1][0]), float(ring[i + 1][1])
        if _point_on_segment(lon, lat, ax, ay, bx, by):
            return True
    return False


def _point_strictly_inside_ring(
    lon: float, lat: float, ring: Sequence[Sequence[float]]
) -> bool:
    if _point_on_ring_boundary(lon, lat, ring):
        return False
    return _point_in_ring(lon, lat, ring)


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_properly_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    """True when segments cross with an interior point (not endpoint-only touch)."""
    ax, ay = a1
    bx, by = a2
    cx, cy = b1
    dx, dy = b2
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)
    # Proper crossing: orientations differ on both segments.
    if (o1 > _EPS and o2 < -_EPS or o1 < -_EPS and o2 > _EPS) and (
        o3 > _EPS and o4 < -_EPS or o3 < -_EPS and o4 > _EPS
    ):
        return True
    return False


def _rings_area_overlap(
    ring_a: Sequence[Sequence[float]], ring_b: Sequence[Sequence[float]]
) -> bool:
    """True if polygon interiors intersect (containment or proper edge cross).

    Boundary-only contact (shared vertex / shared edge without interior overlap)
    returns False.
    """
    if not _ring_is_valid(ring_a) or not _ring_is_valid(ring_b):
        return False

    # Proper edge crossings (X-shaped intersection without shared vertices).
    for i in range(len(ring_a) - 1):
        a1 = (float(ring_a[i][0]), float(ring_a[i][1]))
        a2 = (float(ring_a[i + 1][0]), float(ring_a[i + 1][1]))
        for j in range(len(ring_b) - 1):
            b1 = (float(ring_b[j][0]), float(ring_b[j][1]))
            b2 = (float(ring_b[j + 1][0]), float(ring_b[j + 1][1]))
            if _segments_properly_intersect(a1, a2, b1, b2):
                return True

    # Vertex of one polygon strictly inside the other.
    for lon, lat, *_rest in ring_a[:-1]:
        if _point_strictly_inside_ring(float(lon), float(lat), ring_b):
            return True
    for lon, lat, *_rest in ring_b[:-1]:
        if _point_strictly_inside_ring(float(lon), float(lat), ring_a):
            return True
    return False


def geojson_areas_overlap(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Return True when GeoJSON polygon areas have intersecting interiors.

 Accepts the same shapes as :func:`iter_geojson_rings` (Polygon, MultiPolygon,
 Feature, FeatureCollection). Outer rings only (holes ignored), matching the
 rest of ``services.geometry``.

 Semantics for Admin PPA conflict checks:
 - separated polygons → False
 - one contained in the other → True
 - vertex of one strictly inside the other → True
 - edges properly cross → True
 - boundary-only touch (shared vertex/edge, no interior overlap) → False
 - invalid / empty geometry → False (caller validates contours separately)
 """
    if not a or not b:
        return False
    rings_a = [r for r in iter_geojson_rings(a) if _ring_is_valid(r)]
    rings_b = [r for r in iter_geojson_rings(b) if _ring_is_valid(r)]
    if not rings_a or not rings_b:
        return False
    for ra in rings_a:
        for rb in rings_b:
            if _rings_area_overlap(ra, rb):
                return True
    return False
