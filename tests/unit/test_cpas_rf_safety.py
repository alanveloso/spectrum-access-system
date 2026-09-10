"""CPAS-DPA fail-closed + frozen RF snapshot N/N+1 consistency."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from models.models import Cbsd, Grant
from services.cpas_service import (
    CpasRfEvaluationError,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.dpa_protection import DpaPathLossModel, make_path_loss_fn
from services.dpa_service import activate_dpa, clear_activations, load_dpas
from services.lifecycle import GrantState
from services.propagation.errors import PropagationUnavailableError


_SYNTH_DPA_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>C1Offshore</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3560</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>50</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>80</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
        <Data name="refHeightMeters"><value>50</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -75.05,38.05,0 -75.0,38.05,0 -75.0,38.0,0 -75.05,38.0,0 -75.05,38.05,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


@pytest.fixture
def synth_dpa_kml(tmp_path: Path) -> Path:
    path = tmp_path / "c1_dpa.kml"
    path.write_text(_SYNTH_DPA_KML, encoding="utf-8")
    return path


def _stub_itm_fn(itm_db: float = 100.0):
    def _itm(grant, lat_rx, lon_rx, height_rx):
        return float(itm_db)

    return make_path_loss_fn(model=DpaPathLossModel.ITM_REL1EXT, itm_median_fn=_itm)


def _add_grant(
    db: Session,
    *,
    cbsd_id: str,
    grant_id: str,
    lat: float,
    lon: float,
    height: float = 4.0,
    indoor: bool = False,
    category: str = "A",
    eirp: float = 30.0,
    low_hz: int = 3_550_000_000,
    high_hz: int = 3_560_000_000,
) -> Grant:
    cbsd = Cbsd(
        cbsd_id=cbsd_id,
        fcc_id="fcc-c1",
        user_id="user-c1",
        cbsd_serial_number=f"sn-{cbsd_id}",
        cbsd_category=category,
        registration_json=json.dumps(
            {
                "cbsdCategory": category,
                "installationParam": {
                    "latitude": lat,
                    "longitude": lon,
                    "height": height,
                    "heightType": "AGL",
                    "indoorDeployment": indoor,
                },
            }
        ),
    )
    db.add(cbsd)
    db.flush()
    grant = Grant(
        grant_id=grant_id,
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd_id,
        channel_type="GAA",
        low_frequency=low_hz,
        high_frequency=high_hz,
        max_eirp=eirp,
        grant_expire_time=datetime.now(timezone.utc) + timedelta(days=1),
        lifecycle_state=GrantState.GRANTED.value,
        terminated=False,
    )
    db.add(grant)
    db.flush()
    return grant


def _activate(db: Session, kml: Path) -> None:
    load_dpas(db, kml_paths=[kml])
    clear_activations(db)
    activate_dpa(
        db,
        {
            "dpaId": "C1Offshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )


def test_dpa_propagation_normal_terminates_overlapping(
    db_session: Session, synth_dpa_kml: Path
):
    _activate(db_session, synth_dpa_kml)
    grant = _add_grant(
        db_session,
        cbsd_id="cbsd-c1-ok",
        grant_id="grant-c1-ok",
        lat=38.02,
        lon=-75.02,
        eirp=37.0,
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(
        db_session, snap, path_loss_fn=_stub_itm_fn(80.0)
    )
    assert any(
        d.grant_id == grant.grant_id and d.reason == "dpa_movelist" for d in decisions
    )


def test_dpa_propagation_exception_raises_not_silent_skip(
    db_session: Session, synth_dpa_kml: Path, monkeypatch: pytest.MonkeyPatch
):
    from services import dpa_protection as dpa_mod

    _activate(db_session, synth_dpa_kml)
    _add_grant(
        db_session,
        cbsd_id="cbsd-c1-err",
        grant_id="grant-c1-err",
        lat=38.02,
        lon=-75.02,
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)

    def _boom(*_a, **_k):
        raise PropagationUnavailableError("ITM backend exploded")

    monkeypatch.setattr(dpa_mod, "refresh_activation_movelists", _boom)
    with pytest.raises(CpasRfEvaluationError, match="DPA RF evaluation unavailable"):
        evaluate_cpas_protections(db_session, snap, path_loss_fn=_stub_itm_fn(80.0))


def test_pipeline_rollback_on_dpa_rf_error(
    db_session: Session, synth_dpa_kml: Path, monkeypatch: pytest.MonkeyPatch
):
    from services import dpa_protection as dpa_mod
    from services.dpa_service import list_active_activations

    _activate(db_session, synth_dpa_kml)
    grant = _add_grant(
        db_session,
        cbsd_id="cbsd-c1-rb",
        grant_id="grant-c1-rb",
        lat=38.02,
        lon=-75.02,
        eirp=37.0,
    )
    db_session.commit()
    assert grant.terminated is False

    def _boom(*_a, **_k):
        raise PropagationUnavailableError("NED missing")

    monkeypatch.setattr(dpa_mod, "refresh_activation_movelists", _boom)
    with pytest.raises(CpasRfEvaluationError):
        execute_cpas_pipeline(db_session)

    db_session.refresh(grant)
    assert grant.terminated is False
    # Activations must not look like a successful empty-protect evaluation.
    for act in list_active_activations(db_session):
        # Pre-error state: activate_dpa may have empty movelist; must not claim
        # success via terminated grants.
        assert grant.grant_id not in (act.get("movelist") or [])


def test_refresh_activation_movelists_pending_atomic(
    db_session: Session, synth_dpa_kml: Path, monkeypatch: pytest.MonkeyPatch
):
    """All channels evaluate before any upsert; mid-loop RF error leaves no writes."""
    from services import dpa_protection as dpa_mod
    from services.dpa_protection import (
        DpaGrantRf,
        ProtectedDpaChannel,
        ProtectionReason,
        refresh_activation_movelists,
    )
    from services.dpa_service import list_active_activations

    _activate(db_session, synth_dpa_kml)
    geom = {
        "type": "Polygon",
        "coordinates": [
            [
                [-75.05, 38.05],
                [-75.0, 38.05],
                [-75.0, 38.0],
                [-75.05, 38.0],
                [-75.05, 38.05],
            ]
        ],
    }

    def _channels(_db):
        return [
            ProtectedDpaChannel(
                dpa_id="C1Offshore",
                low_hz=3_550_000_000,
                high_hz=3_560_000_000,
                reason=ProtectionReason.ACTIVE,
                geometry=geom,
                neighborhood_km={"catA_Outdoor": 50.0},
                protection_params={},
                threshold_dbm_per_10mhz=-144.0,
                ref_height_m=50.0,
            ),
            ProtectedDpaChannel(
                dpa_id="C1Offshore",
                low_hz=3_560_000_000,
                high_hz=3_570_000_000,
                reason=ProtectionReason.ACTIVE,
                geometry=geom,
                neighborhood_km={"catA_Outdoor": 50.0},
                protection_params={},
                threshold_dbm_per_10mhz=-144.0,
                ref_height_m=50.0,
            ),
        ]

    def _acts(_db):
        return [
            {
                "dpaId": "C1Offshore",
                "frequencyRange": {
                    "lowFrequency": 3_550_000_000,
                    "highFrequency": 3_560_000_000,
                },
                "movelist": [],
            },
            {
                "dpaId": "C1Offshore",
                "frequencyRange": {
                    "lowFrequency": 3_560_000_000,
                    "highFrequency": 3_570_000_000,
                },
                "movelist": [],
            },
        ]

    monkeypatch.setattr(dpa_mod, "list_protected_dpa_channels", _channels)
    monkeypatch.setattr(
        "services.dpa_service.list_active_activations", _acts
    )
    real_eval = dpa_mod.evaluate_protected_channel
    calls = {"n": 0}

    def _eval_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise PropagationUnavailableError("second channel RF fail")
        return real_eval(*args, **kwargs)

    monkeypatch.setattr(dpa_mod, "evaluate_protected_channel", _eval_once)
    upserts: list[str] = []
    real_upsert = __import__(
        "services.dpa_service", fromlist=["_upsert_activation"]
    )._upsert_activation

    def _spy_upsert(db, *, dpa_id, freq, movelist):
        upserts.append(dpa_id)
        return real_upsert(db, dpa_id=dpa_id, freq=freq, movelist=movelist)

    monkeypatch.setattr("services.dpa_service._upsert_activation", _spy_upsert)
    local = [
        DpaGrantRf(
            grant_id="grant-atomic",
            cbsd_id="cbsd-atomic",
            latitude=38.02,
            longitude=-75.02,
            height_m=4.0,
            height_is_agl=True,
            indoor=False,
            low_hz=3_550_000_000,
            high_hz=3_570_000_000,
            max_eirp_dbm_mhz=37.0,
        )
    ]
    with pytest.raises(PropagationUnavailableError):
        refresh_activation_movelists(
            db_session,
            local_grants=local,
            path_loss_fn=_stub_itm_fn(80.0),
            commit=False,
        )
    assert calls["n"] == 2
    assert upserts == []
    for act in list_active_activations(db_session):
        assert "grant-atomic" not in (act.get("movelist") or [])


def test_rf_snapshot_ignores_live_registration_and_eirp_mutations(
    db_session: Session, synth_dpa_kml: Path
):
    _activate(db_session, synth_dpa_kml)
    grant = _add_grant(
        db_session,
        cbsd_id="cbsd-c1-nn1",
        grant_id="grant-c1-nn1",
        lat=38.02,
        lon=-75.02,
        height=4.0,
        category="A",
        indoor=False,
        eirp=10.0,
    )
    db_session.commit()
    snap_n = freeze_cpas_snapshot(db_session)
    assert len(snap_n.local_grants) == 1
    frozen = snap_n.local_grants[0]
    assert frozen.latitude == pytest.approx(38.02)
    assert frozen.max_eirp_dbm_mhz == pytest.approx(10.0)

    cbsd = db_session.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).one()
    cbsd.registration_json = json.dumps(
        {
            "cbsdCategory": "B",
            "installationParam": {
                "latitude": 39.5,
                "longitude": -76.5,
                "height": 30.0,
                "heightType": "AGL",
                "indoorDeployment": True,
            },
        }
    )
    cbsd.cbsd_category = "B"
    grant.max_eirp = 37.0
    db_session.commit()

    # Path loss uses frozen coords near DPA; live coords are far outside.
    decisions_n = evaluate_cpas_protections(
        db_session, snap_n, path_loss_fn=_stub_itm_fn(80.0)
    )
    assert any(d.grant_id == grant.grant_id for d in decisions_n)
    used = snap_n.local_grants[0]
    assert used.latitude == pytest.approx(38.02)
    assert used.height_m == pytest.approx(4.0)
    assert used.cbsd_category == "A"
    assert used.indoor is False
    assert used.max_eirp_dbm_mhz == pytest.approx(10.0)

    snap_n1 = freeze_cpas_snapshot(db_session)
    assert snap_n1.local_grants[0].latitude == pytest.approx(39.5)
    assert snap_n1.local_grants[0].height_m == pytest.approx(30.0)
    assert snap_n1.local_grants[0].cbsd_category == "B"
    assert snap_n1.local_grants[0].indoor is True
    assert snap_n1.local_grants[0].max_eirp_dbm_mhz == pytest.approx(37.0)


def test_apply_still_targets_live_rows_after_frozen_evaluate(
    db_session: Session, synth_dpa_kml: Path
):
    _activate(db_session, synth_dpa_kml)
    grant = _add_grant(
        db_session,
        cbsd_id="cbsd-c1-apply",
        grant_id="grant-c1-apply",
        lat=38.02,
        lon=-75.02,
        eirp=37.0,
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(
        db_session, snap, path_loss_fn=_stub_itm_fn(80.0)
    )
    assert decisions
    changed = apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    assert changed >= 1
    db_session.refresh(grant)
    assert grant.terminated is True
