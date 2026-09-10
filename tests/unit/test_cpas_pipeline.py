"""transactional CPAS pipeline — freeze, decide, apply+FAD, audit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from models.models import AdminInjectedData, Cbsd, Grant, PeerFadRecord, PeerSas
from services.cpas_service import (
    KIND_CPAS_AUDIT,
    apply_peer_conflict_to_local_grants,
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.fad_service import fad_cbsd_id, get_published_dump


def _add_cbsd_with_grant(db, *, fcc: str, serial: str, grant_id: str) -> tuple[Cbsd, Grant]:
    cbsd = Cbsd(
        cbsd_id=f"{fcc}/{serial}",
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id="user-cpas",
        registration_json=json.dumps(
            {
                "fccId": fcc,
                "cbsdSerialNumber": serial,
                "cbsdCategory": "A",
                "airInterface": {"radioTechnology": "E_UTRA"},
                "measCapability": [],
                "installationParam": {
                    "latitude": 39.0,
                    "longitude": -100.0,
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
        max_eirp=20.0,
        channel_type="GAA",
        grant_expire_time=expire.replace(tzinfo=None),
        terminated=False,
        grant_json="{}",
    )
    db.add(grant)
    db.flush()
    return cbsd, grant


def _seed_peer_conflict(db, peer: PeerSas, cbsd: Cbsd) -> None:
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


def test_pipeline_stages_and_publishes_fad(db_session):
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    names = [s["name"] for s in result["stages"]]
    assert names[:5] == [
        "sync_databases",
        "sync_peer_fads",
        "freeze_snapshot",
        "evaluate_protections",
        "apply_decisions_and_generate_fad",
    ]
    assert "finalize_status_audit" in names
    assert result["dump_id"] is not None
    dump = get_published_dump(db_session)
    assert dump is not None
    assert dump.id == result["dump_id"]
    audits = db_session.query(AdminInjectedData).filter_by(kind=KIND_CPAS_AUDIT).all()
    assert any(
        json.loads(a.data_json).get("event") == "cpas_completed" for a in audits
    )


def test_pipeline_terminates_conflicting_grant_and_audits(db_session, monkeypatch):
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-cpas", serial="sn-cpas", grant_id="G-CPAS-1"
    )
    peer = PeerSas(certificate_hash="peer-cpas", url="https://localhost/v1.3")
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
    db_session.refresh(grant)
    assert grant.terminated is True
    assert grant.lifecycle_state == "TERMINATED"
    assert any(d["reason"] == "peer_same_cbsd_grant" for d in result["decisions"])
    assert result["terminated_grants"] >= 1


def test_fad_failure_rolls_back_grant_terminations(db_session, monkeypatch):
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-rb", serial="sn-rb", grant_id="G-RB-1"
    )
    peer = PeerSas(certificate_hash="peer-rb", url="https://localhost/v1.3")
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
    audits = db_session.query(AdminInjectedData).filter_by(kind=KIND_CPAS_AUDIT).all()
    assert any(json.loads(a.data_json).get("event") == "cpas_failed" for a in audits)


def test_freeze_limits_decisions_to_snapshot_grants(db_session):
    peer = PeerSas(certificate_hash="peer-freeze", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    c1, g1 = _add_cbsd_with_grant(
        db_session, fcc="fcc-a2", serial="sn-a2", grant_id="G-A2"
    )
    _seed_peer_conflict(db_session, peer, c1)
    snapshot = freeze_cpas_snapshot(db_session)
    c2, g2 = _add_cbsd_with_grant(
        db_session, fcc="fcc-b2", serial="sn-b2", grant_id="G-B2"
    )
    _seed_peer_conflict(db_session, peer, c2)
    db_session.commit()

    decisions = evaluate_cpas_protections(db_session, snapshot)
    pks = {d.grant_pk for d in decisions}
    assert g1.id in pks
    assert g2.id not in pks
    # Peer rows added after freeze must not expand the frozen peer set.
    assert snapshot.peer_record_count == 1



def test_apply_peer_conflict_wrapper_still_commits(db_session):
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-w", serial="sn-w", grant_id="G-W"
    )
    peer = PeerSas(certificate_hash="peer-w", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    _seed_peer_conflict(db_session, peer, cbsd)
    db_session.commit()
    apply_peer_conflict_to_local_grants(db_session)
    db_session.refresh(grant)
    assert grant.terminated is True
    assert grant.lifecycle_state == "TERMINATED"
