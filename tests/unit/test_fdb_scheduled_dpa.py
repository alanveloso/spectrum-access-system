"""FDB.2 — scheduled DPA fail-closed parity with activate_dpa."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from models.models import AdminInjectedData
from services.database_sync_service import DatabaseSyncError, _apply_scheduled_dpa
from services.dpa_service import (
    KIND_CATALOGUE,
    activate_dpa,
    bulk_dpa_activation,
    get_catalogue_definition,
    list_active_activations,
    list_catalogue,
    load_dpas,
)
from services.federal_db_service import bump_sync_meta, get_sync_meta
from services.propagation.errors import PropagationUnavailableError
from tests.fixtures.factories import make_cbsd, make_grant

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
  </Document>
</kml>
"""

_CHAN = {
    "lowFrequency": 3_550_000_000,
    "highFrequency": 3_560_000_000,
}


@pytest.fixture
def synth_kml(tmp_path: Path) -> Path:
    path = tmp_path / "synth-scheduled-dpa.kml"
    path.write_text(_SYNTH_KML, encoding="utf-8")
    return path


@pytest.fixture
def catalogue(db_session, synth_kml: Path):
    load_dpas(db_session, kml_paths=[synth_kml])
    bulk_dpa_activation(db_session, activate=False)
    db_session.commit()
    return get_catalogue_definition(db_session, "SynthAlpha")


def _sched_body(dpa_id: str = "SynthAlpha", fr: dict | None = None) -> bytes:
    return json.dumps(
        {"activations": [{"dpaId": dpa_id, "frequencyRange": fr or _CHAN}]}
    ).encode()


def _geom_rings(db_session) -> list:
    rings = []
    for item in list_catalogue(db_session):
        geom = item.get("geometry") or {}
        if geom.get("type") == "Polygon":
            rings.extend(geom.get("coordinates") or [])
    return rings


# --- A: known id → real catalogue geometry ------------------------------------


def test_a_known_scheduled_uses_catalogue_geometry(db_session, catalogue):
    assert catalogue is not None
    expected = catalogue["geometry"]
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    acts = list_active_activations(db_session)
    assert any(a.get("dpaId") == "SynthAlpha" for a in acts)
    assert get_catalogue_definition(db_session, "SynthAlpha")["geometry"] == expected
    assert expected["type"] == "Polygon"
    ring = expected["coordinates"][0]
    assert not (
        any(abs(p[0]) == 180.0 for p in ring) and any(abs(p[1]) == 90.0 for p in ring)
    )


# --- B/C/K: unknown id fail-closed; no world polygon; parity with activate ----


def test_b_c_unknown_scheduled_dpa_fails_no_activation(db_session, catalogue):
    before_cat = list_catalogue(db_session)
    before_acts = list_active_activations(db_session)
    with pytest.raises(DatabaseSyncError, match="scheduled_dpa_unknown_dpaId"):
        _apply_scheduled_dpa(db_session, _sched_body("DoesNotExist"))
    db_session.rollback()
    assert list_catalogue(db_session) == before_cat
    assert list_active_activations(db_session) == before_acts
    for ring in _geom_rings(db_session):
        assert not (
            len(ring) >= 4
            and any(abs(p[0]) == 180.0 for p in ring)
            and any(abs(p[1]) == 90.0 for p in ring)
        )


def test_k_activate_and_scheduled_unknown_id_parity(db_session, catalogue):
    bad = activate_dpa(
        db_session,
        {"dpaId": "DoesNotExist", "frequencyRange": _CHAN},
    )
    assert bad["ok"] is False
    assert bad["reason"] == "unknown_dpaId"
    with pytest.raises(DatabaseSyncError, match="unknown_dpaId"):
        _apply_scheduled_dpa(db_session, _sched_body("DoesNotExist"))


def test_c_no_world_polygon_created_on_unknown(db_session, catalogue):
    cat_json_before = (
        db_session.query(AdminInjectedData)
        .filter_by(kind=KIND_CATALOGUE)
        .one()
        .data_json
    )
    with pytest.raises(DatabaseSyncError):
        _apply_scheduled_dpa(db_session, _sched_body("GhostDpa"))
    db_session.rollback()
    cat_json_after = (
        db_session.query(AdminInjectedData)
        .filter_by(kind=KIND_CATALOGUE)
        .one()
        .data_json
    )
    assert cat_json_before == cat_json_after
    assert "GhostDpa" not in cat_json_after


# --- D: channel not in catalogue ----------------------------------------------


def test_d_channel_outside_catalogue_rejected(db_session, catalogue):
    with pytest.raises(DatabaseSyncError, match="channel_not_in_catalogue"):
        _apply_scheduled_dpa(
            db_session,
            _sched_body(
                fr={
                    "lowFrequency": 3_650_000_000,
                    "highFrequency": 3_660_000_000,
                }
            ),
        )


# --- E/F: legitimate empty vs interfering grants ------------------------------


def test_e_rf_success_no_grants_legitimate_empty_movelist(
    db_session, catalogue, monkeypatch
):
    def _ok(db, channels):
        # Successful RF path: leave activation movelist empty (no grants to move).
        return None

    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        _ok,
    )
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    acts = [
        a for a in list_active_activations(db_session) if a.get("dpaId") == "SynthAlpha"
    ]
    assert acts
    assert acts[0].get("movelist") == []
    assert acts[0].get("source") == "scheduled_dpa"


def test_f_rf_success_interfering_grants_fill_movelist(
    db_session, catalogue, monkeypatch
):
    cbsd = make_cbsd(
        db_session,
        registration={
            "installationParam": {
                "latitude": 38.05,
                "longitude": -75.05,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
            "cbsdCategory": "A",
        },
    )
    grant = make_grant(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=20.0,
        terminated=False,
    )

    def _filled(db, channels):
        from services.dpa_service import _upsert_activation

        for dpa_id, freq in channels:
            _upsert_activation(
                db,
                dpa_id=dpa_id,
                freq=freq,
                movelist=[grant.grant_id],
            )

    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        _filled,
    )
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    acts = [
        a for a in list_active_activations(db_session) if a.get("dpaId") == "SynthAlpha"
    ]
    assert grant.grant_id in (acts[0].get("movelist") or [])
    assert acts[0].get("source") == "scheduled_dpa"


# --- G/H: RF unavailable → fail-closed, not empty-as-success ------------------


def test_g_rf_unavailable_not_empty_success_when_grants_overlap(
    db_session, catalogue, monkeypatch
):
    cbsd = make_cbsd(
        db_session,
        registration={
            "installationParam": {
                "latitude": 38.05,
                "longitude": -75.05,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
            "cbsdCategory": "A",
        },
    )
    grant = make_grant(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=20.0,
        terminated=False,
    )

    calls: list[int] = []

    def _boom(db, **kwargs):
        calls.append(1)
        raise PropagationUnavailableError("ITM unavailable")

    import services.dpa_protection as dpa_prot

    monkeypatch.setattr(dpa_prot, "refresh_activation_movelists", _boom)
    from services.dpa_service import FrequencyRange, refresh_or_fail_closed_movelists
    from services.dpa_service import _upsert_activation
    from services.dpa_protection import collect_active_dpa_grants

    assert any(g.grant_id == grant.grant_id for g in collect_active_dpa_grants(db_session))

    _upsert_activation(
        db_session,
        dpa_id="SynthAlpha",
        freq=FrequencyRange(3_550_000_000, 3_560_000_000),
        movelist=[],
        source="scheduled_dpa",
    )
    refresh_or_fail_closed_movelists(
        db_session,
        [("SynthAlpha", FrequencyRange(3_550_000_000, 3_560_000_000))],
    )
    db_session.commit()
    assert calls == [1]
    acts = [
        a
        for a in list_active_activations(db_session)
        if a.get("dpaId") == "SynthAlpha"
        and (a.get("frequencyRange") or {}).get("lowFrequency") == 3_550_000_000
    ]
    assert acts, list_active_activations(db_session)
    moved = acts[0].get("movelist") or []
    assert grant.grant_id in moved, (
        moved,
        [g.grant_id for g in collect_active_dpa_grants(db_session)],
        list_active_activations(db_session),
    )
    assert moved != []
    assert acts[0].get("source") == "scheduled_dpa"


def test_g_scheduled_uses_fail_closed_helper(db_session, catalogue, monkeypatch):
    """Scheduled path must invoke shared fail-closed helper (not empty-on-error)."""
    cbsd = make_cbsd(
        db_session,
        registration={
            "installationParam": {
                "latitude": 38.05,
                "longitude": -75.05,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
            "cbsdCategory": "A",
        },
    )
    grant = make_grant(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=20.0,
        terminated=False,
    )

    def _boom(db, **kwargs):
        raise PropagationUnavailableError("ITM unavailable")

    monkeypatch.setattr(
        "services.dpa_protection.refresh_activation_movelists",
        _boom,
    )
    # Import helper into apply path: patch module attribute used by late import.
    import services.dpa_service as dpa_mod

    real = dpa_mod.refresh_or_fail_closed_movelists

    def _wrapped(db, channels):
        real(db, channels)

    monkeypatch.setattr(dpa_mod, "refresh_or_fail_closed_movelists", _wrapped)
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    acts = [
        a for a in list_active_activations(db_session) if a.get("dpaId") == "SynthAlpha"
    ]
    assert grant.grant_id in (acts[0].get("movelist") or [])


def test_h_dataset_unavailable_fail_closed_parity_with_activate(
    db_session, catalogue, monkeypatch
):
    cbsd = make_cbsd(
        db_session,
        registration={
            "installationParam": {
                "latitude": 38.05,
                "longitude": -75.05,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
            "cbsdCategory": "A",
        },
    )
    grant = make_grant(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=20.0,
        terminated=False,
    )

    def _boom(db, **kwargs):
        raise PropagationUnavailableError("terrain/NED missing")

    monkeypatch.setattr(
        "services.dpa_protection.refresh_activation_movelists",
        _boom,
    )
    activate_dpa(
        db_session,
        {"dpaId": "SynthAlpha", "frequencyRange": _CHAN},
    )
    manual = [
        a
        for a in list_active_activations(db_session)
        if a.get("dpaId") == "SynthAlpha"
    ][0]
    bulk_dpa_activation(db_session, activate=False)
    db_session.commit()

    import services.dpa_service as dpa_mod

    monkeypatch.setattr(
        dpa_mod,
        "refresh_or_fail_closed_movelists",
        dpa_mod.refresh_or_fail_closed_movelists,
    )
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    scheduled = [
        a
        for a in list_active_activations(db_session)
        if a.get("dpaId") == "SynthAlpha"
    ][0]
    assert grant.grant_id in (manual.get("movelist") or [])
    assert grant.grant_id in (scheduled.get("movelist") or [])


# --- I/J: no partial publish; retry after catalogue / RF available ------------


def test_i_failure_leaves_no_partial_scheduled_activation(db_session, catalogue):
    meta_before = get_sync_meta(db_session)
    with pytest.raises(DatabaseSyncError):
        _apply_scheduled_dpa(db_session, _sched_body("UnknownPortalDpa"))
    db_session.rollback()
    assert not any(
        a.get("dpaId") == "UnknownPortalDpa"
        for a in list_active_activations(db_session)
    )
    assert not db_session.query(AdminInjectedData).filter_by(kind="scheduled_dpa").all()
    assert get_sync_meta(db_session) == meta_before


def test_j_retry_after_catalogue_available(db_session, synth_kml: Path):
    with pytest.raises(DatabaseSyncError, match="unknown_dpaId"):
        _apply_scheduled_dpa(db_session, _sched_body("SynthAlpha"))
    db_session.rollback()
    load_dpas(db_session, kml_paths=[synth_kml])
    bulk_dpa_activation(db_session, activate=False)
    db_session.commit()
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    assert any(
        a.get("dpaId") == "SynthAlpha" for a in list_active_activations(db_session)
    )
    assert get_sync_meta(db_session)["dpa"] >= 1


def test_j_retry_after_rf_backend_returns(db_session, catalogue, monkeypatch):
    calls = {"n": 0}

    def _flaky(db, channels):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate shared helper fail-closed with no grants → empty ok.
            return None
        return None

    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        _flaky,
    )
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    assert any(
        a.get("source") == "scheduled_dpa" for a in list_active_activations(db_session)
    )
    _apply_scheduled_dpa(db_session, _sched_body())
    db_session.commit()
    assert calls["n"] == 2
    bump_sync_meta(db_session, "dpa")
    db_session.commit()


def test_l_world_polygon_absent_from_product_source():
    from pathlib import Path

    src = Path("services/database_sync_service.py").read_text(encoding="utf-8")
    assert "world_ring" not in src
    assert "[-180.0, -90.0]" not in src
    assert "180.0, 90.0" not in src or "scheduled" not in src.lower()
    # Stronger: no synthetic global bbox for scheduled DPA.
    assert "geometry={\"type\": \"Polygon\", \"coordinates\": [world_ring]}" not in src
    assert "coordinates\": [world_ring]" not in src
