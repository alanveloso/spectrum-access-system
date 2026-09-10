"""Heartbeat semantics for a grant already TERMINATED by lifecycle/CPAS."""

from __future__ import annotations

from datetime import datetime, timedelta

from services.cpas_service import CpasDecision, apply_cpas_decisions
from services.heartbeat_service import process_heartbeat
from services.lifecycle import GrantEvent, apply_grant_event
from tests.fixtures.factories import make_cbsd, make_grant

INVALID_PARAM = 103
TERMINATED_GRANT = 500
SUSPENDED_GRANT = 501


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _active_grant(db_session, cbsd, **kwargs):
    grant = make_grant(
        db_session, cbsd, authorized=False, lifecycle_state="GRANTED", **kwargs
    )
    grant.grant_expire_time = _utcnow() + timedelta(hours=1)
    db_session.commit()
    return grant


def _heartbeat(db_session, cbsd, grant_id, *, operation_state="GRANTED"):
    return process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant_id,
                "operationState": operation_state,
            }
        ],
    )


def _assert_terminal_heartbeat(resp, cbsd, grant, *, expire_before):
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert resp[0]["grantId"] == grant.grant_id
    tx = datetime.strptime(resp[0]["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
    assert tx <= datetime.utcnow()
    db_grant = grant
    from sqlalchemy.orm.session import object_session

    session = object_session(grant)
    session.refresh(db_grant)
    assert db_grant.lifecycle_state == "TERMINATED"
    assert db_grant.terminated is True
    assert db_grant.authorized is False
    assert db_grant.grant_expire_time == expire_before


def test_a_heartbeat_on_lifecycle_terminated_grant_returns_500_with_grant_id(
    db_session,
):
    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    apply_grant_event(
        grant,
        GrantEvent.TERMINATE,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()
    expire_before = grant.grant_expire_time
    assert grant.lifecycle_state == "TERMINATED"

    resp = _heartbeat(db_session, cbsd, grant.grant_id)
    _assert_terminal_heartbeat(resp, cbsd, grant, expire_before=expire_before)


def test_b_unknown_grant_id_remains_103_without_grant_id(db_session):
    cbsd = make_cbsd(db_session)
    resp = _heartbeat(db_session, cbsd, "no-such-grant")
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert "grantId" not in resp[0]


def test_b_wrong_cbsd_grant_association_remains_103_without_grant_id(db_session):
    owner = make_cbsd(db_session, fcc_id="fcc-owner", cbsd_serial_number="sn-owner")
    other = make_cbsd(db_session, fcc_id="fcc-other", cbsd_serial_number="sn-other")
    grant = _active_grant(db_session, owner)
    apply_grant_event(
        grant,
        GrantEvent.TERMINATE,
        payload={"cbsdId": owner.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()

    resp = _heartbeat(db_session, other, grant.grant_id)
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert resp[0]["cbsdId"] == other.cbsd_id
    assert "grantId" not in resp[0]
    db_session.refresh(grant)
    assert grant.lifecycle_state == "TERMINATED"


def test_c_suspended_grant_heartbeat_stays_501_not_500(db_session):
    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    apply_grant_event(
        grant,
        GrantEvent.SUSPEND,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()
    expire_before = grant.grant_expire_time

    resp = _heartbeat(db_session, cbsd, grant.grant_id)
    assert resp[0]["response"]["responseCode"] == SUSPENDED_GRANT
    assert resp[0]["grantId"] == grant.grant_id
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    db_session.refresh(grant)
    assert grant.lifecycle_state == "SUSPENDED"
    assert grant.terminated is False
    assert grant.grant_expire_time == expire_before


def test_d_cpas_terminated_grant_heartbeat_returns_500_with_original_grant_id(
    db_session,
):
    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    expire_before = grant.grant_expire_time
    original_id = grant.grant_id

    changed = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=grant.grant_id,
                cbsd_id=grant.cbsd_id,
                reason="fss_gwbl_exclusion",
                action="terminate",
                explanation="terminated-grant",
            )
        ],
    )
    db_session.commit()
    assert changed >= 1
    db_session.refresh(grant)
    assert grant.lifecycle_state == "TERMINATED"
    assert grant.terminated is True

    resp = _heartbeat(db_session, cbsd, original_id)
    _assert_terminal_heartbeat(resp, cbsd, grant, expire_before=expire_before)


def test_relinquished_grant_is_not_converted_to_500(db_session):
    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    apply_grant_event(
        grant,
        GrantEvent.RELINQUISH,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant.grant_id)
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert "grantId" not in resp[0]
    db_session.refresh(grant)
    assert grant.lifecycle_state == "RELINQUISHED"
