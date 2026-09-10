""" — Coordination Core invariants (pre-extraction contracts).

Named invariants that extraction (G2/G5) must preserve. Builds on  seam
characterization; does not change product code.

Invariant IDs (stable for regression naming):
  INV-SNAP-01  snapshot membership is frozen at freeze time
  INV-SNAP-02  local grant RF captured in snapshot is immutable / generation-N
  INV-EVAL-01  evaluate does not mutate durable grant state
  INV-EVAL-02  evaluate is deterministic on the same snapshot
  INV-APPLY-01 apply is the writer; peer/missing-pk/unknown actions are no-ops
  INV-APPLY-02 apply action vocabulary is closed (reduce_power|suspend|terminate)
  INV-FAIL-01  required RF coupling failure fails closed (no silent skip)
  INV-FAIL-02  required protection-data payload gap fails closed in strict mode
  INV-AUDIT-01 successful pipeline records cpas_completed with dump/stages
  INV-AUDIT-02 failed pipeline rolls back grant writes and records cpas_failed
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from models.models import AdminInjectedData, Cbsd, Grant, PeerFadRecord, PeerSas
from protection_data.loader import (
    DatasetValidationError,
    assert_protection_data_ready,
    clear_dataset_bundle_cache,
    set_data_root,
    set_manifests_dir,
    validate_dataset_bundle,
)
from services.cpas_service import (
    KIND_CPAS_AUDIT,
    CpasDecision,
    CpasRfEvaluationError,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.data_injection_service import upsert_fss_record
from services.fad_service import fad_cbsd_id, get_published_dump
from services.propagation.errors import PropagationUnavailableError
from tests.unit.test_protection_data import _seed_markers, _write_bundle


def _add_cbsd_with_grant(
    db: Session,
    *,
    fcc: str,
    serial: str,
    grant_id: str,
    eirp: float = 20.0,
    lat: float = 39.0,
    lon: float = -100.0,
) -> tuple[Cbsd, Grant]:
    cbsd = Cbsd(
        cbsd_id=f"{fcc}/{serial}",
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id="coordination-user",
        registration_json=json.dumps(
            {
                "fccId": fcc,
                "cbsdSerialNumber": serial,
                "cbsdCategory": "A",
                "airInterface": {"radioTechnology": "E_UTRA"},
                "measCapability": [],
                "installationParam": {
                    "latitude": lat,
                    "longitude": lon,
                    "height": 10,
                    "heightType": "AGL",
                },
            }
        ),
    )
    db.add(cbsd)
    db.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id=grant_id,
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        low_frequency=3550000000,
        high_frequency=3560000000,
        max_eirp=eirp,
        channel_type="GAA",
        grant_expire_time=expire.replace(tzinfo=None),
        terminated=False,
        grant_json="{}",
    )
    db.add(grant)
    db.flush()
    return cbsd, grant


def _seed_peer_conflict(db: Session, peer: PeerSas, cbsd: Cbsd) -> None:
    db.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id=fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number),
            data_json=json.dumps(
                {
                    "id": fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number),
                    "grants": [{"id": "peer-g", "terminated": False}],
                }
            ),
        )
    )


def _audit_events(db: Session) -> list[dict]:
    rows = db.query(AdminInjectedData).filter_by(kind=KIND_CPAS_AUDIT).all()
    return [json.loads(r.data_json) for r in rows]


# --- INV-SNAP -----------------------------------------------------------------


def test_inv_snap_01_membership_frozen_at_freeze_time(db_session: Session):
    """INV-SNAP-01: post-freeze grants are invisible to evaluate on that snapshot."""
    peer = PeerSas(certificate_hash="peer-snap01", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    c1, g1 = _add_cbsd_with_grant(
        db_session, fcc="fcc-s1a", serial="sn-s1a", grant_id="G-S1A"
    )
    _seed_peer_conflict(db_session, peer, c1)
    snapshot = freeze_cpas_snapshot(db_session)

    c2, g2 = _add_cbsd_with_grant(
        db_session, fcc="fcc-s1b", serial="sn-s1b", grant_id="G-S1B"
    )
    _seed_peer_conflict(db_session, peer, c2)
    db_session.commit()

    assert g1.id in snapshot.active_grant_pks
    assert g2.id not in snapshot.active_grant_pks
    decisions = evaluate_cpas_protections(db_session, snapshot)
    pks = {d.grant_pk for d in decisions}
    assert g1.id in pks
    assert g2.id not in pks


def test_inv_snap_02_local_rf_is_generation_n(db_session: Session):
    """INV-SNAP-02: frozen local RF ignores mid-run ORM mutations."""
    cbsd, grant = _add_cbsd_with_grant(
        db_session,
        fcc="fcc-s2",
        serial="sn-s2",
        grant_id="G-S2",
        eirp=27.0,
        lat=39.1,
        lon=-100.1,
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    frozen = next(g for g in snap.local_grants if g.grant_pk == grant.id)
    assert frozen.max_eirp_dbm_mhz == 27.0
    assert frozen.latitude == pytest.approx(39.1)
    assert frozen.longitude == pytest.approx(-100.1)

    grant.max_eirp = 5.0
    cbsd.registration_json = json.dumps(
        {
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 10.0,
                "longitude": 10.0,
                "height": 6.0,
                "heightType": "AGL",
            },
        }
    )
    db_session.commit()

    still = next(g for g in snap.local_grants if g.grant_pk == grant.id)
    assert still.max_eirp_dbm_mhz == 27.0
    assert still.latitude == pytest.approx(39.1)
    assert still.longitude == pytest.approx(-100.1)


# --- INV-EVAL -----------------------------------------------------------------


def test_inv_eval_01_evaluate_is_read_only(db_session: Session):
    """INV-EVAL-01: evaluate emits decisions without durable grant mutation."""
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-e1", serial="sn-e1", grant_id="G-E1", eirp=22.0
    )
    peer = PeerSas(certificate_hash="peer-e1", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    _seed_peer_conflict(db_session, peer, cbsd)
    db_session.commit()

    before = (grant.terminated, grant.max_eirp, grant.lifecycle_state)
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snap)
    assert decisions
    db_session.refresh(grant)
    assert (grant.terminated, grant.max_eirp, grant.lifecycle_state) == before


def test_inv_eval_02_evaluate_deterministic_on_same_snapshot(db_session: Session):
    """INV-EVAL-02: repeated evaluate on one snapshot yields identical decisions."""
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-e2", serial="sn-e2", grant_id="G-E2"
    )
    peer = PeerSas(certificate_hash="peer-e2", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    _seed_peer_conflict(db_session, peer, cbsd)
    db_session.commit()

    snap = freeze_cpas_snapshot(db_session)
    a = evaluate_cpas_protections(db_session, snap)
    b = evaluate_cpas_protections(db_session, snap)
    assert [
        (d.grant_pk, d.grant_id, d.action, d.reason, d.authorized_eirp_dbm_mhz)
        for d in a
    ] == [
        (d.grant_pk, d.grant_id, d.action, d.reason, d.authorized_eirp_dbm_mhz)
        for d in b
    ]
    assert any(d.grant_pk == grant.id for d in a)


# --- INV-APPLY ----------------------------------------------------------------


def test_inv_apply_01_writer_skips_peer_and_missing_pk(db_session: Session):
    """INV-APPLY-01: apply mutates only local grants with valid pk."""
    _cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-a1", serial="sn-a1", grant_id="G-A1", eirp=25.0
    )
    db_session.commit()

    # Missing pk: construct without going through typed field defaults.
    ghost = CpasDecision(
        grant_pk=grant.id,
        grant_id="ghost",
        cbsd_id=grant.cbsd_id,
        reason="peer",
        action="terminate",
    )
    object.__setattr__(ghost, "grant_pk", None)

    changed = apply_cpas_decisions(
        db_session,
        [
            ghost,
            CpasDecision(
                grant_pk=grant.id,
                grant_id=f"peer/{grant.grant_id}",
                cbsd_id=grant.cbsd_id,
                reason="peer_ns",
                action="terminate",
            ),
        ],
    )
    db_session.commit()
    assert changed == 0
    db_session.refresh(grant)
    assert grant.terminated is False


def test_inv_apply_02_closed_action_vocabulary(db_session: Session):
    """INV-APPLY-02: frozen apply writes = reduce_power|suspend|terminate; keep is no-op.

    Unknown strings (including ``reassign``, D17 PROVISIONAL/DEFER) are *not*
    part of this frozen contract — document as observation only in notes.
    """
    from services.lifecycle import GrantState

    _cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-a2", serial="sn-a2", grant_id="G-A2", eirp=30.0
    )
    db_session.commit()

    # Frozen no-op: keep (evaluate-only) must not write.
    skipped = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=grant.grant_id,
                cbsd_id=grant.cbsd_id,
                reason="keep",
                action="keep",
            ),
        ],
    )
    db_session.commit()
    assert skipped == 0
    db_session.refresh(grant)
    assert grant.terminated is False
    assert grant.max_eirp == 30.0

    reduced = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=grant.grant_id,
                cbsd_id=grant.cbsd_id,
                reason="iap",
                action="reduce_power",
                authorized_eirp_dbm_mhz=12.0,
            )
        ],
    )
    db_session.commit()
    assert reduced == 1
    db_session.refresh(grant)
    assert grant.max_eirp == 12.0
    assert grant.terminated is False

    grant.lifecycle_state = GrantState.AUTHORIZED.value
    db_session.commit()
    suspended = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=grant.grant_id,
                cbsd_id=grant.cbsd_id,
                reason="protection",
                action="suspend",
            )
        ],
    )
    db_session.commit()
    assert suspended == 1
    db_session.refresh(grant)
    assert grant.lifecycle_state == GrantState.SUSPENDED.value
    assert grant.terminated is False

    terminated = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=grant.grant_id,
                cbsd_id=grant.cbsd_id,
                reason="peer",
                action="terminate",
            )
        ],
    )
    db_session.commit()
    assert terminated == 1
    db_session.refresh(grant)
    assert grant.terminated is True
    assert grant.lifecycle_state == "TERMINATED"


# --- INV-FAIL -----------------------------------------------------------------


def test_inv_fail_01_rf_coupling_unavailable_fail_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """INV-FAIL-01: required RF coupling error aborts evaluate (D5)."""
    from services.iap import coupling as coupling_mod

    assert upsert_fss_record(
        db_session,
        {
            "record": {
                "id": "fss/coordination-fixture",
                "type": "FSS",
                "deploymentParam": [
                    {
                        "installationParam": {
                            "latitude": 39.0,
                            "longitude": -77.0,
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
    # Grant overlaps FSS passband so IAP coupling is required.
    cbsd = Cbsd(
        cbsd_id="fcc-f1/sn-f1",
        fcc_id="fcc-f1",
        cbsd_serial_number="sn-f1",
        user_id="coordination-user",
        registration_json=json.dumps(
            {
                "cbsdCategory": "A",
                "installationParam": {
                    "latitude": 39.001,
                    "longitude": -77.001,
                    "height": 6.0,
                    "heightType": "AGL",
                    "indoorDeployment": False,
                },
            }
        ),
    )
    db_session.add(cbsd)
    db_session.flush()
    expire = datetime.now(timezone.utc) + timedelta(days=1)
    db_session.add(
        Grant(
            grant_id="G-F1",
            cbsd_pk=cbsd.id,
            cbsd_id=cbsd.cbsd_id,
            low_frequency=3_620_000_000,
            high_frequency=3_625_000_000,
            max_eirp=30.0,
            channel_type="GAA",
            grant_expire_time=expire.replace(tzinfo=None),
            terminated=False,
            grant_json="{}",
        )
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)

    def _boom(*_a, **_k):
        raise PropagationUnavailableError("ITM/NED missing")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with pytest.raises(CpasRfEvaluationError, match="coupling unavailable"):
        evaluate_cpas_protections(db_session, snap)


def test_inv_fail_02_protection_data_strict_fail_closed(tmp_path: Path):
    """INV-FAIL-02: strict required datasets fail closed on payload gaps (D5)."""
    clear_dataset_bundle_cache()
    set_data_root(None)
    manifests = tmp_path / "manifests"
    data = tmp_path / "data"
    manifests.mkdir()
    _write_bundle(manifests)
    _seed_markers(data)
    set_manifests_dir(manifests)

    soft = validate_dataset_bundle("test_bundle", data_root=data, strict=False)
    assert soft.ok is True  # VERSION markers present; .flt soft-gap allowed
    hard = validate_dataset_bundle("test_bundle", data_root=data, strict=True)
    assert hard.ok is False
    with pytest.raises(DatasetValidationError, match="incomplete"):
        assert_protection_data_ready("test_bundle", data_root=data, strict=True)

    clear_dataset_bundle_cache()
    set_manifests_dir(None)
    set_data_root(None)


# --- INV-AUDIT ----------------------------------------------------------------


def test_inv_audit_01_completed_records_dump_and_stages(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """INV-AUDIT-01: success path audits cpas_completed with dumpId + stages."""
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-au1", serial="sn-au1", grant_id="G-AU1"
    )
    peer = PeerSas(certificate_hash="peer-au1", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    _seed_peer_conflict(db_session, peer, cbsd)
    db_session.commit()

    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 1, "ok": 1, "failed": 0, "errors": []},
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    assert result["dump_id"] is not None
    assert get_published_dump(db_session) is not None
    db_session.refresh(grant)
    assert grant.terminated is True

    events = _audit_events(db_session)
    completed = [e for e in events if e.get("event") == "cpas_completed"]
    assert len(completed) == 1
    detail = completed[0]
    assert detail["dumpId"] == result["dump_id"]
    assert "freeze_snapshot" in detail["stages"]
    assert "evaluate_protections" in detail["stages"]
    assert "apply_decisions_and_generate_fad" in detail["stages"]
    assert detail.get("terminatedGrants", 0) >= 1


def test_inv_audit_02_failure_rolls_back_and_audits(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """INV-AUDIT-02: failed apply/FAD rolls back grants and audits cpas_failed."""
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-au2", serial="sn-au2", grant_id="G-AU2"
    )
    peer = PeerSas(certificate_hash="peer-au2", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    _seed_peer_conflict(db_session, peer, cbsd)
    db_session.commit()

    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 1, "ok": 1, "failed": 0, "errors": []},
    )
    monkeypatch.setattr(
        "services.cpas_service.create_full_activity_dump",
        lambda db, commit=True: (_ for _ in ()).throw(RuntimeError("inject fad failure")),
    )
    with pytest.raises(RuntimeError, match="inject fad failure"):
        execute_cpas_pipeline(db_session)

    db_session.expire_all()
    db_session.refresh(grant)
    assert grant.terminated is False
    assert get_published_dump(db_session) is None

    events = _audit_events(db_session)
    failed = [e for e in events if e.get("event") == "cpas_failed"]
    assert len(failed) == 1
    assert "RuntimeError" in failed[0].get("error", "")
