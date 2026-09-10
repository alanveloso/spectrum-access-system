"""Rel1Ext PAT.2 Type-3 DPA path loss (clutter + network loading)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.propagation import (
    PropagationEngines,
    compute_propagation_and_antenna_model,
)
from services.propagation.rel1ext_dpa import (
    ACTIVITY_LOSS_FACTOR_DB,
    CLUTTER_TX_AGL_MAX_M,
    PAT2_PATHLOSS_TOLERANCE_DB,
    P2108_LOSS_2KM_DB,
    calc_p2108_clutter_db,
    compose_dpa_pathloss_db,
    pathloss_within_pat2_tolerance,
)
from services.propagation.service import ACTIVITY_LOSS_FACTOR_DEFAULT
from services.terrain.vincenty import geodesic_distance_km, geodesic_point

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PAT2 = (
    REPO_ROOT.parent
    / "winnforum-sas-harness"
    / "src"
    / "harness"
    / "testcases"
    / "testdata"
    / "pat2_test_data.json"
)


def _path_loss(db_loss: float) -> Any:
    return SimpleNamespace(
        db_loss=db_loss,
        incidence_angles=SimpleNamespace(hor_cbsd=0.0, hor_rx=180.0, ver_rx=0.0),
    )


def _engines(**overrides: Any) -> PropagationEngines:
    base: dict[str, Any] = dict(
        calc_itm=lambda *a, **k: _path_loss(100.0),
        calc_hybrid=lambda *a, **k: _path_loss(90.0),
        activity_loss_factor=ACTIVITY_LOSS_FACTOR_DEFAULT,
        antenna_standard_gains=lambda *a, **k: 99.0,
        antenna_fss_gains=lambda *a, **k: 99.0,
        grid_polygon=lambda *_a, **_k: [(-77.1, 38.9)],
        region_nlcd_vote=lambda *_: "SUBURBAN",
        terrain_elevation_m=lambda *_: 0.0,
    )
    base.update(overrides)
    if "calc_p2108" not in overrides:
        terrain_fn = base["terrain_elevation_m"]

        def _p2108(*a: Any, **k: Any) -> float:
            return calc_p2108_clutter_db(
                a[0],
                a[1],
                a[2],
                a[3],
                a[4],
                is_height_cbsd_amsl=k.get("is_height_cbsd_amsl", False),
                terrain_elevation_m=terrain_fn,
            )

        base["calc_p2108"] = _p2108
    return PropagationEngines(**base)


def _dpa_request(*, height_m: float, height_type: str = "AGL", **extra: Any) -> dict[str, Any]:
    # Synthetic geometry (not harness fixture IDs); distance ~ tens of km → clutter cap path.
    body = {
        "modelType": "3",
        "reliabilityLevel": 0.5,
        "cbsd": {
            "latitude": 38.0,
            "longitude": -77.0,
            "height": height_m,
            "heightType": height_type,
            "indoorDeployment": True,
            "antennaAzimuth": 90,
            "antennaGain": 16,
            "antennaBeamwidth": 30,
        },
        "dpaPoint": {
            "latitude": 38.1,
            "longitude": -77.1,
            "height": 50.0,
            "heightType": "AGL",
        },
    }
    body.update(extra)
    return body


def test_compose_adds_itm_clutter_and_activity():
    assert compose_dpa_pathloss_db(80.0, 2.0) == pytest.approx(90.0)
    assert ACTIVITY_LOSS_FACTOR_DB == 8.0


def test_pat2_tolerance_strict_less_than_ref_plus_margin():
    assert pathloss_within_pat2_tolerance(100.0, 100.0)
    assert pathloss_within_pat2_tolerance(100.9, 100.0)
    assert not pathloss_within_pat2_tolerance(101.0, 100.0)
    assert PAT2_PATHLOSS_TOLERANCE_DB == 1.0


def test_p2108_zero_when_tx_agl_above_six_metres():
    # Far enough for non-zero clutter if height allowed.
    lat1, lon1 = 33.0, -117.0
    lat2, lon2 = geodesic_point(lat1, lon1, 1.0, 90.0)
    assert geodesic_distance_km(lat1, lon1, lat2, lon2) == pytest.approx(1.0, abs=1e-3)
    assert calc_p2108_clutter_db(lat1, lon1, 6.01, lat2, lon2) == 0.0
    assert calc_p2108_clutter_db(lat1, lon1, CLUTTER_TX_AGL_MAX_M, lat2, lon2) > 0.0


def test_p2108_caps_at_two_km_and_zero_below_quarter_km():
    lat1, lon1 = 33.0, -117.0
    near_lat, near_lon = geodesic_point(lat1, lon1, 0.1, 0.0)
    far_lat, far_lon = geodesic_point(lat1, lon1, 3.0, 0.0)
    assert calc_p2108_clutter_db(lat1, lon1, 3.0, near_lat, near_lon) == 0.0
    assert calc_p2108_clutter_db(lat1, lon1, 3.0, far_lat, far_lon) == pytest.approx(
        P2108_LOSS_2KM_DB
    )


def test_dpa_type3_low_agl_includes_clutter_and_loading():
    req = _dpa_request(height_m=3.0)
    result = compute_propagation_and_antenna_model(
        req,
        engines=_engines(calc_itm=lambda *a, **k: _path_loss(70.0)),
    )
    clutter = calc_p2108_clutter_db(
        req["cbsd"]["latitude"],
        req["cbsd"]["longitude"],
        3.0,
        req["dpaPoint"]["latitude"],
        req["dpaPoint"]["longitude"],
    )
    assert clutter > 0.0
    assert result["pathlossDb"] == pytest.approx(compose_dpa_pathloss_db(70.0, clutter))
    assert set(result) == {"pathlossDb"}


def test_dpa_type3_ignores_injected_harness_p2108_engine():
    """Type-3 must use the UUT P.2108 formula, not engines.calc_p2108 (no dual path)."""
    engines = _engines(
        calc_itm=lambda *a, **k: _path_loss(70.0),
        calc_p2108=lambda *a, **k: 999.0,
    )
    req = _dpa_request(height_m=10.0)
    result = compute_propagation_and_antenna_model(req, engines=engines)
    assert result["pathlossDb"] == pytest.approx(70.0 + 0.0 + 8.0)
    assert result["pathlossDb"] != pytest.approx(70.0 + 999.0 + 8.0)


def test_dpa_type3_high_agl_still_applies_network_loading():
    result = compute_propagation_and_antenna_model(
        _dpa_request(height_m=10.0),
        engines=_engines(calc_itm=lambda *a, **k: _path_loss(70.0)),
    )
    # Clutter gate → 0; activity/loading still mandatory for all CBSDs.
    assert result["pathlossDb"] == pytest.approx(70.0 + 0.0 + 8.0)
    assert "txAntennaGainDbi" not in result
    assert "rxAntennaGainDbi" not in result


def test_dpa_type3_matches_reference_oracle_within_pat2_tolerance():
    """Same engines → UUT equals reference composition (strictly within +1 dB)."""
    engines = _engines(calc_itm=lambda *a, **k: _path_loss(55.5))
    req = _dpa_request(height_m=4.0)
    uut = compute_propagation_and_antenna_model(req, engines=engines)
    clutter = calc_p2108_clutter_db(
        req["cbsd"]["latitude"],
        req["cbsd"]["longitude"],
        req["cbsd"]["height"],
        req["dpaPoint"]["latitude"],
        req["dpaPoint"]["longitude"],
    )
    ref = compose_dpa_pathloss_db(55.5, clutter)
    assert pathloss_within_pat2_tolerance(uut["pathlossDb"], ref)
    assert uut["pathlossDb"] == pytest.approx(ref)


def test_pat2_trial_matrix_from_harness_config_when_present():
    """Exercise official PAT.2 CBSD×DPA request shapes and Rel1Ext cohort gates.

    ITM is stubbed (compiled extension often absent). This validates wiring,
    response shape, and ≤6 m / >6 m clutter gates — not numerical ITM±1 dB
    tolerance against a live reference model.
    """
    if not HARNESS_PAT2.is_file():
        pytest.skip("harness pat2_test_data.json not available")

    data = json.loads(HARNESS_PAT2.read_text(encoding="utf-8"))
    cbsds = data["cbsds"]
    dpa_points = data["dpa_points"]
    assert len(cbsds) >= 1 and len(dpa_points) >= 1

    heights = [float(c["height"]) for c in cbsds if c.get("heightType", "AGL") == "AGL"]
    assert any(h <= CLUTTER_TX_AGL_MAX_M for h in heights)
    assert any(h > CLUTTER_TX_AGL_MAX_M for h in heights)

    engines = _engines(
        calc_itm=lambda *a, **k: _path_loss(40.0),
        calc_p2108=lambda *a, **k: 999.0,  # must be ignored by Type-3 UUT path
    )
    trials = 0
    for cbsd in cbsds:
        for dpa in dpa_points:
            trials += 1
            req = {
                "modelType": "3",
                "reliabilityLevel": 0.5,
                "cbsd": {
                    "latitude": cbsd["latitude"],
                    "longitude": cbsd["longitude"],
                    "height": cbsd["height"],
                    "heightType": cbsd.get("heightType", "AGL"),
                    "indoorDeployment": bool(cbsd["indoorDeployment"]),
                },
                "dpaPoint": {
                    "latitude": dpa["latitude"],
                    "longitude": dpa["longitude"],
                    "height": dpa["height"],
                    "heightType": dpa.get("heightType", "AGL"),
                },
            }
            uut = compute_propagation_and_antenna_model(req, engines=engines)
            clutter = calc_p2108_clutter_db(
                float(cbsd["latitude"]),
                float(cbsd["longitude"]),
                float(cbsd["height"]),
                float(dpa["latitude"]),
                float(dpa["longitude"]),
                is_height_cbsd_amsl=(cbsd.get("heightType") == "AMSL"),
                terrain_elevation_m=engines.terrain_elevation_m,
            )
            height_type = cbsd.get("heightType", "AGL")
            height_agl = float(cbsd["height"])
            if height_type == "AMSL":
                height_agl -= float(
                    engines.terrain_elevation_m(
                        float(cbsd["latitude"]), float(cbsd["longitude"])
                    )
                )
            if height_agl > CLUTTER_TX_AGL_MAX_M:
                assert clutter == 0.0
            assert uut["pathlossDb"] == pytest.approx(
                compose_dpa_pathloss_db(40.0, clutter)
            )
            assert uut["pathlossDb"] != pytest.approx(40.0 + 999.0 + 8.0)
            assert set(uut) == {"pathlossDb"}

    assert trials == len(cbsds) * len(dpa_points)
