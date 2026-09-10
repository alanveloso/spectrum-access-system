"""Rel1Ext HBT.13 TxExpire — AGL/AMSL, catalogue fail-closed."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from models.models import AdminInjectedData
from services import dpa_neighborhood as dpa_nb
from services.dpa_neighborhood import DpaNeighborhoodStatus
from services.dpa_service import (
    KIND_CATALOGUE,
    clear_activations,
    load_dpas,
)
from services.grant_service import process_grant
from services.heartbeat_service import process_heartbeat
from services.terrain.exceptions import TerrainDataUnavailable
from services.terrain.fake import CallableTerrainProvider
from tests.fixtures.factories import cat_a_install, make_cbsd, make_grant

SUCCESS = 0

_SYNTH_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>TxExpireAlpha</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3650</value></Data>
        <Data name="catA_Indoor_6m_NeighborhoodDistanceKm"><value>30</value></Data>
        <Data name="catA_Indoor_NeighborhoodDistanceKm"><value>50</value></Data>
        <Data name="catA_Outdoor_6m_NeighborhoodDistanceKm"><value>40</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>60</value></Data>
        <Data name="catB_6m_NeighborhoodDistanceKm"><value>70</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>80</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -105.30,40.00,0 -105.20,40.00,0 -105.20,40.10,0 -105.30,40.10,0 -105.30,40.00,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""

_KML_NO_GEOMETRY_ACTIVE = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>NoGeomDpa</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3650</value></Data>
        <Data name="catA_Indoor_6m_NeighborhoodDistanceKm"><value>30</value></Data>
      </ExtendedData>
    </Placemark>
  </Document>
</kml>
"""


@pytest.fixture
def dpa_catalogue(db_session, tmp_path: Path):
    """Load DPA geometry/neighborhoods without leaving channels active (HBT.13)."""
    path = tmp_path / "tx-expire-dpa.kml"
    path.write_text(_SYNTH_KML, encoding="utf-8")
    load_dpas(db_session, kml_paths=[path])
    clear_activations(db_session, commit=True)
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).count() == 1
    return path


@pytest.fixture(autouse=True)
def _reset_terrain():
    dpa_nb.reset_terrain_provider()
    yield
    dpa_nb.reset_terrain_provider()


def _install(
    *,
    lat: float,
    lon: float,
    height: float,
    height_type: str = "AGL",
    indoor: bool = True,
    category: str = "A",
) -> dict:
    inst = cat_a_install(lat=lat, lon=lon, indoor=indoor, height=height)
    inst["heightType"] = height_type
    return {"cbsdCategory": category, "installationParam": inst}


def _near_cbsd(
    db_session,
    *,
    height: float = 3.0,
    height_type: str = "AGL",
    indoor: bool = True,
    category: str = "A",
):
    """Inside CatA indoor ≤6 m neighborhood (~east of polygon edge)."""
    cbsd = make_cbsd(db_session, cbsd_category=category)
    cbsd.registration_json = json.dumps(
        _install(
            lat=40.05,
            lon=-105.15,
            height=height,
            height_type=height_type,
            indoor=indoor,
            category=category,
        )
    )
    db_session.commit()
    return cbsd


def _far_cbsd(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = json.dumps(
        _install(lat=35.0, lon=-110.0, height=3.0, indoor=True, category="A")
    )
    db_session.commit()
    return cbsd


def _tx_delta_sec(iso_tx: str) -> float:
    from services.clock import ensure_utc, utc_now

    tx = ensure_utc(datetime.strptime(iso_tx, "%Y-%m-%dT%H:%M:%SZ"))
    return (tx - utc_now().replace(microsecond=0)).total_seconds()


def _heartbeat_tx(db_session, cbsd, *, low: int, high: int, monkeypatch, default_sec=3600):
    monkeypatch.setattr(dpa_nb, "TRANSMIT_EXPIRE_SEC", default_sec)
    grant = make_grant(
        db_session,
        cbsd,
        authorized=False,
        lifecycle_state="GRANTED",
        low_hz=low,
        high_hz=high,
    )
    grant.grant_expire_time = datetime.utcnow().replace(microsecond=0) + timedelta(hours=3)
    db_session.commit()
    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "GRANTED",
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS
    return _tx_delta_sec(resp[0]["transmitExpireTime"])


def test_neighborhood_key_prefers_height_and_indoor_buckets():
    nb = {
        "catA_Indoor_6m_NeighborhoodDistanceKm": 11.0,
        "catA_Indoor_NeighborhoodDistanceKm": 22.0,
        "catA_Outdoor_6m_NeighborhoodDistanceKm": 33.0,
        "catA_Outdoor_NeighborhoodDistanceKm": 44.0,
        "catB_6m_NeighborhoodDistanceKm": 55.0,
        "catBNeighborhoodDistanceKm": 66.0,
    }
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category="A", indoor=True, height_agl_m=4.0
        )
        == 11.0
    )
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category="A", indoor=True, height_agl_m=8.0
        )
        == 22.0
    )
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category="A", indoor=False, height_agl_m=4.0
        )
        == 33.0
    )
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category="A", indoor=False, height_agl_m=8.0
        )
        == 44.0
    )
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category="B", indoor=False, height_agl_m=4.0
        )
        == 55.0
    )
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category="B", indoor=False, height_agl_m=8.0
        )
        == 66.0
    )


def test_agl_4m_and_8m_select_distinct_indoor_radii(db_session, dpa_catalogue):
    cbsd_lo = _near_cbsd(db_session, height=4.0, height_type="AGL", indoor=True)
    cbsd_hi = _near_cbsd(db_session, height=8.0, height_type="AGL", indoor=True)
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            {
                "catA_Indoor_6m_NeighborhoodDistanceKm": 30.0,
                "catA_Indoor_NeighborhoodDistanceKm": 50.0,
            },
            category="A",
            indoor=True,
            height_agl_m=4.0,
        )
        == 30.0
    )
    assert (
        dpa_nb.neighborhood_radius_km_for_cbsd(
            {
                "catA_Indoor_6m_NeighborhoodDistanceKm": 30.0,
                "catA_Indoor_NeighborhoodDistanceKm": 50.0,
            },
            category="A",
            indoor=True,
            height_agl_m=8.0,
        )
        == 50.0
    )
    assert dpa_nb.evaluate_dpa_neighborhood(db_session, cbsd_lo) is DpaNeighborhoodStatus.INSIDE
    assert dpa_nb.evaluate_dpa_neighborhood(db_session, cbsd_hi) is DpaNeighborhoodStatus.INSIDE


def test_amsl_equivalent_to_agl_selects_same_radius(db_session, dpa_catalogue):
    ground = 1000.0
    dpa_nb.set_terrain_provider(CallableTerrainProvider(lambda _lat, _lon: ground))
    # AGL 4 m ↔ AMSL 1004 m; AGL 8 m ↔ AMSL 1008 m
    cbsd_agl4 = _near_cbsd(db_session, height=4.0, height_type="AGL")
    cbsd_amsl4 = _near_cbsd(db_session, height=ground + 4.0, height_type="AMSL")
    cbsd_agl8 = _near_cbsd(db_session, height=8.0, height_type="AGL")
    cbsd_amsl8 = _near_cbsd(db_session, height=ground + 8.0, height_type="AMSL")

    for agl_cbsd, amsl_cbsd in ((cbsd_agl4, cbsd_amsl4), (cbsd_agl8, cbsd_amsl8)):
        _, _, h_agl, indoor, cat = dpa_nb._cbsd_lat_lon_height_agl(agl_cbsd)
        _, _, h_amsl, indoor2, cat2 = dpa_nb._cbsd_lat_lon_height_agl(amsl_cbsd)
        assert h_agl == pytest.approx(h_amsl)
        assert indoor == indoor2 and cat == cat2
        nb = {
            "catA_Indoor_6m_NeighborhoodDistanceKm": 30.0,
            "catA_Indoor_NeighborhoodDistanceKm": 50.0,
        }
        assert dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category=cat, indoor=indoor, height_agl_m=h_agl
        ) == dpa_nb.neighborhood_radius_km_for_cbsd(
            nb, category=cat2, indoor=indoor2, height_agl_m=h_amsl
        )


def test_amsl_without_terrain_is_indeterminate_not_silent_agl(db_session, dpa_catalogue):
    def _boom(lat: float, lon: float) -> float:
        raise TerrainDataUnavailable("no tiles")

    dpa_nb.set_terrain_provider(CallableTerrainProvider(_boom))
    cbsd = _near_cbsd(db_session, height=1004.0, height_type="AMSL")
    assert (
        dpa_nb.evaluate_dpa_neighborhood(db_session, cbsd)
        is DpaNeighborhoodStatus.INDETERMINATE
    )
    # Must not treat AMSL numeric as AGL (would be INSIDE with huge "AGL").
    assert dpa_nb.cbsd_in_any_dpa_neighborhood(db_session, cbsd) is False


def test_amsl_terrain_missing_applies_240_cap_fail_closed(
    db_session, dpa_catalogue, monkeypatch
):
    def _boom(lat: float, lon: float) -> float:
        raise TerrainDataUnavailable("no tiles")

    dpa_nb.set_terrain_provider(CallableTerrainProvider(_boom))
    cbsd = _near_cbsd(db_session, height=1004.0, height_type="AMSL")
    delta = _heartbeat_tx(
        db_session, cbsd, low=3_550_000_000, high=3_560_000_000, monkeypatch=monkeypatch
    )
    assert 0 < delta <= 240


def test_active_dpa_missing_geometry_indeterminate_fail_closed(
    db_session, tmp_path: Path, monkeypatch
):
    path = tmp_path / "nogeom.kml"
    path.write_text(_KML_NO_GEOMETRY_ACTIVE, encoding="utf-8")
    load_dpas(db_session, kml_paths=[path])
    # load_dpas activates channels; geometry absent → INDETERMINATE (not OUTSIDE).
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = json.dumps(
        _install(lat=40.05, lon=-105.15, height=4.0, indoor=True)
    )
    db_session.commit()
    assert (
        dpa_nb.evaluate_dpa_neighborhood(db_session, cbsd)
        is DpaNeighborhoodStatus.INDETERMINATE
    )
    monkeypatch.setattr(dpa_nb, "TRANSMIT_EXPIRE_SEC", 3600)
    assert (
        dpa_nb.transmit_expire_horizon_sec(
            db_session, cbsd, low_hz=3_550_000_000, high_hz=3_560_000_000
        )
        == 240
    )
    # Active DPA also suspends heartbeat (501) — TxExpire fail-closed is evaluated above.
    assert dpa_nb.cbsd_in_any_dpa_neighborhood(db_session, cbsd) is False


def test_empty_catalogue_is_outside(db_session):
    cbsd = _far_cbsd(db_session)
    assert (
        dpa_nb.evaluate_dpa_neighborhood(db_session, cbsd) is DpaNeighborhoodStatus.OUTSIDE
    )


def test_heartbeat_inside_dpa_neighborhood_caps_tx_at_240(
    db_session, dpa_catalogue, monkeypatch
):
    cbsd = _near_cbsd(db_session, height=4.0)
    assert dpa_nb.cbsd_in_any_dpa_neighborhood(db_session, cbsd) is True
    delta = _heartbeat_tx(
        db_session, cbsd, low=3_550_000_000, high=3_560_000_000, monkeypatch=monkeypatch
    )
    assert 0 < delta <= 240


def test_heartbeat_outside_neighborhood_uses_default_horizon(
    db_session, dpa_catalogue, monkeypatch
):
    cbsd = _far_cbsd(db_session)
    assert dpa_nb.cbsd_in_any_dpa_neighborhood(db_session, cbsd) is False
    delta = _heartbeat_tx(
        db_session, cbsd, low=3_550_000_000, high=3_560_000_000, monkeypatch=monkeypatch
    )
    assert 3500 <= delta <= 3600


def test_heartbeat_neighborhood_but_outside_3550_3650_not_capped_to_240(
    db_session, dpa_catalogue, monkeypatch
):
    cbsd = _near_cbsd(db_session)
    delta = _heartbeat_tx(
        db_session, cbsd, low=3_660_000_000, high=3_670_000_000, monkeypatch=monkeypatch
    )
    assert 3500 <= delta <= 3600


def test_grant_response_includes_tx_expire_with_neighborhood_cap(
    db_session, dpa_catalogue, monkeypatch
):
    monkeypatch.setattr(dpa_nb, "TRANSMIT_EXPIRE_SEC", 3600)
    cbsd = _near_cbsd(db_session)
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": {
                    "maxEirp": 20.0,
                    "operationFrequencyRange": {
                        "lowFrequency": 3_550_000_000,
                        "highFrequency": 3_560_000_000,
                    },
                },
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert "transmitExpireTime" in resp[0]
    delta = _tx_delta_sec(resp[0]["transmitExpireTime"])
    assert 0 < delta <= 240


def test_resolve_height_agl_rejects_amsl_without_silent_fallback():
    with pytest.raises(TerrainDataUnavailable):

        def _boom(lat: float, lon: float) -> float:
            raise TerrainDataUnavailable("missing")

        dpa_nb.resolve_height_agl_m(
            40.0,
            -105.0,
            1004.0,
            "AMSL",
            terrain=CallableTerrainProvider(_boom),
        )
