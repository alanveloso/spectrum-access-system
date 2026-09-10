"""Definitive federal termination (500) precedes generic grant expiration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from services.federal_db_service import bump_sync_meta, heartbeat_federal_code
from services.grant_service import DEFAULT_GRANT_DURATION_SEC
from services.heartbeat_service import process_heartbeat
from services.lifecycle import GrantEvent, apply_grant_event
from tests.fixtures.factories import cat_a_install, make_cbsd, make_fss, make_grant

INVALID_PARAM = 103
TERMINATED_GRANT = 500
SUSPENDED_GRANT = 501

LAT, LON = 40.0, -105.27


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _located_cbsd(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = json.dumps(
        {
            "fccId": cbsd.fcc_id,
            "cbsdSerialNumber": cbsd.cbsd_serial_number,
            "userId": cbsd.user_id,
            "cbsdCategory": "A",
            "installationParam": cat_a_install(lat=LAT, lon=LON),
        }
    )
    db_session.commit()
    return cbsd


def _grant(
    db_session,
    cbsd,
    *,
    expired: bool,
    low_hz: int = 3_660_000_000,
    high_hz: int = 3_670_000_000,
    grant_json: dict | None = None,
):
    grant = make_grant(
        db_session,
        cbsd,
        low_hz=low_hz,
        high_hz=high_hz,
        authorized=False,
        lifecycle_state="GRANTED",
    )
    if expired:
        grant.grant_expire_time = _utcnow() - timedelta(seconds=1)
    else:
        grant.grant_expire_time = _utcnow() + timedelta(hours=1)
    if grant_json is not None:
        grant.grant_json = json.dumps(grant_json)
    db_session.commit()
    return grant


def _near_fss(db_session):
    make_fss(
        db_session,
        payload={
            "record": {
                "id": "fss-federal-hb",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": LAT, "longitude": LON},
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_700_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            }
        },
    )


def _heartbeat(db_session, cbsd, grant):
    return process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "GRANTED",
            }
        ],
    )


def test_default_grant_duration_unchanged_by_federal_hb():
    assert DEFAULT_GRANT_DURATION_SEC == 900


def test_a_expired_stale_fss_returns_500_and_terminates(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=True, grant_json={"fss_gen": 0})
    _near_fss(db_session)
    bump_sync_meta(db_session, "fss")
    db_session.commit()

    assert heartbeat_federal_code(db_session, cbsd, grant) == TERMINATED_GRANT

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert resp[0]["grantId"] == grant.grant_id
    tx = datetime.strptime(resp[0]["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
    assert tx <= datetime.utcnow()
    db_session.refresh(grant)
    assert grant.lifecycle_state == "TERMINATED"
    assert grant.terminated is True


def test_b_expired_without_federal_protection_remains_103(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=True)
    db_session.commit()

    assert heartbeat_federal_code(db_session, cbsd, grant) is None

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    db_session.refresh(grant)
    assert grant.lifecycle_state == "EXPIRED"
    assert grant.terminated is True


def test_c_expired_federal_501_only_remains_103(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=True, grant_json={"fss_gen": 1})
    _near_fss(db_session)
    bump_sync_meta(db_session, "fss")
    db_session.commit()

    assert heartbeat_federal_code(db_session, cbsd, grant) == SUSPENDED_GRANT

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    db_session.refresh(grant)
    assert grant.lifecycle_state == "EXPIRED"
    assert grant.terminated is True


def test_d_non_expired_stale_fss_still_500(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=False, grant_json={"fss_gen": 0})
    _near_fss(db_session)
    bump_sync_meta(db_session, "fss")
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    db_session.refresh(grant)
    assert grant.lifecycle_state == "TERMINATED"


def test_e_non_expired_federal_501_still_501(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=False, grant_json={"fss_gen": 1})
    _near_fss(db_session)
    bump_sync_meta(db_session, "fss")
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == SUSPENDED_GRANT
    db_session.refresh(grant)
    assert grant.lifecycle_state == "GRANTED"
    assert grant.terminated is False


def test_f_already_terminated_remains_500_via_lifecycle(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=False)
    apply_grant_event(
        grant,
        GrantEvent.TERMINATE,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()
    expire_before = grant.grant_expire_time

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    assert resp[0]["grantId"] == grant.grant_id
    db_session.refresh(grant)
    assert grant.lifecycle_state == "TERMINATED"
    assert grant.grant_expire_time == expire_before


def test_g_relinquished_remains_103_even_with_stale_fss(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _grant(db_session, cbsd, expired=False, grant_json={"fss_gen": 0})
    apply_grant_event(
        grant,
        GrantEvent.RELINQUISH,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    _near_fss(db_session)
    bump_sync_meta(db_session, "fss")
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "grantId" not in resp[0]
    db_session.refresh(grant)
    assert grant.lifecycle_state == "RELINQUISHED"


def test_fdb8_counterfactual_expired_stale_fss_generations(db_session):
    """Isolated fixture mirroring contaminated FDB_8 grant generations (2 grants)."""
    cbsd = _located_cbsd(db_session)
    stamp = {"fss_gen": 0, "gwbl_gen": 0, "exz_gen": 0, "dpa_gen": 0}
    g1 = _grant(
        db_session,
        cbsd,
        expired=True,
        low_hz=3_650_000_000,
        high_hz=3_660_000_000,
        grant_json=stamp,
    )
    g2 = _grant(
        db_session,
        cbsd,
        expired=True,
        low_hz=3_660_000_000,
        high_hz=3_670_000_000,
        grant_json=stamp,
    )
    _near_fss(db_session)
    # Simulate post-CPAS sync generation well above grant stamp (FDB_8: 146).
    from models.models import AdminInjectedData

    db_session.query(AdminInjectedData).filter_by(kind="federal_sync_meta").delete()
    db_session.add(
        AdminInjectedData(
            kind="federal_sync_meta",
            data_json=json.dumps({"fss": 146, "gwbl": 0, "exz": 0, "dpa": 0}),
        )
    )
    db_session.commit()

    for grant in (g1, g2):
        assert heartbeat_federal_code(db_session, cbsd, grant) == TERMINATED_GRANT
    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": g1.grant_id,
                "operationState": "GRANTED",
            },
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": g2.grant_id,
                "operationState": "GRANTED",
            },
        ],
    )
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    assert resp[1]["response"]["responseCode"] == TERMINATED_GRANT
