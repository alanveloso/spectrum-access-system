"""DPA catalogue load and Admin activation lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services.dpa_service import (
    KIND_AUDIT,
    KIND_CATALOGUE,
    activate_dpa,
    bulk_dpa_activation,
    channelize,
    deactivate_dpa,
    grant_overlaps_active_dpa,
    list_active_activations,
    list_catalogue,
    load_dpas,
    parse_dpa_kml,
    reset_dpa_state,
)
from services.meas_report import FLAG_DPA_ACTIVE

client = TestClient(app)

assert FLAG_DPA_ACTIVE == "dpa_active"

_SYNTH_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>SynthAlpha</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3570</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>40</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -75.0,38.0,0 -75.1,38.0,0 -75.1,38.1,0 -75.0,38.1,0 -75.0,38.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>SynthBeta</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3560-3580</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>20</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -76.0,39.0,0 -76.1,39.0,0 -76.1,39.1,0 -76.0,39.1,0 -76.0,39.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


@pytest.fixture
def synth_kml(tmp_path: Path) -> Path:
    path = tmp_path / "synth-dpas.kml"
    path.write_text(_SYNTH_KML, encoding="utf-8")
    return path


def test_channelize_emits_10mhz_slots():
    chans = channelize(3_550_000_000, 3_570_000_000)
    assert [(c.low_hz, c.high_hz) for c in chans] == [
        (3_550_000_000, 3_560_000_000),
        (3_560_000_000, 3_570_000_000),
    ]


def test_parse_dpa_kml_extracts_geometry_and_neighborhood(synth_kml: Path):
    defs = parse_dpa_kml(synth_kml)
    assert {d.dpa_id for d in defs} == {"SynthAlpha", "SynthBeta"}
    alpha = next(d for d in defs if d.dpa_id == "SynthAlpha")
    assert alpha.freq_low_hz == 3_550_000_000
    assert alpha.freq_high_hz == 3_570_000_000
    assert alpha.neighborhood_km["catBNeighborhoodDistanceKm"] == 40.0
    assert alpha.geometry is not None
    assert alpha.geometry["type"] == "Polygon"
    assert len(alpha.channels()) == 2


def test_load_dpas_persists_catalogue_and_activates_all_channels(
    db_session, synth_kml: Path
):
    result = load_dpas(db_session, kml_paths=[synth_kml])
    assert result["catalogueSize"] == 2
    # Alpha: 2 channels, Beta: 2 channels
    assert result["activations"] == 4
    catalogue = list_catalogue(db_session)
    assert len(catalogue) == 2
    assert all(item.get("geometry") for item in catalogue)
    assert all(item.get("movelist") == [] for item in catalogue)
    actives = list_active_activations(db_session)
    assert len(actives) == 4
    assert all(a.get("movelist") == [] for a in actives)
    assert grant_overlaps_active_dpa(db_session, 3_550_000_000, 3_560_000_000)
    audits = db_session.query(AdminInjectedData).filter_by(kind=KIND_AUDIT).all()
    assert any("load_dpas" in (r.data_json or "") for r in audits)


def test_bulk_deactivate_then_selective_activate(db_session, synth_kml: Path):
    load_dpas(db_session, kml_paths=[synth_kml])
    bulk_dpa_activation(db_session, activate=False)
    assert list_active_activations(db_session) == []
    assert list_catalogue(db_session)  # catalogue retained

    ok = activate_dpa(
        db_session,
        {
            "dpaId": "SynthAlpha",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    assert ok["ok"] is True
    assert len(list_active_activations(db_session)) == 1

    # Unknown id rejected (no state change)
    bad = activate_dpa(
        db_session,
        {
            "dpaId": "DoesNotExist",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    assert bad["ok"] is False
    assert bad["reason"] == "unknown_dpaId"
    assert len(list_active_activations(db_session)) == 1

    # Channel outside DPA band / not an exact catalogue channel rejected
    oob = activate_dpa(
        db_session,
        {
            "dpaId": "SynthAlpha",
            "frequencyRange": {
                "lowFrequency": 3_650_000_000,
                "highFrequency": 3_660_000_000,
            },
        },
    )
    assert oob["ok"] is False
    assert oob["reason"] == "channel_not_in_catalogue"

    # Non-exact (20 MHz) range rejected even if inside declared band
    wide = activate_dpa(
        db_session,
        {
            "dpaId": "SynthAlpha",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_570_000_000,
            },
        },
    )
    assert wide["ok"] is False
    assert wide["reason"] == "channel_not_in_catalogue"


def test_selective_deactivation_preserves_other_activations(
    db_session, synth_kml: Path
):
    load_dpas(db_session, kml_paths=[synth_kml])
    bulk_dpa_activation(db_session, activate=False)
    activate_dpa(
        db_session,
        {
            "dpaId": "SynthAlpha",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    activate_dpa(
        db_session,
        {
            "dpaId": "SynthBeta",
            "frequencyRange": {
                "lowFrequency": 3_560_000_000,
                "highFrequency": 3_570_000_000,
            },
        },
    )
    deactivate_dpa(
        db_session,
        {
            "dpaId": "SynthAlpha",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    remaining = list_active_activations(db_session)
    assert len(remaining) == 1
    assert remaining[0]["dpaId"] == "SynthBeta"


def test_bulk_activate_reloads_all_channels(db_session, synth_kml: Path):
    load_dpas(db_session, kml_paths=[synth_kml])
    bulk_dpa_activation(db_session, activate=False)
    result = bulk_dpa_activation(db_session, activate=True)
    assert result["activate"] is True
    assert result["activations"] == 4


def test_bulk_missing_activate_does_not_mutate(db_session, synth_kml: Path):
    load_dpas(db_session, kml_paths=[synth_kml])
    before = len(list_active_activations(db_session))
    result = bulk_dpa_activation(db_session, activate=None)
    assert result["ok"] is False
    assert result["reason"] == "activate_required"
    assert len(list_active_activations(db_session)) == before


def test_resolve_empty_explicit_paths_does_not_fallback():
    from services.dpa_service import resolve_dpa_kml_paths

    assert resolve_dpa_kml_paths([]) == []


def test_reset_dpa_state_clears_catalogue_activations_audit(
    db_session, synth_kml: Path
):
    load_dpas(db_session, kml_paths=[synth_kml])
    reset_dpa_state(db_session)
    assert list_catalogue(db_session) == []
    assert list_active_activations(db_session) == []
    assert (
        db_session.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).count() == 0
    )
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_AUDIT).count() == 0


def test_admin_load_dpas_http_empty_200(db_session, synth_kml: Path, monkeypatch):
    monkeypatch.setenv("SAS_DPA_KML_PATHS", str(synth_kml))
    # Ensure admin mTLS bypass / test client works like other admin tests.
    resp = client.post("/admin/trigger/load_dpas")
    assert resp.status_code == 200
    assert resp.content == b""
    assert len(list_catalogue(db_session)) == 2
    assert len(list_active_activations(db_session)) == 4


def test_admin_bulk_and_selective_http(db_session, synth_kml: Path, monkeypatch):
    monkeypatch.setenv("SAS_DPA_KML_PATHS", str(synth_kml))
    assert client.post("/admin/trigger/load_dpas").status_code == 200
    assert (
        client.post(
            "/admin/trigger/bulk_dpa_activation", json={"activate": False}
        ).status_code
        == 200
    )
    assert list_active_activations(db_session) == []
    assert (
        client.post(
            "/admin/trigger/dpa_activation",
            json={
                "dpaId": "SynthAlpha",
                "frequencyRange": {
                    "lowFrequency": 3_550_000_000,
                    "highFrequency": 3_560_000_000,
                },
            },
        ).status_code
        == 200
    )
    assert len(list_active_activations(db_session)) == 1
    assert (
        client.post(
            "/admin/trigger/dpa_deactivation",
            json={
                "dpaId": "SynthAlpha",
                "frequencyRange": {
                    "lowFrequency": 3_550_000_000,
                    "highFrequency": 3_560_000_000,
                },
            },
        ).status_code
        == 200
    )
    assert list_active_activations(db_session) == []


def test_load_dpas_missing_catalogue_raises(db_session, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SAS_DPA_KML_PATHS", str(tmp_path / "missing.kml"))
    # Also hide default resolution by pointing env only.
    with pytest.raises(FileNotFoundError):
        load_dpas(db_session, kml_paths=[])
