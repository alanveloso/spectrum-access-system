"""CBSD / Grant lifecycle state machine."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.lifecycle import (
    CBSD_TRANSITIONS,
    GRANT_TRANSITIONS,
    CbsdEvent,
    CbsdState,
    GrantEvent,
    GrantState,
    apply_grant_event,
    apply_grant_state,
    evaluate_cbsd_transition,
    evaluate_grant_transition,
    heartbeat_operation_allowed,
    resolve_cbsd_state,
    resolve_grant_state,
)
from tests.fixtures.factories import make_cbsd, make_grant


def test_transition_tables_cover_required_metadata():
    for spec in CBSD_TRANSITIONS.values():
        assert spec.sources
        assert spec.target
        assert spec.persistent_effect
        assert spec.isolation in {"row_lock", "none"}
        assert isinstance(spec.required_fields, frozenset)
    for spec in GRANT_TRANSITIONS.values():
        assert spec.sources
        assert spec.target
        assert spec.persistent_effect
        assert spec.isolation in {"row_lock", "none"}


@pytest.mark.parametrize(
    "event,current,ok",
    [
        (CbsdEvent.REGISTER, CbsdState.UNREGISTERED, True),
        (CbsdEvent.REGISTER, CbsdState.DEREGISTERED, True),
        (CbsdEvent.REGISTER, CbsdState.REGISTERED, False),
        (CbsdEvent.REREGISTER, CbsdState.REGISTERED, True),
        (CbsdEvent.REREGISTER, CbsdState.UNREGISTERED, False),
        (CbsdEvent.DEREGISTER, CbsdState.REGISTERED, True),
        (CbsdEvent.DEREGISTER, CbsdState.UNREGISTERED, False),
        (CbsdEvent.DEREGISTER, CbsdState.DEREGISTERED, False),
    ],
)
def test_cbsd_transitions_parametrized(event, current, ok):
    payload = {
        "fccId": "fcc1",
        "cbsdSerialNumber": "sn1",
        "userId": "user1",
        "cbsdId": "fcc1/sn1",
    }
    outcome = evaluate_cbsd_transition(event, current=current, payload=payload)
    assert outcome.ok is ok
    if ok:
        assert outcome.to_state == CBSD_TRANSITIONS[event].target
    else:
        assert outcome.response_code in {102, 103}


@pytest.mark.parametrize(
    "event,current,ok",
    [
        (GrantEvent.APPROVE, GrantState.REQUESTED, True),
        (GrantEvent.APPROVE, GrantState.GRANTED, False),
        (GrantEvent.AUTHORIZE, GrantState.GRANTED, True),
        (GrantEvent.AUTHORIZE, GrantState.AUTHORIZED, True),
        (GrantEvent.AUTHORIZE, GrantState.SUSPENDED, True),
        (GrantEvent.AUTHORIZE, GrantState.TERMINATED, False),
        (GrantEvent.AUTHORIZE, GrantState.RELINQUISHED, False),
        (GrantEvent.SUSPEND, GrantState.AUTHORIZED, True),
        (GrantEvent.SUSPEND, GrantState.EXPIRED, False),
        (GrantEvent.TERMINATE, GrantState.GRANTED, True),
        (GrantEvent.TERMINATE, GrantState.RELINQUISHED, False),
        (GrantEvent.EXPIRE, GrantState.AUTHORIZED, True),
        (GrantEvent.RELINQUISH, GrantState.AUTHORIZED, True),
        (GrantEvent.RELINQUISH, GrantState.TERMINATED, False),
        (GrantEvent.RELINQUISH, GrantState.RELINQUISHED, False),
    ],
)
def test_grant_transitions_parametrized(event, current, ok):
    payload = {
        "cbsdId": "c-1",
        "grantId": "g-1",
        "operationState": "GRANTED",
        "operationParam": {"maxEirp": 20},
    }
    outcome = evaluate_grant_transition(event, current=current, payload=payload)
    assert outcome.ok is ok


def test_missing_required_fields_return_102():
    outcome = evaluate_cbsd_transition(
        CbsdEvent.REGISTER, current=CbsdState.UNREGISTERED, payload={}
    )
    assert outcome.ok is False
    assert outcome.response_code == 102


def test_resolve_and_apply_grant_states(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=False, lifecycle_state="GRANTED")
    # Extend expire so resolve does not treat as EXPIRED.
    grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    assert resolve_grant_state(grant) is GrantState.GRANTED
    out = apply_grant_event(
        grant,
        GrantEvent.AUTHORIZE,
        payload={
            "cbsdId": cbsd.cbsd_id,
            "grantId": grant.grant_id,
            "operationState": "GRANTED",
        },
    )
    assert out.ok
    assert grant.lifecycle_state == GrantState.AUTHORIZED.value
    assert grant.authorized is True

    out2 = apply_grant_event(
        grant,
        GrantEvent.RELINQUISH,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    assert out2.ok
    assert grant.lifecycle_state == GrantState.RELINQUISHED.value
    assert grant.terminated is True

    # Relinquish again must fail (not idempotent).
    out3 = apply_grant_event(
        grant,
        GrantEvent.RELINQUISH,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    assert out3.ok is False
    assert out3.response_code == 103


def test_heartbeat_rejects_relinquished_and_unsync(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session,
        cbsd,
        authorized=False,
        lifecycle_state="GRANTED",
    )
    grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    unsync = heartbeat_operation_allowed(grant, operation_state="AUTHORIZED")
    assert unsync.ok is False
    assert unsync.response_code == 502

    apply_grant_state(grant, GrantState.RELINQUISHED)
    db_session.commit()
    dead = heartbeat_operation_allowed(grant, operation_state="GRANTED")
    assert dead.ok is False
    assert dead.response_code == 103


def test_heartbeat_operation_allowed_terminated_is_500_not_103(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session,
        cbsd,
        authorized=False,
        lifecycle_state="GRANTED",
    )
    grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    apply_grant_state(grant, GrantState.TERMINATED)
    db_session.commit()
    outcome = heartbeat_operation_allowed(grant, operation_state="GRANTED")
    assert outcome.ok is False
    assert outcome.response_code == 500
    assert outcome.from_state == GrantState.TERMINATED.value


def test_relinquishment_sets_relinquished_state(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED"
    )
    grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    from services.relinquishment_service import process_relinquishment

    resp = process_relinquishment(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id}],
    )
    assert resp[0]["response"]["responseCode"] == 0
    db_session.refresh(grant)
    assert grant.lifecycle_state == GrantState.RELINQUISHED.value
    assert grant.terminated is True


def test_deregistration_requires_registered(db_session):
    from models.models import Cbsd
    from services.deregistration_service import process_deregistration

    cbsd = make_cbsd(db_session)
    assert resolve_cbsd_state(cbsd) is CbsdState.REGISTERED

    resp = process_deregistration(db_session, [{"cbsdId": cbsd.cbsd_id}])
    assert resp[0]["response"]["responseCode"] == 0
    assert db_session.query(Cbsd).filter_by(cbsd_id=cbsd.cbsd_id).first() is None


def test_authorize_idempotent_from_authorized(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED"
    )
    grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()
    out = apply_grant_event(
        grant,
        GrantEvent.AUTHORIZE,
        payload={
            "cbsdId": cbsd.cbsd_id,
            "grantId": grant.grant_id,
            "operationState": "AUTHORIZED",
        },
    )
    assert out.ok
    assert out.idempotent_hit is True
