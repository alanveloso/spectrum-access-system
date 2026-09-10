"""Additional behavioral tests for WINNF_FT_S_REG (Registration) branches
not covered by tests/unit/test_registration_protocol.py: CPI signature structure,
conditionals deep-merge, quiet-zone rejection, measReportConfig (MES_1) and the
IntegrityError race-recovery path.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.exc import IntegrityError

from models.models import Cbsd, CpiUser
from services.cpi_signature import b64url_decode as _b64url_decode
from services.cpi_signature import (
    decode_cpi_signed_data as _decode_cpi_signed_data,
)
from services.cpi_signature import sign_cpi_payload
from services.meas_report import FLAG_MEAS_REG, MEAS_WITHOUT_GRANT, set_admin_flag
from services.quiet_zone_service import NRQZ_EAST, NRQZ_NORTH, NRQZ_SOUTH, NRQZ_WEST
from services.registration_service import (
    _cpi_missing_params,
    _make_cbsd_id,
    process_registration,
)
from tests.fixtures.factories import (
    cat_a_install,
    make_conditionals,
    make_fcc_id,
    make_user_id,
)

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103
PENDING = 200

# A published NRAO/NRRO Quiet Zone coordinate (47 CFR § 1.924(a) NAD-83 bounds);
# real regulatory geometry, not a harness fixture.
NRQZ_LAT = (NRQZ_SOUTH + NRQZ_NORTH) / 2.0
NRQZ_LON = (NRQZ_WEST + NRQZ_EAST) / 2.0


def _b64url_encode(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cert_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


def _prof(cpi_id: str, **extra) -> dict:
    data = {
        "cpiId": cpi_id,
        "cpiName": "Installer",
        "installCertificationTime": _cert_time(),
    }
    data.update(extra)
    return data


def _cpi_sig(payload: dict, *, digital_signature: str = "sig", protected: str = "hdr") -> dict:
    """Structural-only fake (not cryptographically valid)."""
    return {
        "protectedHeader": protected,
        "encodedCpiSignedData": _b64url_encode(payload),
        "digitalSignature": digital_signature,
    }


def _real_cpi_sig(
    payload: dict,
    private_pem: str,
    *,
    algorithm: str = "RS256",
) -> dict:
    return sign_cpi_payload(payload, private_pem, algorithm=algorithm)


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


# --- _b64url_decode / _decode_cpi_signed_data --------------------------------


def test_b64url_decode_roundtrip_without_padding():
    encoded = _b64url_encode({"a": 1})
    assert json.loads(_b64url_decode(encoded)) == {"a": 1}


def test_decode_cpi_signed_data_missing_encoded_returns_none():
    assert _decode_cpi_signed_data({"digitalSignature": "x"}) is None


def test_decode_cpi_signed_data_invalid_base64_returns_none():
    assert _decode_cpi_signed_data({"encodedCpiSignedData": "%%%not-b64%%%"}) is None


def test_decode_cpi_signed_data_valid_returns_payload():
    encoded = _b64url_encode({"fccId": "f1"})
    assert _decode_cpi_signed_data({"encodedCpiSignedData": encoded}) == {"fccId": "f1"}


# --- _cpi_missing_params -------------------------------------------------------


def test_cpi_missing_digital_signature_returns_missing_param(db_session):
    sig = _cpi_sig({"professionalInstallerData": _prof("cpi-1")})
    sig["digitalSignature"] = ""
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == MISSING_PARAM


def test_cpi_missing_encoded_data_returns_missing_param(db_session):
    sig = {"protectedHeader": "h", "digitalSignature": "s"}
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == MISSING_PARAM


def test_cpi_missing_protected_header_returns_missing_param(db_session):
    sig = _cpi_sig({"professionalInstallerData": _prof("cpi-1")})
    sig["protectedHeader"] = ""
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == MISSING_PARAM


def test_cpi_undecodable_signed_data_returns_invalid_param(db_session):
    sig = {
        "protectedHeader": "h",
        "digitalSignature": "s",
        "encodedCpiSignedData": "%%%garbage%%%",
    }
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == INVALID_PARAM


def test_cpi_missing_cpi_id_returns_missing_param(db_session):
    sig = _cpi_sig({"professionalInstallerData": {"installCertificationTime": _cert_time()}})
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == MISSING_PARAM


def test_cpi_missing_install_cert_time_returns_missing_param(db_session):
    sig = _cpi_sig({"professionalInstallerData": {"cpiId": "cpi-ok"}})
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == MISSING_PARAM


def test_cpi_complete_structure_returns_none(db_session):
    sig = _cpi_sig(
        {
            "fccId": "FCC-X",
            "cbsdSerialNumber": "SN-X",
            "installationParam": {"height": 1.0},
            "professionalInstallerData": _prof("cpi-ok"),
        }
    )
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) is None


def test_cpi_missing_signed_fcc_id_returns_missing_param(db_session):
    sig = _cpi_sig(
        {
            "cbsdSerialNumber": "SN-X",
            "installationParam": {"height": 1.0},
            "professionalInstallerData": _prof("cpi-ok"),
        }
    )
    assert _cpi_missing_params({"cpiSignatureData": sig}, db_session) == MISSING_PARAM


# --- process_registration: CPI signature end-to-end ---------------------------


def test_registration_incomplete_cpi_signature_returns_102(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    sig = {"protectedHeader": "h", "digitalSignature": "s"}  # missing encoded data
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-1",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "cpiSignatureData": sig,
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_registration_valid_cpi_signature_merges_installation_and_succeeds(db_session):
    private_pem, public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    db_session.add(
        CpiUser(cpi_id="cpi-valid-1", cpi_name="Installer", cpi_public_key=public_pem)
    )
    db_session.commit()

    signed = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-2",
        "professionalInstallerData": _prof("cpi-valid-1"),
        "installationParam": {
            **cat_a_install(indoor=False, height=5.0),
            "antennaAzimuth": 0,
            "antennaGain": 10,
            "antennaBeamwidth": 30,
        },
    }
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-2",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "cpiSignatureData": _real_cpi_sig(signed, private_pem),
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    row = db_session.query(Cbsd).filter_by(
        cbsd_id=_make_cbsd_id(fcc.fcc_id, "sn-cpi-2")
    ).first()
    assert row is not None
    stored = json.loads(row.registration_json)
    assert stored["installationParam"]["height"] == 5.0
    assert stored["installationParam"]["indoorDeployment"] is False


def test_registration_cpi_signature_with_unknown_cpi_id_returns_103(db_session):
    private_pem, _public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    signed = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-3",
        "professionalInstallerData": _prof("cpi-does-not-exist"),
        "installationParam": cat_a_install(),
    }
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-3",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "cpiSignatureData": _real_cpi_sig(signed, private_pem),
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_registration_cat_b_cpi_signature_plus_cleartext_install_returns_103(db_session):
    private_pem, public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    db_session.add(CpiUser(cpi_id="cpi-clear-b", cpi_name="", cpi_public_key=public_pem))
    db_session.commit()
    signed = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-4",
        "professionalInstallerData": _prof("cpi-clear-b"),
        "installationParam": cat_a_install(indoor=False),
    }
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-4",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": cat_a_install(indoor=False),
        "cpiSignatureData": _real_cpi_sig(signed, private_pem),
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_registration_cat_a_cpi_signature_plus_cleartext_install_returns_103(db_session):
    """Any category with both CPI-signed data and cleartext installationParam is invalid."""
    private_pem, public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    db_session.add(CpiUser(cpi_id="cpi-clear-a", cpi_name="", cpi_public_key=public_pem))
    db_session.commit()
    signed = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-5",
        "professionalInstallerData": _prof("cpi-clear-a"),
        "installationParam": cat_a_install(),
    }
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cpi-5",
        "userId": user.user_id,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": cat_a_install(),
        "cpiSignatureData": _real_cpi_sig(signed, private_pem),
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


# --- conditionals merge (deep merge) ------------------------------------------


def test_conditionals_deep_merge_supplies_missing_installation_fields(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    make_conditionals(
        db_session,
        fcc_id=fcc.fcc_id,
        cbsd_serial_number="sn-cond-1",
        data={
            "cbsdCategory": "A",
            "airInterface": {"radioTechnology": "E_UTRA"},
            "installationParam": {
                "latitude": 40.0,
                "longitude": -105.27,
                "height": 3.0,
                "heightType": "AGL",
                "indoorDeployment": True,
            },
        },
    )
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cond-1",
        "userId": user.user_id,
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_cat_b_conditionals_alone_without_cpi_succeeds(db_session):
    """Cat B fully specified via preloaded conditionals (no CPI, no cleartext)."""
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    make_conditionals(
        db_session,
        fcc_id=fcc.fcc_id,
        cbsd_serial_number="sn-cond-b1",
        data={
            "cbsdCategory": "B",
            "airInterface": {"radioTechnology": "E_UTRA"},
            "installationParam": {
                "latitude": 39.1,
                "longitude": -94.58,
                "height": 5.0,
                "heightType": "AGL",
                "antennaAzimuth": 0,
                "antennaGain": 10,
                "antennaBeamwidth": 30,
            },
        },
    )
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-cond-b1",
        "userId": user.user_id,
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS


# --- field validation branches -------------------------------------------------


def test_serial_number_too_long_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(fcc.fcc_id, "s" * 65, user.user_id)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_fcc_id_too_long_returns_103(db_session):
    fcc = make_fcc_id(db_session, fcc_id="f" * 25)
    user = make_user_id(db_session)
    payload = _full_payload(fcc.fcc_id, "sn-long-fcc", user.user_id)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_user_id_invalid_characters_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    payload = _full_payload(fcc.fcc_id, "sn-bad-user", "user with spaces!")
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_meas_capability_not_a_list_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(
        fcc.fcc_id, "sn-meas-1", user.user_id, measCapability="not-a-list"
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_meas_capability_unknown_value_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(
        fcc.fcc_id, "sn-meas-2", user.user_id, measCapability=["BOGUS_CAPABILITY"]
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_latitude_out_of_range_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = cat_a_install()
    install["latitude"] = 95.0
    payload = _full_payload(fcc.fcc_id, "sn-lat-1", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_longitude_out_of_range_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = cat_a_install()
    install["longitude"] = -190.0
    payload = _full_payload(fcc.fcc_id, "sn-lon-1", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_antenna_azimuth_out_of_range_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = cat_a_install()
    install["antennaAzimuth"] = 400.0
    payload = _full_payload(fcc.fcc_id, "sn-az-1", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_height_type_invalid_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = cat_a_install()
    install["heightType"] = "BOGUS"
    payload = _full_payload(fcc.fcc_id, "sn-ht-1", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_eirp_capability_over_fcc_max_returns_103(db_session):
    fcc = make_fcc_id(db_session, fcc_max_eirp=30.0)
    user = make_user_id(db_session)
    install = cat_a_install()
    install["eirpCapability"] = 35.0
    payload = _full_payload(fcc.fcc_id, "sn-eirp-1", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_eirp_capability_over_cat_a_limit_returns_103(db_session):
    fcc = make_fcc_id(db_session, fcc_max_eirp=47.0)
    user = make_user_id(db_session)
    install = cat_a_install()
    install["eirpCapability"] = 35.0
    payload = _full_payload(fcc.fcc_id, "sn-eirp-2", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_cat_b_indoor_deployment_true_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = {
        **cat_a_install(indoor=True),
        "antennaAzimuth": 0,
        "antennaGain": 10,
        "antennaBeamwidth": 30,
    }
    payload = _full_payload(
        fcc.fcc_id, "sn-catb-indoor", user.user_id, cbsdCategory="B", installationParam=install
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_pending_missing_installation_param_entirely(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-nopend-1",
        "userId": user.user_id,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == PENDING


def test_pending_cat_a_missing_indoor_deployment_flag(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = cat_a_install()
    del install["indoorDeployment"]
    payload = _full_payload(fcc.fcc_id, "sn-nopend-2", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == PENDING


def test_pending_cat_b_missing_antenna_fields(db_session):
    """Cat B via CPI-signed installationParam missing antennaBeamwidth → PENDING."""
    private_pem, public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    db_session.add(CpiUser(cpi_id="cpi-nopend-3", cpi_name="", cpi_public_key=public_pem))
    db_session.commit()
    signed = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-nopend-3",
        "professionalInstallerData": _prof("cpi-nopend-3"),
        "installationParam": {
            **cat_a_install(indoor=False),
            "antennaAzimuth": 0,
            "antennaGain": 10,
        },
    }
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-nopend-3",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "cpiSignatureData": _real_cpi_sig(signed, private_pem),
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == PENDING


# --- blacklist -----------------------------------------------------------------


def test_registration_blacklisted_fcc_serial_returns_101(db_session):
    from services.blacklist_service import add_fcc_id_blacklist

    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    add_fcc_id_blacklist(db_session, fcc.fcc_id)
    payload = _full_payload(fcc.fcc_id, "sn-blk-1", user.user_id)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == 101


# --- quiet zone (QPR.2) ---------------------------------------------------------


def test_registration_inside_nrqz_quiet_zone_returns_103(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    install = cat_a_install(lat=NRQZ_LAT, lon=NRQZ_LON)
    payload = _full_payload(fcc.fcc_id, "sn-qz-1", user.user_id, installationParam=install)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert (
        db_session.query(Cbsd)
        .filter_by(fcc_id=fcc.fcc_id, cbsd_serial_number="sn-qz-1")
        .first()
        is None
    )


# --- MES_1: measReportConfig on successful registration -------------------------


def test_registration_meas_report_config_included_when_flag_set(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)
    payload = _full_payload(
        fcc.fcc_id,
        "sn-mes-1",
        user.user_id,
        measCapability=[MEAS_WITHOUT_GRANT],
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0]["measReportConfig"] == [MEAS_WITHOUT_GRANT]


def test_registration_meas_report_config_absent_without_capability(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)
    payload = _full_payload(fcc.fcc_id, "sn-mes-2", user.user_id)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert "measReportConfig" not in resp[0]


# --- IntegrityError race-recovery path -----------------------------------------


def test_registration_integrity_error_on_insert_returns_103_and_rolls_back(
    db_session, monkeypatch
):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _full_payload(fcc.fcc_id, "sn-race-1", user.user_id)

    original_flush = db_session.flush
    calls = {"count": 0}

    def _flush_once_then_raise(*args, **kwargs):
        calls["count"] += 1
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(db_session, "flush", _flush_once_then_raise)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert calls["count"] == 1

    monkeypatch.setattr(db_session, "flush", original_flush)
    row = (
        db_session.query(Cbsd)
        .filter_by(fcc_id=fcc.fcc_id, cbsd_serial_number="sn-race-1")
        .first()
    )
    assert row is None

    # A retry without the induced failure must succeed normally.
    resp2 = process_registration(db_session, [payload])
    assert resp2[0]["response"]["responseCode"] == SUCCESS
