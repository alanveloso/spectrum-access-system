"""Rel1Ext DPA AMSL terrain wiring (no IPR fixture IDs/coords)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from models.models import Grant
from services.dpa_protection import (
    DpaGrantRf,
    DpaPathLossModel,
    DpaPathLossUnavailable,
    default_rel1ext_path_loss_fn,
    make_path_loss_fn,
    proposed_grant_violates_dpa,
    rel1ext_dpa_path_loss_db,
)
from services.dpa_service import activate_dpa, clear_activations, load_dpas
from services.propagation.rel1ext_dpa import ACTIVITY_LOSS_FACTOR_DB, calc_p2108_clutter_db
from services.terrain.vincenty import geodesic_point
from tests.fixtures.factories import make_cbsd, make_fcc_id, make_user_id

client = TestClient(app, raise_server_exceptions=True)

_LAT, _LON = 33.10, -117.20
_RX_LAT, _RX_LON = geodesic_point(_LAT, _LON, 1.0, 90.0)

_SYNTH_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>SyntheticAmslDpa</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3560</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>80</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
        <Data name="refHeightMeters"><value>50</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -94.70,39.20,0 -94.40,39.20,0 -94.40,39.00,0 -94.70,39.00,0 -94.70,39.20,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


def _grant(*, height_m: float, height_is_agl: bool) -> DpaGrantRf:
    return DpaGrantRf(
        grant_id="g-amsl",
        cbsd_id="c-amsl",
        latitude=_LAT,
        longitude=_LON,
        height_m=height_m,
        height_is_agl=height_is_agl,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )


def test_amsl_default_rel1ext_completes_when_terrain_injected():
    called: list[tuple[float, float]] = []

    def terrain(lat: float, lon: float) -> float:
        called.append((lat, lon))
        return 100.0

    def itm(grant, lat_rx, lon_rx, height_rx):
        return 110.0

    fn = default_rel1ext_path_loss_fn(itm_median_fn=itm, terrain_elevation_m=terrain)
    total = fn(_grant(height_m=105.0, height_is_agl=False), _RX_LAT, _RX_LON, 50.0)
    assert called
    assert called[0] == (_LAT, _LON)
    clutter = calc_p2108_clutter_db(
        _LAT, _LON, 5.0, _RX_LAT, _RX_LON, is_height_cbsd_amsl=False
    )
    assert total == pytest.approx(110.0 + clutter + ACTIVITY_LOSS_FACTOR_DB)


def test_amsl_missing_terrain_is_dpa_unavailable_not_valueerror():
    fn = default_rel1ext_path_loss_fn(itm_median_fn=lambda *a, **k: 110.0)
    with pytest.raises(DpaPathLossUnavailable, match="terrain"):
        fn(_grant(height_m=105.0, height_is_agl=False), _RX_LAT, _RX_LON, 50.0)


def test_amsl_rel1ext_path_loss_db_missing_terrain_is_unavailable():
    with pytest.raises(DpaPathLossUnavailable, match="terrain"):
        rel1ext_dpa_path_loss_db(
            _grant(height_m=105.0, height_is_agl=False),
            _RX_LAT,
            _RX_LON,
            50.0,
            median_path_loss_db=100.0,
        )


def test_terrain_backend_failure_is_unavailable_and_fail_closed(
    db_session: Session, tmp_path: Path
):
    def boom(lat: float, lon: float) -> float:
        raise OSError("ned tile missing")

    fn = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 110.0,
        terrain_elevation_m=boom,
    )
    with pytest.raises(DpaPathLossUnavailable, match="terrain"):
        fn(_grant(height_m=105.0, height_is_agl=False), _RX_LAT, _RX_LON, 50.0)

    kml = tmp_path / "dpa_amsl.kml"
    kml.write_text(_SYNTH_KML, encoding="utf-8")
    load_dpas(db_session, kml_paths=[kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "SyntheticAmslDpa",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    cbsd = make_cbsd(
        db_session,
        cbsd_id="c-dpa-amsl",
        registration={
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 39.10,
                "longitude": -94.58,
                "height": 105.0,
                "heightType": "AMSL",
                "indoorDeployment": False,
            },
        },
    )
    assert proposed_grant_violates_dpa(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
        path_loss_fn=fn,
    ) is True


def test_agl_does_not_require_terrain_and_matches_existing_numeric():
    called: list[tuple[float, float]] = []

    def terrain(lat: float, lon: float) -> float:
        called.append((lat, lon))
        return 999.0

    agl = _grant(height_m=5.0, height_is_agl=True)
    with_terrain = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 100.0,
        terrain_elevation_m=terrain,
    )(agl, _RX_LAT, _RX_LON, 50.0)
    without = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 100.0,
    )(agl, _RX_LAT, _RX_LON, 50.0)
    clutter = calc_p2108_clutter_db(_LAT, _LON, 5.0, _RX_LAT, _RX_LON)
    assert called == []
    assert with_terrain == pytest.approx(without)
    assert with_terrain == pytest.approx(100.0 + clutter + ACTIVITY_LOSS_FACTOR_DB)


def test_amsl_derived_agl_five_metres_matches_agl_formula():
    terrain = lambda lat, lon: 100.0  # noqa: E731
    amsl = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 100.0,
        terrain_elevation_m=terrain,
    )(_grant(height_m=105.0, height_is_agl=False), _RX_LAT, _RX_LON, 50.0)
    agl5 = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 100.0,
    )(_grant(height_m=5.0, height_is_agl=True), _RX_LAT, _RX_LON, 50.0)
    assert amsl == pytest.approx(agl5)
    assert calc_p2108_clutter_db(_LAT, _LON, 5.0, _RX_LAT, _RX_LON) > 0.0


def test_amsl_derived_agl_above_six_metres_clutter_zero():
    terrain = lambda lat, lon: 90.0  # noqa: E731
    total = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 100.0,
        terrain_elevation_m=terrain,
    )(_grant(height_m=105.0, height_is_agl=False), _RX_LAT, _RX_LON, 50.0)
    assert total == pytest.approx(100.0 + 0.0 + ACTIVITY_LOSS_FACTOR_DB)


def _seed_dpa_and_register_amsl(db_session: Session, tmp_path: Path) -> str:
    kml = tmp_path / "dpa_amsl.kml"
    kml.write_text(_SYNTH_KML, encoding="utf-8")
    load_dpas(db_session, kml_paths=[kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "SyntheticAmslDpa",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    resp = client.post(
        "/v1.2/registration",
        json={
            "registrationRequest": [
                {
                    "fccId": fcc.fcc_id,
                    "cbsdSerialNumber": "sn-dpa-amsl",
                    "userId": user.user_id,
                    "cbsdCategory": "A",
                    "airInterface": {"radioTechnology": "E_UTRA"},
                    "installationParam": {
                        "latitude": 39.10,
                        "longitude": -94.58,
                        "height": 105.0,
                        "heightType": "AMSL",
                        "indoorDeployment": True,
                    },
                }
            ]
        },
    )
    assert resp.status_code == 200
    return resp.json()["registrationResponse"][0]["cbsdId"]


def _grant_http(cbsd_id: str):
    return client.post(
        "/v1.2/grant",
        json={
            "grantRequest": [
                {
                    "cbsdId": cbsd_id,
                    "operationParam": {
                        "maxEirp": 20.0,
                        "operationFrequencyRange": {
                            "lowFrequency": 3_550_000_000,
                            "highFrequency": 3_560_000_000,
                        },
                    },
                }
            ]
        },
    )


def test_http_amsl_dpa_aggregate_reject_is_protocol_400(
    db_session: Session, tmp_path: Path, monkeypatch
):
    from services import dpa_protection as dpa_mod

    cbsd_id = _seed_dpa_and_register_amsl(db_session, tmp_path)
    fn = make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=lambda *a, **k: 80.0,
        terrain_elevation_m=lambda *_: 100.0,
    )
    monkeypatch.setattr(dpa_mod, "default_rel1ext_path_loss_fn", lambda **_: fn)
    resp = _grant_http(cbsd_id)
    assert resp.status_code == 200
    body = resp.json()["grantResponse"][0]
    assert body["cbsdId"] == cbsd_id
    assert body["response"]["responseCode"] == 400
    assert "grantId" not in body
    assert db_session.query(Grant).count() == 0


def test_http_amsl_terrain_unavailable_fail_closed_400(
    db_session: Session, tmp_path: Path, monkeypatch
):
    from services import dpa_protection as dpa_mod

    cbsd_id = _seed_dpa_and_register_amsl(db_session, tmp_path)
    fn = default_rel1ext_path_loss_fn(itm_median_fn=lambda *a, **k: 110.0)
    monkeypatch.setattr(dpa_mod, "default_rel1ext_path_loss_fn", lambda **_: fn)
    resp = _grant_http(cbsd_id)
    assert resp.status_code == 200
    body = resp.json()["grantResponse"][0]
    assert body["response"]["responseCode"] == 400
    assert "grantId" not in body
    assert db_session.query(Grant).count() == 0
