"""PPA-scoped Shapely helpers (PAL service-area clip / containment).

Kept out of ``services.geometry`` so EXZ/GWPZ/DPA/IAP keep existing semantics.
"""

from __future__ import annotations

from typing import Any

from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

# Matches harness isPpaWithinServiceArea: ppa.buffer(-1e-6).within(SA).
# ~0.11 m; applied only to PPA/claimed vs licensed service-area membership.
PPA_SA_BUFFER_DEG = 1e-6


def geojson_to_shapely(geom: dict[str, Any] | None) -> BaseGeometry | None:
    if not isinstance(geom, dict):
        return None
    kind = geom.get("type")
    if kind == "FeatureCollection":
        parts = []
        for feat in geom.get("features") or []:
            if not isinstance(feat, dict) or not isinstance(feat.get("geometry"), dict):
                continue
            parts.append(shape(feat["geometry"]))
        if not parts:
            return None
        return unary_union(parts)
    if kind == "Feature":
        inner = geom.get("geometry")
        if not isinstance(inner, dict):
            return None
        return shape(inner)
    if kind == "GeometryCollection":
        geoms = geom.get("geometries") or []
        parts = [shape(g) for g in geoms if isinstance(g, dict)]
        if not parts:
            return None
        return unary_union(parts)
    try:
        return shape(geom)
    except (ValueError, TypeError, AttributeError):
        return None


def _areal(geom: BaseGeometry | None) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    gtype = geom.geom_type
    if gtype in {"Polygon", "MultiPolygon"}:
        return geom
    if gtype == "GeometryCollection":
        parts = [
            g
            for g in geom.geoms
            if g.geom_type in {"Polygon", "MultiPolygon"} and not g.is_empty
        ]
        if not parts:
            return None
        return unary_union(parts)
    return None


def shapely_to_geojson(geom: BaseGeometry | None) -> dict[str, Any] | None:
    areal = _areal(geom)
    if areal is None:
        return None
    mapped = mapping(areal)
    if not isinstance(mapped, dict):
        return None
    return mapped


def as_feature_collection(geom: dict[str, Any] | None) -> dict[str, Any] | None:
    gj = geom
    if gj is None:
        return None
    if gj.get("type") == "FeatureCollection":
        return gj
    if gj.get("type") == "Feature":
        return {"type": "FeatureCollection", "features": [gj]}
    if gj.get("type") in {"Polygon", "MultiPolygon"}:
        return {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": gj}],
        }
    shaped = geojson_to_shapely(gj)
    out = shapely_to_geojson(shaped)
    if out is None:
        return None
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": out}],
    }


def union_geojson(*geoms: dict[str, Any] | None) -> dict[str, Any] | None:
    parts = []
    for geom in geoms:
        shaped = _areal(geojson_to_shapely(geom))
        if shaped is not None:
            parts.append(shaped)
    if not parts:
        return None
    return shapely_to_geojson(unary_union(parts))


def intersect_geojson(
    a: dict[str, Any] | None, b: dict[str, Any] | None
) -> dict[str, Any] | None:
    sa = _areal(geojson_to_shapely(a))
    sb = _areal(geojson_to_shapely(b))
    if sa is None or sb is None:
        return None
    return shapely_to_geojson(sa.intersection(sb))


def polygon_within_service_area(
    contour: dict[str, Any] | None,
    service_area: dict[str, Any] | None,
    *,
    buffer_deg: float | None = PPA_SA_BUFFER_DEG,
) -> bool:
    """True when contour interior lies in SA (reference buffer-then-within)."""
    ppa = _areal(geojson_to_shapely(contour))
    sa = _areal(geojson_to_shapely(service_area))
    if ppa is None or sa is None:
        return False
    candidate = ppa.buffer(-abs(buffer_deg)) if buffer_deg else ppa
    if candidate.is_empty:
        return bool(sa.covers(ppa))
    return bool(candidate.within(sa))


def polygon_covered_by(
    inner: dict[str, Any] | None, outer: dict[str, Any] | None
) -> bool:
    """Full topology: outer covers inner (claimed ⊆ RF max). No SA epsilon."""
    a = _areal(geojson_to_shapely(inner))
    b = _areal(geojson_to_shapely(outer))
    if a is None or b is None:
        return False
    return bool(b.covers(a))


def point_covered_by_area(
    lat: float, lon: float, area: dict[str, Any] | None
) -> bool:
    from shapely.geometry import Point

    sa = _areal(geojson_to_shapely(area))
    if sa is None:
        return False
    return bool(sa.covers(Point(float(lon), float(lat))))
