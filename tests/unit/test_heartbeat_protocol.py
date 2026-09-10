"""Behavioral tests for WINNF_FT_S_HBT (Heartbeat) protocol."""

from __future__ import annotations

from datetime import datetime, timedelta

from services.heartbeat_service import process_heartbeat
from tests.fixtures.factories import make_cbsd, make_dpa, make_grant

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103
SUSPENDED_GRANT = 501
UNSYNC_OP_PARAM = 502


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def test_granted_heartbeat_with_matching_state_returns_0_and_authorizes(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=False, lifecycle_state="GRANTED")
    grant.grant_expire_time = _utcnow() + timedelta(hours=1)
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
    tx = datetime.strptime(resp[0]["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
    assert tx > datetime.utcnow()

    db_session.refresh(grant)
    assert grant.lifecycle_state == "AUTHORIZED"
    assert grant.authorized is True


def test_missing_operation_state_returns_102_and_echoes_grant_id(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=False, lifecycle_state="GRANTED")
    grant.grant_expire_time = _utcnow() + timedelta(hours=1)
    db_session.commit()

    resp = process_heartbeat(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id}],
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert resp[0]["grantId"] == grant.grant_id


def test_unknown_grant_id_returns_103_with_cbsd_id_without_grant_id(db_session):
    cbsd = make_cbsd(db_session)

    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": "no-such-grant",
                "operationState": "GRANTED",
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert "grantId" not in resp[0]


def test_unsync_authorized_state_while_grant_granted_returns_502(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=False, lifecycle_state="GRANTED")
    grant.grant_expire_time = _utcnow() + timedelta(hours=1)
    db_session.commit()

    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "AUTHORIZED",
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == UNSYNC_OP_PARAM

    db_session.refresh(grant)
    assert grant.authorized is False


def test_expired_grant_returns_103_and_sets_expired_state(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    grant.grant_expire_time = _utcnow() - timedelta(seconds=1)
    db_session.commit()

    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "AUTHORIZED",
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM

    db_session.refresh(grant)
    assert grant.lifecycle_state == "EXPIRED"
    assert grant.terminated is True


def test_dpa_active_overlap_returns_501_without_persisting_suspended(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        authorized=False,
        lifecycle_state="GRANTED",
    )
    grant.grant_expire_time = _utcnow() + timedelta(hours=1)
    db_session.commit()

    make_dpa(
        db_session,
        active=True,
        payload={
            "dpaId": "dpa-active-1",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )

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
    assert resp[0]["response"]["responseCode"] == SUSPENDED_GRANT

    db_session.refresh(grant)
    assert grant.lifecycle_state == "GRANTED"
    assert grant.terminated is False
    assert grant.authorized is False


def test_certificate_mismatch_returns_103_without_any_ids(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)
    grant = make_grant(db_session, cbsd)

    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "AUTHORIZED",
            }
        ],
        certificate_hash=other,
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]
    assert "grantId" not in resp[0]
