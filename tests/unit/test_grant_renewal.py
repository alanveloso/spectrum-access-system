"""grant renewal with PAL licenseExpiration cap."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from models.models import PalRecord
from services.clock import ensure_utc
from services.grant_renewal import (
    AUTH_CONTEXT_KEY,
    apply_renewal,
    build_auth_context,
    compute_renewal,
    load_auth_context,
    resolve_pal_license_cap,
)
from services.grant_service import DEFAULT_GRANT_DURATION_SEC
from services.lifecycle import GrantEvent, apply_grant_event
from tests.fixtures.factories import make_cbsd, make_grant


def _stamp_auth(grant, *, channel="GAA", pal_id=None, pal_exp=None, gens=None):
    meta = json.loads(grant.grant_json or "{}")
    meta[AUTH_CONTEXT_KEY] = build_auth_context(
        channel_type=channel,
        pal_license_exp=pal_exp,
        pal_id=pal_id,
    )
    if gens:
        meta.update(gens)
    grant.grant_json = json.dumps(meta)
    grant.channel_type = channel


def test_gaa_renewal_extends_by_default_duration(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    now = datetime.utcnow().replace(microsecond=0)
    grant.grant_expire_time = now + timedelta(minutes=5)
    _stamp_auth(grant, channel="GAA")
    db_session.commit()

    result = apply_renewal(db_session, grant, now=now)
    assert result.ok
    assert result.new_expire == ensure_utc(now) + timedelta(
        seconds=DEFAULT_GRANT_DURATION_SEC
    )
    assert grant.grant_expire_time == result.new_expire


def test_pal_renewal_capped_by_license_expiration(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    now = datetime.utcnow().replace(microsecond=0)
    license_exp = now + timedelta(minutes=10)
    grant.grant_expire_time = now + timedelta(minutes=5)
    _stamp_auth(
        grant,
        channel="PAL",
        pal_id="pal-renew-1",
        pal_exp=license_exp,
    )
    db_session.add(
        PalRecord(
            pal_id="pal-renew-1",
            user_id=cbsd.user_id,
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            license_status="VALID",
            license_expiration=license_exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            record_json="{}",
        )
    )
    db_session.commit()

    result = apply_renewal(db_session, grant, now=now)
    assert result.ok
    # Default duration is 900s; license cap is 600s → must use cap.
    assert result.new_expire == ensure_utc(license_exp)
    assert grant.grant_expire_time == ensure_utc(license_exp)


def test_pal_renewal_uses_live_pal_record_over_stale_context(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    now = datetime.utcnow().replace(microsecond=0)
    stale = now + timedelta(hours=2)
    live = now + timedelta(minutes=8)
    grant.grant_expire_time = now + timedelta(minutes=5)
    _stamp_auth(grant, channel="PAL", pal_id="pal-live", pal_exp=stale)
    db_session.add(
        PalRecord(
            pal_id="pal-live",
            user_id=cbsd.user_id,
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            license_status="VALID",
            license_expiration=live.strftime("%Y-%m-%dT%H:%M:%SZ"),
            record_json="{}",
        )
    )
    db_session.commit()

    assert resolve_pal_license_cap(db_session, grant) == ensure_utc(live)
    result = apply_renewal(db_session, grant, now=now)
    assert result.ok
    assert result.new_expire == ensure_utc(live)


def test_cannot_renew_terminated_or_expired_grant(db_session):
    cbsd = make_cbsd(db_session)
    now = datetime.utcnow().replace(microsecond=0)

    terminated = make_grant(
        db_session, cbsd, authorized=True, terminated=True, lifecycle_state="TERMINATED"
    )
    terminated.grant_expire_time = now + timedelta(hours=1)
    _stamp_auth(terminated, channel="GAA")
    db_session.commit()
    assert compute_renewal(db_session, terminated, now=now).ok is False

    expired = make_grant(
        db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED"
    )
    expired.grant_expire_time = now - timedelta(seconds=1)
    _stamp_auth(expired, channel="GAA")
    db_session.commit()
    assert compute_renewal(db_session, expired, now=now).ok is False


def test_cannot_renew_when_pal_license_already_expired(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    now = datetime.utcnow().replace(microsecond=0)
    grant.grant_expire_time = now + timedelta(minutes=5)
    past = now - timedelta(minutes=1)
    _stamp_auth(grant, channel="PAL", pal_id="pal-dead", pal_exp=past)
    db_session.add(
        PalRecord(
            pal_id="pal-dead",
            user_id=cbsd.user_id,
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            license_status="VALID",
            license_expiration=past.strftime("%Y-%m-%dT%H:%M:%SZ"),
            record_json="{}",
        )
    )
    db_session.commit()
    result = compute_renewal(db_session, grant, now=now)
    assert result.ok is False
    assert result.detail == "pal_license_expired"


def test_cannot_renew_when_pal_license_status_invalid(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    now = datetime.utcnow().replace(microsecond=0)
    future = now + timedelta(hours=1)
    grant.grant_expire_time = now + timedelta(minutes=5)
    _stamp_auth(grant, channel="PAL", pal_id="pal-revoked", pal_exp=future)
    db_session.add(
        PalRecord(
            pal_id="pal-revoked",
            user_id=cbsd.user_id,
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            license_status="EXPIRED",
            license_expiration=future.strftime("%Y-%m-%dT%H:%M:%SZ"),
            record_json="{}",
        )
    )
    db_session.commit()
    result = compute_renewal(db_session, grant, now=now)
    assert result.ok is False
    assert result.detail == "pal_license_invalid"


def test_renewal_restamps_when_federal_generation_drifted(db_session):
    from services.grant_renewal import load_grant_meta, protection_stamp_drifted

    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    now = datetime.utcnow().replace(microsecond=0)
    grant.grant_expire_time = now + timedelta(minutes=5)
    _stamp_auth(
        grant,
        channel="GAA",
        gens={"fss_gen": 1, "gwbl_gen": 0, "exz_gen": 0, "dpa_gen": 0},
    )
    db_session.commit()
    # Current stamp defaults to 0 → drifted relative to stored fss_gen=1.
    assert protection_stamp_drifted(db_session, grant) is True
    result = apply_renewal(db_session, grant, now=now)
    assert result.ok
    assert result.detail == "protections_restamped"
    meta = load_grant_meta(grant)
    assert meta.get("fss_gen") == 0
    assert meta.get("gwbl_gen") == 0


def test_heartbeat_grant_renew_respects_pal_cap(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session, cbsd, authorized=False, lifecycle_state="GRANTED"
    )
    now = datetime.utcnow().replace(microsecond=0)
    license_exp = now + timedelta(minutes=10)
    grant.grant_expire_time = now + timedelta(minutes=5)
    _stamp_auth(grant, channel="PAL", pal_id="pal-hbt", pal_exp=license_exp)
    db_session.add(
        PalRecord(
            pal_id="pal-hbt",
            user_id=cbsd.user_id,
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            license_status="VALID",
            license_expiration=license_exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            record_json="{}",
        )
    )
    db_session.commit()

    from services.heartbeat_service import process_heartbeat

    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "GRANTED",
                "grantRenew": True,
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == 0
    assert "grantExpireTime" in resp[0]
    db_session.refresh(grant)
    assert grant.grant_expire_time == ensure_utc(license_exp)
    assert load_auth_context(grant)["channelType"] == "PAL"


def test_heartbeat_rejects_renew_of_relinquished_grant(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(
        db_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED"
    )
    now = datetime.utcnow().replace(microsecond=0)
    grant.grant_expire_time = now + timedelta(hours=1)
    _stamp_auth(grant, channel="GAA")
    apply_grant_event(
        grant,
        GrantEvent.RELINQUISH,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()

    from services.heartbeat_service import process_heartbeat

    resp = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "AUTHORIZED",
                "grantRenew": True,
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == 103
