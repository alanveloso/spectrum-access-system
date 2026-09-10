"""CPAS freeze→evaluate is read-only; apply writes.

Pins the Coordination Core seam seed ( D1/D3) without expanding into the
full  invariant suite.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from models.models import Cbsd, Grant, PeerFadRecord, PeerSas
from services.cpas_service import (
    CpasDecision,
    CpasSnapshot,
    FrozenLocalGrantRf,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    freeze_cpas_snapshot,
)
from services.fad_service import fad_cbsd_id


def _add_cbsd_with_grant(
    db, *, fcc: str, serial: str, grant_id: str, eirp: float = 20.0
) -> tuple[Cbsd, Grant]:
    cbsd = Cbsd(
        cbsd_id=f"{fcc}/{serial}",
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id="characterization-user",
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
        max_eirp=eirp,
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


def test_snapshot_and_frozen_grant_rf_are_immutable():
    frozen = FrozenLocalGrantRf(
        grant_pk=1,
        grant_id="g",
        cbsd_id="c",
        fcc_id="fcc",
        cbsd_serial_number="sn",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
        lifecycle_state="GRANTED",
        terminated=False,
        latitude=39.0,
        longitude=-100.0,
        height_m=10.0,
        height_type="AGL",
        indoor=False,
        cbsd_category="A",
    )
    with pytest.raises(FrozenInstanceError):
        frozen.max_eirp_dbm_mhz = 10.0  # type: ignore[misc]

    snap = CpasSnapshot(
        frozen_at="2026-08-10T00:00:00Z",
        active_grant_pks=(1,),
        local_grants=(frozen,),
    )
    with pytest.raises(FrozenInstanceError):
        snap.active_grant_pks = (2,)  # type: ignore[misc]


def test_evaluate_does_not_mutate_grant_apply_does(db_session):
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-seam", serial="sn-seam", grant_id="G-SEAM-1", eirp=23.0
    )
    peer = PeerSas(certificate_hash="peer-seam", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    _seed_peer_conflict(db_session, peer, cbsd)
    db_session.commit()

    before_terminated = grant.terminated
    before_eirp = grant.max_eirp
    before_state = grant.lifecycle_state

    snapshot = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snapshot)
    assert any(d.grant_pk == grant.id for d in decisions)

    db_session.refresh(grant)
    assert grant.terminated is before_terminated
    assert grant.max_eirp == before_eirp
    assert grant.lifecycle_state == before_state

    changed = apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    assert changed >= 1
    db_session.refresh(grant)
    assert grant.terminated is True
    assert grant.lifecycle_state == "TERMINATED"


def test_apply_reduce_power_decision_writes_max_eirp_only(db_session):
    cbsd, grant = _add_cbsd_with_grant(
        db_session, fcc="fcc-red", serial="sn-red", grant_id="G-RED-1", eirp=30.0
    )
    db_session.commit()

    decision = CpasDecision(
        grant_pk=grant.id,
        grant_id=grant.grant_id,
        cbsd_id=grant.cbsd_id,
        reason="characterization_reduce",
        action="reduce_power",
        authorized_eirp_dbm_mhz=18.0,
        explanation="characterization-seam",
    )
    changed = apply_cpas_decisions(db_session, [decision])
    db_session.commit()
    assert changed == 1
    db_session.refresh(grant)
    assert grant.max_eirp == 18.0
    assert grant.terminated is False
