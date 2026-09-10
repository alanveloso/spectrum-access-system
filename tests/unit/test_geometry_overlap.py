"""Geometric area overlap for Admin PPA conflict checks ( follow-up)."""

from __future__ import annotations

from services.geometry import geojson_areas_overlap
from services.ppa_service import _geometries_overlap


def _poly(coords: list[list[float]]) -> dict:
    ring = list(coords)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    return {"type": "Polygon", "coordinates": [ring]}


def _fc(poly: dict) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": poly}],
    }


def test_separated_polygons_do_not_overlap():
    a = _poly([[0, 0], [1, 0], [1, 1], [0, 1]])
    b = _poly([[3, 3], [4, 3], [4, 4], [3, 4]])
    assert geojson_areas_overlap(a, b) is False
    assert _geometries_overlap(a, b) is False


def test_one_polygon_contained_in_other_overlaps():
    outer = _poly([[0, 0], [4, 0], [4, 4], [0, 4]])
    inner = _poly([[1, 1], [2, 1], [2, 2], [1, 2]])
    assert geojson_areas_overlap(outer, inner) is True
    assert geojson_areas_overlap(inner, outer) is True


def test_vertex_strictly_inside_other_overlaps():
    """A vertex of one polygon lies in the interior of the other."""
    a = _poly([[0, 0], [2, 0], [2, 2], [0, 2]])
    # Diamond whose left vertex (1,1) is inside A; other vertices outside A.
    b = _poly([[1, 1], [3, 0], [4, 1], [3, 2]])
    assert geojson_areas_overlap(a, b) is True


def test_edge_crossing_only_overlaps():
    """Classic cross bars: no vertex of either lies inside the other."""
    horizontal = _poly([[0, 0], [4, 0], [4, 1], [0, 1]])
    vertical = _poly([[1.5, -1], [2.5, -1], [2.5, 2], [1.5, 2]])
    assert geojson_areas_overlap(horizontal, vertical) is True
    # FeatureCollection / MultiPolygon paths used by create_ppa contours.
    assert geojson_areas_overlap(_fc(horizontal), vertical) is True
    multi = {
        "type": "MultiPolygon",
        "coordinates": [vertical["coordinates"]],
    }
    assert geojson_areas_overlap(horizontal, multi) is True


def test_boundary_only_touch_is_not_overlap():
    """Shared edge / shared vertex without interior intersection → not a conflict.

    Admin PPA semantics: adjacent service areas may share a boundary.
    """
    left = _poly([[0, 0], [1, 0], [1, 1], [0, 1]])
    right = _poly([[1, 0], [2, 0], [2, 1], [1, 1]])  # shares edge x=1
    assert geojson_areas_overlap(left, right) is False

    # Shared corner only.
    a = _poly([[0, 0], [1, 0], [1, 1], [0, 1]])
    b = _poly([[1, 1], [2, 1], [2, 2], [1, 2]])
    assert geojson_areas_overlap(a, b) is False


def test_invalid_geometry_does_not_claim_overlap():
    assert geojson_areas_overlap(None, _poly([[0, 0], [1, 0], [1, 1], [0, 1]])) is False
    assert geojson_areas_overlap({}, {}) is False
    # Degenerate: fewer than 3 distinct vertices.
    bad = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]}
    good = _poly([[0, 0], [2, 0], [2, 2], [0, 2]])
    assert geojson_areas_overlap(bad, good) is False
    # Unclosed / empty FeatureCollection.
    empty_fc = {"type": "FeatureCollection", "features": []}
    assert geojson_areas_overlap(empty_fc, good) is False
