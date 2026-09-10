"""PAT Admin propagation / antenna model query."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from main import app
from services.propagation import (
    PropagationEngines,
    PropagationRequestError,
    compute_propagation_and_antenna_model,
)
from services.propagation.service import ACTIVITY_LOSS_FACTOR_DEFAULT
import database

client = TestClient(app)


@pytest.fixture(autouse=True)
def _db_schema():
    database.init_db(retries=1, delay_seconds=0)
    yield


@dataclass
class _Angles:
    hor_cbsd: float = 10.0
    hor_rx: float = 20.0
    ver_rx: float = -5.0


def _path_loss(db_loss: float, *, bearing: float = 45.0) -> Any:
    return SimpleNamespace(
        db_loss=db_loss,
        incidence_angles=_Angles(hor_cbsd=bearing, hor_rx=bearing + 180.0, ver_rx=-2.0),
    )


def _fake_engines(**overrides: Any) -> PropagationEngines:
    base = dict(
        calc_itm=lambda *a, **k: _path_loss(100.0),
        calc_hybrid=lambda *a, **k: _path_loss(90.0),
        calc_p2108=lambda *a, **k: 3.0,
        activity_loss_factor=ACTIVITY_LOSS_FACTOR_DEFAULT,
        antenna_standard_gains=lambda *a, **k: 6.5,
        antenna_fss_gains=lambda *a, **k: 1.25,
        grid_polygon=lambda _geom, _arc: [(-77.1, 38.9)],
        region_nlcd_vote=lambda _pts: "SUBURBAN",
        terrain_elevation_m=lambda _lat, _lon: 50.0,
    )
    base.update(overrides)
    return PropagationEngines(**base)


def _cbsd(**extra: Any) -> dict[str, Any]:
    body = {
        "latitude": 38.9,
        "longitude": -77.1,
        "height": 4.0,
        "heightType": "AGL",
        "indoorDeployment": False,
        "antennaAzimuth": 90,
        "antennaBeamwidth": 60,
        "antennaGain": 10,
    }
    body.update(extra)
    return body


def test_fss_model_returns_pathloss_and_tx_gain():
    engines = _fake_engines()
    result = compute_propagation_and_antenna_model(
        {
            "modelType": "1",
            "reliabilityLevel": 0.05,
            "cbsd": _cbsd(),
            "fss": {
                "latitude": 38.91,
                "longitude": -77.11,
                "height": 5.0,
                "antennaAzimuth": 10,
                "antennaElevation": 5,
                "antennaGain": 30,
            },
        },
        engines=engines,
    )
    assert result["pathlossDb"] == 100.0
    assert result["txAntennaGainDbi"] == 6.5
    assert "rxAntennaGainDbi" not in result


def test_fss_rx_gain_when_key_present():
    result = compute_propagation_and_antenna_model(
        {
            "modelType": "1",
            "reliabilityLevel": -1,
            "cbsd": _cbsd(),
            "fss": {
                "latitude": 38.91,
                "longitude": -77.11,
                "height": 5.0,
                "antennaAzimuth": 10,
                "antennaElevation": 5,
                "antennaGain": 30,
                "rxAntennaGainRequired": False,
            },
        },
        engines=_fake_engines(),
    )
    assert result["rxAntennaGainDbi"] == 1.25


def test_ppa_hybrid_path():
    result = compute_propagation_and_antenna_model(
        {
            "modelType": "2",
            "reliabilityLevel": -1,
            "cbsd": _cbsd(),
            "ppa": {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}},
        },
        engines=_fake_engines(),
    )
    assert result["pathlossDb"] == 90.0
    assert result["txAntennaGainDbi"] == 6.5


def test_ppa_rejects_multi_point_grid():
    engines = _fake_engines(grid_polygon=lambda *_a, **_k: [(0.0, 0.0), (1.0, 1.0)])
    with pytest.raises(PropagationRequestError, match="more than a single"):
        compute_propagation_and_antenna_model(
            {
                "modelType": "2",
                "reliabilityLevel": -1,
                "cbsd": _cbsd(),
                "ppa": {"geometry": {}},
            },
            engines=engines,
        )


def test_dpa_adds_clutter_and_activity_loss():
    from services.propagation.rel1ext_dpa import calc_p2108_clutter_db, compose_dpa_pathloss_db

    cbsd = _cbsd()
    dpa = {
        "latitude": 38.0,
        "longitude": -76.0,
        "height": 50.0,
        "heightType": "AGL",
    }
    result = compute_propagation_and_antenna_model(
        {
            "modelType": "3",
            "reliabilityLevel": 0.5,
            "cbsd": cbsd,
            "dpaPoint": dpa,
        },
        engines=_fake_engines(
            calc_itm=lambda *a, **k: _path_loss(80.0),
            # Injected harness P.2108 must be ignored for Type-3 (UUT owns formula).
            calc_p2108=lambda *a, **k: 2.0,
        ),
    )
    clutter = calc_p2108_clutter_db(
        cbsd["latitude"],
        cbsd["longitude"],
        cbsd["height"],
        dpa["latitude"],
        dpa["longitude"],
    )
    assert result["pathlossDb"] == pytest.approx(compose_dpa_pathloss_db(80.0, clutter))
    assert set(result) == {"pathlossDb"}


def test_dpa_amsl_converts_via_terrain():
    from services.propagation.rel1ext_dpa import calc_p2108_clutter_db, compose_dpa_pathloss_db

    seen: dict[str, float] = {}

    def itm(*args, **kwargs):
        # rx height is 6th positional after lat/lon/h tx and lat/lon rx
        seen["rx_height"] = args[5]
        return _path_loss(70.0)

    cbsd = _cbsd()
    dpa = {
        "latitude": 38.0,
        "longitude": -76.0,
        "height": 150.0,
        "heightType": "AMSL",
    }
    def terrain(*_: object) -> float:
        return 50.0

    result = compute_propagation_and_antenna_model(
        {
            "modelType": "3",
            "reliabilityLevel": 0.5,
            "cbsd": cbsd,
            "dpaPoint": dpa,
        },
        engines=_fake_engines(
            calc_itm=itm,
            terrain_elevation_m=terrain,
            calc_p2108=lambda *a, **k: 0.0,
        ),
    )
    assert seen["rx_height"] == pytest.approx(100.0)
    clutter = calc_p2108_clutter_db(
        cbsd["latitude"],
        cbsd["longitude"],
        cbsd["height"],
        dpa["latitude"],
        dpa["longitude"],
        terrain_elevation_m=terrain,
    )
    assert result["pathlossDb"] == pytest.approx(compose_dpa_pathloss_db(70.0, clutter))


def test_invalid_reliability_rejected():
    with pytest.raises(PropagationRequestError, match="reliabilityLevel"):
        compute_propagation_and_antenna_model(
            {
                "modelType": "1",
                "reliabilityLevel": 0.5,
                "cbsd": _cbsd(),
                "fss": {"latitude": 1, "longitude": 2, "height": 3},
            },
            engines=_fake_engines(),
        )


def test_dpa_requires_reliability_half():
    with pytest.raises(PropagationRequestError, match="reliabilityLevel is not 0.5"):
        compute_propagation_and_antenna_model(
            {
                "modelType": "3",
                "reliabilityLevel": 0.05,
                "cbsd": _cbsd(),
                "dpaPoint": {
                    "latitude": 1,
                    "longitude": 2,
                    "height": 3,
                    "heightType": "AGL",
                },
            },
            engines=_fake_engines(),
        )


def test_admin_route_400_on_invalid_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "services.propagation.load_reference_engines",
        lambda: _fake_engines(),
    )
    resp = client.post(
        "/admin/query/propagation_and_antenna_model",
        json={"reliabilityLevel": 0.5, "cbsd": _cbsd()},
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_admin_route_200_fss_with_injected_engines(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "services.propagation.load_reference_engines",
        lambda: _fake_engines(),
    )
    resp = client.post(
        "/admin/query/propagation_and_antenna_model",
        json={
            "modelType": "1",
            "reliabilityLevel": 0.95,
            "cbsd": _cbsd(),
            "fss": {"latitude": 38.91, "longitude": -77.11, "height": 5.0},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pathlossDb"] == 100.0
    assert body["txAntennaGainDbi"] == 6.5


def test_admin_route_no_longer_returns_501(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "services.propagation.load_reference_engines",
        lambda: _fake_engines(),
    )
    resp = client.post(
        "/admin/query/propagation_and_antenna_model",
        json={
            "modelType": "3",
            "reliabilityLevel": 0.5,
            "cbsd": _cbsd(),
            "dpaPoint": {
                "latitude": 38.0,
                "longitude": -76.0,
                "height": 10.0,
                "heightType": "AGL",
            },
        },
    )
    assert resp.status_code == 200
    assert resp.status_code != 501


def test_itm_value_error_maps_to_request_error():
    engines = _fake_engines(
        calc_itm=lambda *a, **k: (_ for _ in ()).throw(ValueError("bad profile"))
    )
    with pytest.raises(PropagationRequestError, match="bad profile"):
        compute_propagation_and_antenna_model(
            {
                "modelType": "1",
                "reliabilityLevel": 0.05,
                "cbsd": _cbsd(),
                "fss": {"latitude": 38.91, "longitude": -77.11, "height": 5.0},
            },
            engines=engines,
        )


def test_fss_rx_gain_requires_antenna_fields():
    with pytest.raises(PropagationRequestError, match="antennaElevation"):
        compute_propagation_and_antenna_model(
            {
                "modelType": "1",
                "reliabilityLevel": -1,
                "cbsd": _cbsd(),
                "fss": {
                    "latitude": 38.91,
                    "longitude": -77.11,
                    "height": 5.0,
                    "antennaAzimuth": 10,
                    "antennaGain": 30,
                    "rxAntennaGainRequired": True,
                },
            },
            engines=_fake_engines(),
        )


def test_admin_route_503_when_engines_unavailable(monkeypatch: pytest.MonkeyPatch):
    from services.propagation import PropagationUnavailableError

    monkeypatch.setattr(
        "services.propagation.load_reference_engines",
        lambda: (_ for _ in ()).throw(PropagationUnavailableError("no itm")),
    )
    resp = client.post(
        "/admin/query/propagation_and_antenna_model",
        json={
            "modelType": "1",
            "reliabilityLevel": 0.05,
            "cbsd": _cbsd(),
            "fss": {"latitude": 38.91, "longitude": -77.11, "height": 5.0},
        },
    )
    assert resp.status_code == 503
    assert "no itm" in resp.json()["detail"]


def test_repeatability_identical_for_same_inputs():
    engines = _fake_engines()
    req = {
        "modelType": "1",
        "reliabilityLevel": 0.05,
        "cbsd": _cbsd(),
        "fss": {"latitude": 38.91, "longitude": -77.11, "height": 5.0},
    }
    a = compute_propagation_and_antenna_model(req, engines=engines)
    b = compute_propagation_and_antenna_model(req, engines=engines)
    assert a == b
