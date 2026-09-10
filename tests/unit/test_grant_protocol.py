"""Behavioral tests for WINNF_FT_S_GRA (Grant) protocol."""

from __future__ import annotations

from models.models import Grant
from services.grant_service import process_grant
from tests.fixtures.factories import make_cbsd, make_grant

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300
GRANT_CONFLICT = 401

CBRS_LOW_HZ = 3_550_000_000


def _op_param(*, low_hz=3_550_000_000, high_hz=3_560_000_000, max_eirp=20.0) -> dict:
    return {
        "maxEirp": max_eirp,
        "operationFrequencyRange": {"lowFrequency": low_hz, "highFrequency": high_hz},
    }


def test_gaa_success_persists_grant_granted_gaa_60s_heartbeat(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )

    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0]["channelType"] == "GAA"
    assert resp[0]["heartbeatInterval"] == 60
    grant_id = resp[0]["grantId"]

    row = db_session.query(Grant).filter_by(grant_id=grant_id).first()
    assert row is not None
    assert row.lifecycle_state == "GRANTED"
    assert row.channel_type == "GAA"
    assert row.heartbeat_interval == 60


def test_missing_cbsd_id_returns_102(db_session):
    resp = process_grant(
        db_session, [{"operationParam": _op_param()}]
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert "cbsdId" not in resp[0]


def test_unknown_cbsd_id_returns_103(db_session):
    resp = process_grant(
        db_session,
        [{"cbsdId": "no-such-cbsd/serial", "operationParam": _op_param()}],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]


def test_missing_max_eirp_returns_102_with_cbsd_id(db_session):
    cbsd = make_cbsd(db_session)
    op = _op_param()
    del op["maxEirp"]

    resp = process_grant(db_session, [{"cbsdId": cbsd.cbsd_id, "operationParam": op}])
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_frequency_out_of_cbrs_returns_300(db_session):
    cbsd = make_cbsd(db_session)
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": _op_param(
                    low_hz=3_000_000_000, high_hz=3_010_000_000
                ),
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == UNSUPPORTED_SPECTRUM


def test_eirp_too_high_for_cat_a_returns_103(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=25.0)}],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_conflict_with_existing_active_grant_returns_401(db_session):
    cbsd = make_cbsd(db_session)
    make_grant(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        authorized=True,
        lifecycle_state="AUTHORIZED",
    )

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == GRANT_CONFLICT
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_batch_two_overlapping_grants_one_success_one_conflict(db_session):
    cbsd = make_cbsd(db_session)
    request = {"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}

    resp = process_grant(db_session, [dict(request), dict(request)])
    assert len(resp) == 2
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[1]["response"]["responseCode"] == GRANT_CONFLICT


def test_certificate_mismatch_returns_103_without_echo(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param()}],
        certificate_hash=other,
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]
