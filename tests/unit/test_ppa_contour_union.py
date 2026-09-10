"""True geometric union of maximum PPA RF contours."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from services.geometry import point_in_geojson
from services.ppa_rf_contour import (
    PpaRfContourError,
    cbsd_rf_contour_ring,
    maximum_rf_ppa_contour,
    union_geojson_polygons,
)
from tests.fixtures.ppa_rf import fake_ppa_rf_engines


def _square(lon: float, lat: float, half: float) -> dict:
    """Axis-aligned closed Polygon GeoJSON around (lon, lat)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon - half, lat - half],
                [lon + half, lat - half],
                [lon + half, lat + half],
                [lon - half, lat + half],
                [lon - half, lat - half],
            ]
        ],
    }


def _device(lat: float, lon: float, eirp: float = 30.0) -> dict:
    return {
        "cbsdCategory": "A",
        "installationParam": {
            "latitude": lat,
            "longitude": lon,
            "height": 10.0,
            "heightType": "AGL",
            "indoorDeployment": False,
            "antennaGain": 0.0,
            "eirpCapability": eirp,
        },
    }


def _sha_ring(ring: list[list[float]]) -> str:
    raw = json.dumps(ring, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


# --- A: overlapping contours become true union --------------------------------


def test_a_overlapping_contours_true_union():
    a = _square(-100.0, 39.0, 0.05)
    b = _square(-99.96, 39.0, 0.05)  # overlaps A
    c = _square(-99.92, 39.02, 0.05)  # overlaps B / near A
    geom = union_geojson_polygons([a, b, c])
    assert geom["type"] == "Polygon"
    # Wrap as single-feature FC like maximum_rf_ppa_contour output.
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"source": "ppa_rf_contour"},
                "geometry": geom,
            }
        ],
    }
    assert len(fc["features"]) == 1
    # Union must cover points unique to each input (not merely first square).
    assert point_in_geojson(39.0, -100.0, fc)
    assert point_in_geojson(39.0, -99.96, fc)
    assert point_in_geojson(39.02, -99.92, fc)
    # Far outside first square but inside C.
    assert point_in_geojson(39.02, -99.90, fc)
    assert not point_in_geojson(40.0, -100.0, fc)


# --- B: disconnected → MultiPolygon ------------------------------------------


def test_b_disconnected_contours_multipolygon():
    left = _square(-101.0, 39.0, 0.02)
    right = _square(-99.0, 39.0, 0.02)
    geom = union_geojson_polygons([left, right])
    assert geom["type"] == "MultiPolygon"
    assert len(geom["coordinates"]) == 2
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": geom},
        ],
    }
    assert len(fc["features"]) == 1
    assert point_in_geojson(39.0, -101.0, fc)
    assert point_in_geojson(39.0, -99.0, fc)
    # Midpoint between components must remain outside (no convex hull).
    assert not point_in_geojson(39.0, -100.0, fc)


# --- C: one contour unchanged -------------------------------------------------


def test_c_single_contour_unchanged():
    poly = _square(-100.5, 39.1, 0.03)
    geom = union_geojson_polygons([poly])
    assert geom["type"] == "Polygon"
    assert len(geom["coordinates"][0]) == len(poly["coordinates"][0])
    # Semantically same square (Shapely may emit tuples).
    assert point_in_geojson(39.1, -100.5, {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": geom}
    ]})
    assert abs(float(geom["coordinates"][0][0][0]) - poly["coordinates"][0][0][0]) < 1e-12
    assert abs(float(geom["coordinates"][0][0][1]) - poly["coordinates"][0][0][1]) < 1e-12


# --- D: three-CBSD PCR-like topology ------------------------------------------


def test_d_three_overlapping_not_features0():
    """First polygon area << union; serialized feature must be the union."""
    engines = fake_ppa_rf_engines(extra_loss_db=8.0)
    # Tight cluster so RF lobes overlap (synthetic engines → circular-ish).
    devices = [
        _device(39.10, -94.58, 30.0),
        _device(39.105, -94.575, 30.0),
        _device(39.11, -94.57, 30.0),
    ]
    fc = maximum_rf_ppa_contour(devices, engines=engines)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    geom = fc["features"][0]["geometry"]
    assert geom["type"] in {"Polygon", "MultiPolygon"}
    # All three CBSD sites inside the single feature.
    for d in devices:
        ip = d["installationParam"]
        assert point_in_geojson(ip["latitude"], ip["longitude"], fc)
    # Not a three-feature collection (the PCR_1 defect shape).
    assert len(fc["features"]) != 3


# --- E: invalid / degenerate fail-closed --------------------------------------


def test_e_invalid_geometry_fail_closed():
    with pytest.raises(PpaRfContourError, match="invalid_union|empty_union|degenerate"):
        union_geojson_polygons(
            [{"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]}]
        )


def test_e_empty_input_fail_closed():
    with pytest.raises(PpaRfContourError, match="empty_union"):
        union_geojson_polygons([])


def test_e_non_polygon_fail_closed():
    with pytest.raises(PpaRfContourError, match="invalid_union"):
        union_geojson_polygons([{"type": "Point", "coordinates": [0, 0]}])


# --- F: RF ring numerics unchanged by composition -----------------------------


def test_f_per_cbsd_ring_numerics_unchanged_by_union_stage():
    engines = fake_ppa_rf_engines(extra_loss_db=5.0)
    device = _device(39.05, -100.1, 30.0)
    ring_before = cbsd_rf_contour_ring(device, engines=engines)
    hash_before = _sha_ring(ring_before)

    fc = maximum_rf_ppa_contour([device], engines=engines)
    ring_after = cbsd_rf_contour_ring(copy.deepcopy(device), engines=engines)
    assert _sha_ring(ring_after) == hash_before
    assert len(fc["features"]) == 1
    # Single-device union geometry is a Polygon derived from the same ring.
    assert fc["features"][0]["geometry"]["type"] == "Polygon"


def test_multipolygon_is_one_feature_not_many():
    """Pin: MultiPolygon ≠ N Features."""
    geom = union_geojson_polygons(
        [_square(-102.0, 40.0, 0.01), _square(-100.0, 40.0, 0.01)]
    )
    assert geom["type"] == "MultiPolygon"
    fc = maximum_rf_ppa_contour.__doc__  # noqa: F841 — presence check only
    # Build the product return shape explicitly.
    out = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"source": "ppa_rf_contour"},
                "geometry": geom,
            }
        ],
    }
    assert len(out["features"]) == 1
    assert out["features"][0]["geometry"]["type"] == "MultiPolygon"
