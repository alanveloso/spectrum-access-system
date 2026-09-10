"""Unit tests for cryptographic CPI signature verification ()."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from models.models import Cbsd, CpiUser
from services.cpi_signature import (
    ALLOWED_ALGORITHMS,
    INVALID_PARAM,
    MISSING_PARAM,
    b64url_decode,
    b64url_encode,
    sign_cpi_payload,
    structural_cpi_error,
    verify_cpi_signature,
)
from services.registration_service import process_registration
from tests.fixtures.factories import cat_a_install, make_fcc_id, make_user_id


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


def _ec_keypair() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
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


def _cert_time(when: datetime | None = None) -> str:
    dt = when or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _signed_install_payload(
    *,
    fcc_id: str,
    serial: str,
    cpi_id: str,
    install_time: str | None = None,
) -> dict:
    return {
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "installationParam": {
            **cat_a_install(indoor=False, height=5.0),
            "antennaAzimuth": 0,
            "antennaGain": 10,
            "antennaBeamwidth": 30,
        },
        "professionalInstallerData": {
            "cpiId": cpi_id,
            "cpiName": "Test Installer",
            "installCertificationTime": install_time or _cert_time(),
        },
    }


def _flip_one_byte(token: str, index: int = 4) -> str:
    """Alter a single character in a Base64URL segment (tamper test)."""
    chars = list(token)
    assert len(chars) > index
    # Stay within base64url alphabet when flipping.
    chars[index] = "A" if chars[index] != "A" else "B"
    return "".join(chars)


@pytest.mark.parametrize("algorithm,keygen", [("RS256", _rsa_keypair), ("ES256", _ec_keypair)])
def test_verify_cpi_signature_accepts_valid_rs256_and_es256(algorithm, keygen):
    private_pem, public_pem = keygen()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    sig = sign_cpi_payload(payload, private_pem, algorithm=algorithm)
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
    )
    assert result.ok
    assert result.algorithm == algorithm
    assert result.cpi_id == "cpi-1"
    assert result.payload["installationParam"]["height"] == 5.0
    assert algorithm in ALLOWED_ALGORITHMS


@pytest.mark.parametrize(
    "field",
    ["protectedHeader", "encodedCpiSignedData", "digitalSignature"],
)
def test_verify_rejects_one_byte_tamper_of_each_jwt_segment(field):
    private_pem, public_pem = _rsa_keypair()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    sig[field] = _flip_one_byte(sig[field])
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
    )
    assert not result.ok
    assert result.response_code == INVALID_PARAM


def test_verify_rejects_disallowed_algorithm():
    private_pem, public_pem = _rsa_keypair()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    # Forge protected header with alg=none while keeping payload/sig bytes.
    forged_header = b64url_encode(
        json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    sig["protectedHeader"] = forged_header
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
    )
    assert not result.ok
    assert result.response_code == INVALID_PARAM


def test_verify_rejects_wrong_public_key():
    private_pem, _ = _rsa_keypair()
    _, other_public = _rsa_keypair()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    result = verify_cpi_signature(
        sig,
        public_key_pem=other_public,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
    )
    assert not result.ok
    assert result.response_code == INVALID_PARAM


def test_verify_rejects_fcc_serial_mismatch():
    private_pem, public_pem = _rsa_keypair()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-OTHER",
        request_serial="SN-1",
    )
    assert not result.ok
    assert result.response_code == INVALID_PARAM


def test_verify_rejects_signed_payload_missing_fcc_binding():
    private_pem, public_pem = _rsa_keypair()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    del payload["fccId"]
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
    )
    assert not result.ok
    assert result.response_code == MISSING_PARAM


def test_verify_rejects_signed_payload_missing_installation_param():
    private_pem, public_pem = _rsa_keypair()
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    del payload["installationParam"]
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
    )
    assert not result.ok
    assert result.response_code == MISSING_PARAM


def test_structural_rejects_non_string_jwt_segments():
    payload = _signed_install_payload(fcc_id="FCC-1", serial="SN-1", cpi_id="cpi-1")
    encoded = b64url_encode(json.dumps(payload).encode("utf-8"))
    sig = {
        "protectedHeader": {"alg": "RS256"},
        "encodedCpiSignedData": encoded,
        "digitalSignature": "sig",
    }
    assert structural_cpi_error(sig) == INVALID_PARAM


def test_verify_rejects_future_install_certification_time():
    private_pem, public_pem = _rsa_keypair()
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    payload = _signed_install_payload(
        fcc_id="FCC-1",
        serial="SN-1",
        cpi_id="cpi-1",
        install_time=_cert_time(future),
    )
    sig = sign_cpi_payload(payload, private_pem, algorithm="RS256")
    result = verify_cpi_signature(
        sig,
        public_key_pem=public_pem,
        request_fcc_id="FCC-1",
        request_serial="SN-1",
        now=datetime.now(timezone.utc),
    )
    assert not result.ok
    assert result.response_code == INVALID_PARAM


def test_structural_requires_install_certification_time():
    payload = {
        "professionalInstallerData": {"cpiId": "cpi-1"},
    }
    encoded = b64url_encode(json.dumps(payload).encode("utf-8"))
    sig = {
        "protectedHeader": "hdr",
        "encodedCpiSignedData": encoded,
        "digitalSignature": "sig",
    }
    assert structural_cpi_error(sig) == MISSING_PARAM


def test_registration_accepts_cryptographically_valid_cpi(db_session):
    private_pem, public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    db_session.add(
        CpiUser(cpi_id="cpi-crypto-1", cpi_name="Installer", cpi_public_key=public_pem)
    )
    db_session.commit()

    signed = _signed_install_payload(
        fcc_id=fcc.fcc_id, serial="sn-crypto-1", cpi_id="cpi-crypto-1"
    )
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-crypto-1",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "cpiSignatureData": sign_cpi_payload(signed, private_pem, algorithm="RS256"),
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == 0
    row = db_session.query(Cbsd).filter_by(cbsd_id=f"{fcc.fcc_id}/sn-crypto-1").first()
    assert row is not None


def test_registration_rejects_tampered_digital_signature(db_session):
    private_pem, public_pem = _rsa_keypair()
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    db_session.add(
        CpiUser(cpi_id="cpi-crypto-2", cpi_name="Installer", cpi_public_key=public_pem)
    )
    db_session.commit()

    signed = _signed_install_payload(
        fcc_id=fcc.fcc_id, serial="sn-crypto-2", cpi_id="cpi-crypto-2"
    )
    sig = sign_cpi_payload(signed, private_pem, algorithm="RS256")
    sig["digitalSignature"] = _flip_one_byte(sig["digitalSignature"])
    payload = {
        "fccId": fcc.fcc_id,
        "cbsdSerialNumber": "sn-crypto-2",
        "userId": user.user_id,
        "cbsdCategory": "B",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "cpiSignatureData": sig,
    }
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_b64url_roundtrip():
    raw = b'{"a":1}'
    assert b64url_decode(b64url_encode(raw)) == raw
