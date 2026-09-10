"""CPAS production IAP wiring (ProtectionPoints + coupling, no test kwargs)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from models.models import Cbsd, EscSensor, Grant
from services.cpas_service import (
    CpasRfEvaluationError,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.data_injection_service import upsert_fss_record
from services.iap import dbm_to_mw
from services.iap.models import ProtectedEntityKind, ProtectionPoint
from services.lifecycle import GrantState
from services.propagation.errors import PropagationUnavailableError


def _constant_coupling(mw_per_mw_eirp: float):
    def coupling(grant, point, channel, eirp_dbm_mhz):
        return dbm_to_mw(eirp_dbm_mhz) * mw_per_mw_eirp

    return coupling


def _add_grant(
    db: Session,
    *,
    cbsd_id: str,
    grant_id: str,
    lat: float,
    lon: float,
    eirp: float = 30.0,
    low_hz: int = 3_620_000_000,
    high_hz: int = 3_625_000_000,
) -> Grant:
    cbsd = Cbsd(
        cbsd_id=cbsd_id,
        fcc_id="fcc-c2",
        user_id="user-c2",
        cbsd_serial_number=f"sn-{cbsd_id}",
        cbsd_category="A",
        registration_json=json.dumps(
            {
                "cbsdCategory": "A",
                "installationParam": {
                    "latitude": lat,
                    "longitude": lon,
                    "height": 6.0,
                    "heightType": "AGL",
                    "indoorDeployment": False,
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


def _inject_fss(db: Session, *, lat: float = 39.0, lon: float = -77.0, fss_id: str = "fss/c2-1") -> None:
    # Passband to CBRS high → co-channel IAP on overlapping CBRS segment.
    assert upsert_fss_record(
        db,
        {
            "record": {
                "id": fss_id,
                "type": "FSS",
                "deploymentParam": [
                    {
                        "installationParam": {
                            "latitude": lat,
                            "longitude": lon,
                            "height": 1.5,
                            "heightType": "AGL",
                        },
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_600_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            },
            "ttc": False,
        },
    )


def test_a_no_iap_entities_skips_iap(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from services.iap import coupling as coupling_mod

    probed = {"n": 0}

    def _boom(*_a, **_k):
        probed["n"] += 1
        raise AssertionError("no coupling without entities")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    _add_grant(db_session, cbsd_id="c2-a", grant_id="g-c2-a", lat=39.1, lon=-77.1)
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snap)
    assert probed["n"] == 0
    assert all(d.reason != "iap" for d in decisions)


def test_b_c_production_path_runs_iap_without_kwargs(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    grant = _add_grant(
        db_session, cbsd_id="c2-b", grant_id="g-c2-b", lat=39.001, lon=-77.001, eirp=37.0
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    assert any(k == "fss" for k, _rid, _data in snap.protection_records)

    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    # No iap_points / iap_coupling kwargs — production resolve.
    decisions = evaluate_cpas_protections(db_session, snap)
    assert any(d.grant_id == grant.grant_id and d.reason == "iap" for d in decisions)


def test_c_execute_pipeline_uses_production_iap(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    grant = _add_grant(
        db_session, cbsd_id="c2-pipe", grant_id="g-c2-pipe", lat=39.001, lon=-77.001, eirp=37.0
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated or (
        grant.max_eirp is not None and float(grant.max_eirp) < 37.0
    )


def test_d_e_peer_contributes_never_mutated(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from models.models import PeerFadRecord, PeerSas
    from services.iap import coupling as coupling_mod
    from services.iap.peer_fad import peer_grant_rf_id

    _inject_fss(db_session, lat=39.0, lon=-77.0)
    local = _add_grant(
        db_session, cbsd_id="c2-peerloc", grant_id="g-c2-peerloc", lat=39.001, lon=-77.001, eirp=20.0
    )
    peer = PeerSas(certificate_hash="peer-c2", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="peer-cbsd-c2",
            data_json=json.dumps(
                {
                    "id": "peer-cbsd-c2",
                    "installationParam": {
                        "latitude": 39.002,
                        "longitude": -77.002,
                        "height": 6.0,
                        "heightType": "AGL",
                        "indoorDeployment": False,
                    },
                    "grants": [
                        {
                            "id": "peer-g-c2",
                            "terminated": False,
                            "operationParam": {
                                "maxEirp": 37.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": 3_620_000_000,
                                    "highFrequency": 3_625_000_000,
                                },
                            },
                        }
                    ],
                }
            ),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snap)
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-c2")
    assert all(d.grant_id != peer_gid for d in decisions)
    assert all(d.grant_pk is not None for d in decisions)
    apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    row = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="peer-cbsd-c2")
        .one()
    )
    assert "peer-g-c2" in row.data_json
    db_session.refresh(local)
    assert local.grant_id == "g-c2-peerloc"


def test_f_coupling_unavailable_fail_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    _add_grant(db_session, cbsd_id="c2-f", grant_id="g-c2-f", lat=39.0, lon=-77.0)
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)

    def _boom(*_a, **_k):
        raise PropagationUnavailableError("ITM/NED missing")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with pytest.raises(CpasRfEvaluationError, match="coupling unavailable"):
        evaluate_cpas_protections(db_session, snap)


def test_g_itm_required_no_silent_free_space(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from config import clear_settings_cache, get_settings
    from services.iap import coupling as coupling_mod
    from services.propagation.errors import PropagationUnavailableError

    clear_settings_cache()
    monkeypatch.setenv("SAS_IAP_PATH_LOSS_MODEL", "itm")
    clear_settings_cache()
    assert get_settings().sas_iap_path_loss_model == "itm"

    def _boom(*_a, **_k):
        raise PropagationUnavailableError("reference_models missing")

    monkeypatch.setattr(coupling_mod, "load_reference_engines", _boom, raising=False)
    monkeypatch.setattr(
        "services.propagation.engines.load_reference_engines", _boom
    )
    with pytest.raises(PropagationUnavailableError):
        coupling_mod.make_production_iap_coupling(path_loss_model="itm")
    clear_settings_cache()


def test_h_snapshot_n_ignores_n1_protection_and_rf(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session, lat=39.0, lon=-77.0)
    grant = _add_grant(
        db_session, cbsd_id="c2-h", grant_id="g-c2-h", lat=39.001, lon=-77.001, eirp=37.0
    )
    db_session.commit()
    snap_n = freeze_cpas_snapshot(db_session)
    # Mid-run N+1: move CBSD far away + add second FSS (must not affect N).
    cbsd = db_session.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).one()
    cbsd.registration_json = json.dumps(
        {
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 10.0,
                "longitude": 10.0,
                "height": 6.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
        }
    )
    upsert_fss_record(
        db_session,
        {
            "record": {
                "id": "fss/c2-n1",
                "type": "FSS",
                "deploymentParam": [
                    {
                        "installationParam": {
                            "latitude": 10.0,
                            "longitude": 10.0,
                            "height": 1.5,
                            "heightType": "AGL",
                        },
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_620_000_000,
                                "highFrequency": 3_625_000_000,
                            }
                        },
                    }
                ],
            }
        },
    )
    db_session.commit()

    seen_points: list[str] = []
    real_iap = __import__(
        "services.cpas_service", fromlist=["_evaluate_iap_decisions_from_frozen"]
    )._evaluate_iap_decisions_from_frozen

    def _spy(local_grants, **kwargs):
        for p in kwargs.get("iap_points") or []:
            seen_points.append(p.point_id)
        # Frozen RF must still be near original FSS.
        assert local_grants[0].latitude == pytest.approx(39.001)
        return real_iap(local_grants, **kwargs)

    monkeypatch.setattr(
        "services.cpas_service._evaluate_iap_decisions_from_frozen", _spy
    )
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    evaluate_cpas_protections(db_session, snap_n)
    assert "fss-cc:fss/c2-1" in seen_points
    assert all(not p.startswith("fss-") or "c2-n1" not in p for p in seen_points)
    assert "fss-cc:fss/c2-n1" not in seen_points
    assert "fss-bl:fss/c2-n1" not in seen_points

    snap_n1 = freeze_cpas_snapshot(db_session)
    ids_n1 = [p.point_id for p in __import__(
        "services.iap.protection_points", fromlist=["build_protection_points_from_frozen"]
    ).build_protection_points_from_frozen(snap_n1.protection_records)]
    assert "fss-bl:fss/c2-n1" in ids_n1 or "fss-cc:fss/c2-n1" in ids_n1


def test_i_explicit_override_precedence(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    grant = _add_grant(
        db_session, cbsd_id="c2-i", grant_id="g-c2-i", lat=39.001, lon=-77.001, eirp=37.0
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    production_calls = {"n": 0}

    def _prod():
        production_calls["n"] += 1
        return _constant_coupling(1.0)

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _prod)
    override_point = ProtectionPoint(
        point_id="override-esc",
        latitude=39.001,
        longitude=-77.001,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
        threshold_dbm=40.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    decisions = evaluate_cpas_protections(
        db_session,
        snap,
        iap_points=[override_point],
        iap_coupling=_constant_coupling(1e-18),
    )
    assert production_calls["n"] == 0
    # Tiny coupling → keep (no reduce/terminate from IAP).
    assert not any(
        d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep"
        for d in decisions
    )


def test_j_deterministic_order(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    db_session.add(
        EscSensor(
            record_id="esc-c2-j",
            data_json=json.dumps(
                {
                    "installationParam": {
                        "latitude": 39.01,
                        "longitude": -77.01,
                        "height": 3.0,
                        "heightType": "AGL",
                        "antennaAzimuth": 0.0,
                        "azimuthRadiationPattern": [
                            {"angle": i, "gain": 30.0} for i in range(360)
                        ],
                    },
                    "protectionFrequencyRange": {
                        "lowFrequency": 3_620_000_000,
                        "highFrequency": 3_625_000_000,
                    },
                }
            ),
        )
    )
    g1 = _add_grant(db_session, cbsd_id="c2-j1", grant_id="g-c2-j1", lat=39.001, lon=-77.001)
    g2 = _add_grant(db_session, cbsd_id="c2-j2", grant_id="g-c2-j2", lat=39.002, lon=-77.002)
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(0.5),
    )
    snap = freeze_cpas_snapshot(db_session)
    a = evaluate_cpas_protections(db_session, snap)
    b = evaluate_cpas_protections(db_session, snap)
    assert [(d.grant_id, d.action, d.authorized_eirp_dbm_mhz) for d in a] == [
        (d.grant_id, d.action, d.authorized_eirp_dbm_mhz) for d in b
    ]
    del g1, g2


def test_c_iap_disabled_under_cbrs_fails_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """CBRS requires aggregate_linear_power; SAS_IAP_ENABLED=false must not skip IAP."""
    from config import clear_settings_cache
    from runtime.toggle_reconciliation import RequiredCapabilityDisabledError
    from services.iap.coupling import iap_enabled

    monkeypatch.setenv("SAS_IAP_ENABLED", "false")
    clear_settings_cache()
    with pytest.raises(RequiredCapabilityDisabledError):
        iap_enabled()
    monkeypatch.delenv("SAS_IAP_ENABLED", raising=False)
    clear_settings_cache()


def test_c_iap_disabled_skips_when_profile_does_not_require_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Non-requiring profile may disable IAP; CPAS skips without fail-open on CBRS."""
    from config import clear_settings_cache
    from runtime import PluginRegistry, compose_runtime
    from runtime.context import runtime_composition_scope
    from services.iap import coupling as coupling_mod
    from tests.fixtures.synthetic_profile import (
        synthetic_deployment,
        synthetic_profile,
    )

    monkeypatch.setenv("SAS_IAP_ENABLED", "false")
    clear_settings_cache()
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    _inject_fss(db_session)
    _add_grant(db_session, cbsd_id="c2-dis", grant_id="g-c2-dis", lat=39.0, lon=-77.0)
    db_session.commit()
    probed = {"n": 0}

    def _boom(*_a, **_k):
        probed["n"] += 1
        raise AssertionError("disabled")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with runtime_composition_scope(composition):
        snap = freeze_cpas_snapshot(db_session)
        decisions = evaluate_cpas_protections(db_session, snap)
    assert probed["n"] == 0
    assert all(d.reason != "iap" for d in decisions)
    monkeypatch.delenv("SAS_IAP_ENABLED", raising=False)
    clear_settings_cache()


def test_pipeline_rollback_when_iap_coupling_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    grant = _add_grant(
        db_session, cbsd_id="c2-rb", grant_id="g-c2-rb", lat=39.0, lon=-77.0, eirp=37.0
    )
    db_session.commit()

    def _boom(*_a, **_k):
        raise PropagationUnavailableError("no itm")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with pytest.raises(CpasRfEvaluationError):
        execute_cpas_pipeline(db_session)
    db_session.refresh(grant)
    assert grant.terminated is False
    assert grant.max_eirp == pytest.approx(37.0)
