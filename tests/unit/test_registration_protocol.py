"""Behavioral tests for WINNF_FT_S_REG (Registration) protocol."""

from __future__ import annotations

from datetime import datetime, timedelta

from models.models import Cbsd
from services.lifecycle import GrantState
from services.registration_service import _make_cbsd_id, process_registration
from tests.fixtures.factories import (
    cat_a_install,
    make_fcc_id,
    make_grant,
    make_user_id,
)

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103
PENDING = 200

OWNER_CERT = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"


def _full_payload(fcc_id: str, serial: str, user_id: str, **overrides) -> dict:
    payload = {
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "userId": user_id,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": cat_a_install(),
    }
    payload.update(overrides)
    return payload


def test_cat_a_success_persists_registered_cbsd_with_certificate(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(fcc.fcc_id, "sn-reg-1", user.user_id)

    resp = process_registration(db_session, [payload], certificate_hash=OWNER_CERT)

    assert resp[0]["response"]["responseCode"] == SUCCESS
    expected_cbsd_id = _make_cbsd_id(fcc.fcc_id, "sn-reg-1")
    assert resp[0]["cbsdId"] == expected_cbsd_id

    row = db_session.query(Cbsd).filter_by(cbsd_id=expected_cbsd_id).first()
    assert row is not None
    assert row.lifecycle_state == "REGISTERED"
    assert row.certificate_hash == OWNER_CERT


def test_missing_user_id_returns_102(db_session):
    fcc = make_fcc_id(db_session)
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-reg-2",
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": cat_a_install(),
    }

    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert "cbsdId" not in resp[0]


def test_unknown_fcc_id_returns_103_and_no_cbsd_row(db_session):
    user = make_user_id(db_session)
    payload = _full_payload("unknown-fcc-xyz", "sn-reg-3", user.user_id)

    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]

    assert (
        db_session.query(Cbsd).filter_by(fcc_id="unknown-fcc-xyz").first() is None
    )


def test_pending_incomplete_registration_returns_200(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-reg-4",
        "userId": user.user_id,
    }

    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == PENDING
    assert "cbsdId" not in resp[0]
    assert (
        db_session.query(Cbsd)
        .filter_by(fcc_id=fcc.fcc_id, cbsd_serial_number="sn-reg-4")
        .first()
        is None
    )


def test_cat_a_outdoor_height_over_6m_agl_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(
        fcc.fcc_id,
        "sn-reg-5",
        user.user_id,
        installationParam=cat_a_install(indoor=False, height=10.0),
    )

    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]


def test_cat_b_cleartext_install_without_cpi_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(
        fcc.fcc_id,
        "sn-reg-6",
        user.user_id,
        cbsdCategory="B",
        installationParam={
            **cat_a_install(indoor=False),
            "antennaAzimuth": 0,
            "antennaGain": 10,
            "antennaBeamwidth": 30,
        },
    )

    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]


def test_reregister_same_certificate_terminates_existing_grants(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(fcc.fcc_id, "sn-reg-7", user.user_id)

    first = process_registration(db_session, [payload], certificate_hash=OWNER_CERT)
    assert first[0]["response"]["responseCode"] == SUCCESS
    cbsd_id = first[0]["cbsdId"]

    cbsd = db_session.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
    grant = make_grant(
        db_session, cbsd, authorized=True, lifecycle_state=GrantState.AUTHORIZED.value
    )
    grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    second = process_registration(db_session, [payload], certificate_hash=OWNER_CERT)
    assert second[0]["response"]["responseCode"] == SUCCESS

    db_session.refresh(grant)
    assert grant.lifecycle_state == GrantState.TERMINATED.value
    assert grant.terminated is True


def test_batch_success_and_missing_returns_two_responses(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    good = _full_payload(fcc.fcc_id, "sn-reg-8", user.user_id)
    bad = {"fccId": fcc.fcc_id}

    resp = process_registration(db_session, [good, bad])
    assert len(resp) == 2
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[1]["response"]["responseCode"] == MISSING_PARAM
