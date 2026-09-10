"""Behavioral tests for WINNF_FT_S_HBT branches not covered by
tests/unit/test_heartbeat_protocol.py: WISP termination, federal DB
(EXZ/DPA/FSS/GWBL) terminate-vs-suspend, MEAS_WITH_GRANT enforcement,
blacklist, and peer-SAS grant termination (GRA_6).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from models.models import AdminInjectedData, PeerFadRecord, PeerSas
from services.fad_service import fad_cbsd_id
from services.federal_db_service import bump_sync_meta
from services.heartbeat_service import process_heartbeat
from services.meas_report import FLAG_MEAS_HBT, MEAS_WITH_GRANT, set_admin_flag
from tests.fixtures.factories import (
    cat_a_install,
    make_cbsd,
    make_grant,
)

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
TERMINATED_GRANT = 500
SUSPENDED_GRANT = 501
UNSYNC_OP_PARAM = 502

LAT, LON = 40.0, -105.27


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _located_cbsd(db_session, *, extra: dict | None = None):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "installationParam": cat_a_install(lat=LAT, lon=LON),
    }
    if extra:
        registration.update(extra)
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    return cbsd


def _active_grant(db_session, cbsd, **kwargs):
    grant = make_grant(db_session, cbsd, authorized=False, lifecycle_state="GRANTED", **kwargs)
    grant.grant_expire_time = _utcnow() + timedelta(hours=1)
    db_session.commit()
    return grant


def _heartbeat(db_session, cbsd, grant, **overrides):
    payload = {
        "cbsdId": cbsd.cbsd_id,
        "grantId": grant.grant_id,
        "operationState": "GRANTED",
    }
    payload.update(overrides)
    return process_heartbeat(db_session, [payload])


# --- WISP overlap terminates the grant (500) -----------------------------------


def test_wisp_overlap_terminates_grant(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _active_grant(
        db_session, cbsd, low_hz=3_550_000_000, high_hz=3_560_000_000
    )
    db_session.add(
        AdminInjectedData(
            kind="wisp",
            data_json=json.dumps(
                {
                    "record": {
                        "deploymentParam": [
                            {
                                "operationParam": {
                                    "operationFrequencyRange": {
                                        "lowFrequency": 3_550_000_000,
                                        "highFrequency": 3_560_000_000,
                                    }
                                }
                            }
                        ]
                    }
                }
            ),
        )
    )
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    db_session.refresh(grant)
    assert grant.terminated is True
    assert grant.lifecycle_state == "TERMINATED"


# --- Federal EXZ: terminate stale grants, suspend current ones ----------------


def test_federal_exz_terminates_stale_grant(db_session):
    from services.exclusion_zone_service import persist_exclusion_zone
    from tests.fixtures.factories import square_polygon

    cbsd = _located_cbsd(db_session)
    grant = _active_grant(
        db_session, cbsd, low_hz=3_550_000_000, high_hz=3_560_000_000
    )
    persist_exclusion_zone(
        db_session,
        {
            "zone": square_polygon(LON, LAT),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )
    bump_sync_meta(db_session, "exz")
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    db_session.refresh(grant)
    assert grant.terminated is True


def test_federal_exz_suspends_grant_already_current(db_session):
    from services.exclusion_zone_service import persist_exclusion_zone
    from services.grant_renewal import AUTH_CONTEXT_KEY, build_auth_context
    from tests.fixtures.factories import square_polygon

    cbsd = _located_cbsd(db_session)
    grant = _active_grant(
        db_session, cbsd, low_hz=3_550_000_000, high_hz=3_560_000_000
    )
    persist_exclusion_zone(
        db_session,
        {
            "zone": square_polygon(LON, LAT),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )
    bump_sync_meta(db_session, "exz")
    # Stamp the grant as already current with the bumped generation.
    meta = {"exz_gen": 1, AUTH_CONTEXT_KEY: build_auth_context(channel_type="GAA")}
    grant.grant_json = json.dumps(meta)
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == SUSPENDED_GRANT
    db_session.refresh(grant)
    assert grant.terminated is False
    assert grant.lifecycle_state == "GRANTED"


# --- Scheduled DPA sync conflict ------------------------------------------------


def test_scheduled_dpa_sync_terminates_stale_grant(db_session):
    cbsd = _located_cbsd(db_session)
    grant = _active_grant(
        db_session, cbsd, low_hz=3_550_000_000, high_hz=3_560_000_000
    )
    bump_sync_meta(db_session, "dpa")
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT


# --- FSS neighborhood: 501 vs FDB.5 gwbl-add termination (500) -----------------


def test_fss_neighborhood_suspends_without_gwbl(db_session):
    from tests.fixtures.factories import make_fss

    cbsd = _located_cbsd(db_session)
    grant = _active_grant(
        db_session, cbsd, low_hz=3_660_000_000, high_hz=3_670_000_000
    )
    make_fss(
        db_session,
        payload={
            "record": {
                "id": "fss-hbt",
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
    bump_sync_meta(db_session, "fss")
    # Grant already synced at the current fss generation → suspend, not terminate.
    grant.grant_json = json.dumps({"fss_gen": 1})
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == SUSPENDED_GRANT


def test_fss_gwbl_added_terminates_existing_grant(db_session):
    from tests.fixtures.factories import make_fss

    cbsd = _located_cbsd(db_session)
    grant = _active_grant(
        db_session, cbsd, low_hz=3_660_000_000, high_hz=3_670_000_000
    )
    make_fss(
        db_session,
        payload={
            "record": {
                "id": "fss-hbt-gwbl",
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
    db_session.add(
        AdminInjectedData(
            kind="gwbl", data_json=json.dumps({"latitude": LAT, "longitude": LON})
        )
    )
    bump_sync_meta(db_session, "gwbl")
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT


# --- Blacklist -------------------------------------------------------------------


def test_blacklisted_cbsd_returns_101_with_ids(db_session):
    from services.blacklist_service import add_fcc_id_blacklist

    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    add_fcc_id_blacklist(db_session, cbsd.fcc_id)

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == BLACKLISTED
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    assert resp[0]["grantId"] == grant.grant_id


# --- Peer SAS active grant terminates local grant (GRA_6) ----------------------


def test_peer_sas_active_grant_terminates_local_grant(db_session):
    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    peer = PeerSas(certificate_hash="peer-cert-hbt", url="https://peer-hbt.example.test/v1.3")
    db_session.add(peer)
    db_session.commit()
    target_id = fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number)
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id=target_id,
            data_json=json.dumps(
                {"id": target_id, "grants": [{"terminated": False}]}
            ),
        )
    )
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == TERMINATED_GRANT
    db_session.refresh(grant)
    assert grant.terminated is True


# --- MEAS_WITH_GRANT enforcement after SAS requested a report -----------------


def _cbsd_with_grant_meas_capability(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "measCapability": [MEAS_WITH_GRANT],
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    return cbsd


def test_meas_report_required_after_sas_requested_it(db_session):
    cbsd = _cbsd_with_grant_meas_capability(db_session)
    grant = _active_grant(db_session, cbsd)
    grant.meas_report_requested = True
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_meas_report_invalid_after_sas_requested_it(db_session):
    cbsd = _cbsd_with_grant_meas_capability(db_session)
    grant = _active_grant(db_session, cbsd)
    grant.meas_report_requested = True
    db_session.commit()

    resp = _heartbeat(
        db_session, cbsd, grant, measReport={"rcvdPowerMeasReports": []}
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_meas_report_valid_after_sas_requested_it_succeeds(db_session):
    cbsd = _cbsd_with_grant_meas_capability(db_session)
    grant = _active_grant(db_session, cbsd)
    grant.meas_report_requested = True
    db_session.commit()

    valid_report = {
        "rcvdPowerMeasReports": [
            {
                "measFrequency": 3_550_000_000,
                "measBandwidth": 10_000_000,
                "measRcvdPower": -80.0,
            }
        ]
    }
    resp = _heartbeat(db_session, cbsd, grant, measReport=valid_report)
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_ask_meas_flag_sets_meas_report_config_with_grant(db_session):
    cbsd = _cbsd_with_grant_meas_capability(db_session)
    grant = _active_grant(db_session, cbsd)
    set_admin_flag(db_session, FLAG_MEAS_HBT)

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0]["measReportConfig"] == [MEAS_WITH_GRANT]
    db_session.refresh(grant)
    assert grant.meas_report_requested is True


# --- transmitExpireTime clipped to grantExpireTime when close to expiry -------


def test_transmit_expire_time_clipped_to_grant_expire_time(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd, authorized=False, lifecycle_state="GRANTED")
    grant.grant_expire_time = _utcnow() + timedelta(seconds=10)
    db_session.commit()

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == SUCCESS
    tx = datetime.strptime(resp[0]["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
    from services.clock import ensure_utc

    assert ensure_utc(tx) == ensure_utc(grant.grant_expire_time).replace(microsecond=0)


# --- SUSPENDED grant lifecycle state served through heartbeat_operation_allowed


def test_heartbeat_on_already_suspended_grant_returns_501(db_session):
    from services.lifecycle import GrantEvent, apply_grant_event

    cbsd = make_cbsd(db_session)
    grant = _active_grant(db_session, cbsd)
    apply_grant_event(
        grant,
        GrantEvent.SUSPEND,
        payload={"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id},
    )
    db_session.commit()
    assert grant.lifecycle_state == "SUSPENDED"

    resp = _heartbeat(db_session, cbsd, grant)
    assert resp[0]["response"]["responseCode"] == SUSPENDED_GRANT
