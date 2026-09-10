"""Fail-closed mTLS: certification/production deny non-TLS; test mode explicit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import HTTPException

from config import clear_settings_cache
from services.cbsd_auth import authorize_cbsd_operation
from services.error_handlers import CERT_ERROR
from services.mtls_auth import (
    OID_ROLE_CBSD,
    OID_ROLE_SAS,
    require_admin_certificate,
    sha1_fingerprint_colon,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _plain_request() -> MagicMock:
    req = MagicMock()
    req.scope = {}
    return req


def _tls_request_with_cert(cert: x509.Certificate) -> MagicMock:
    der = cert.public_bytes(serialization.Encoding.DER)
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = der
    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object
    req = MagicMock()
    req.scope = {"transport": transport}
    return req


def _tls_request_without_peer_cert() -> MagicMock:
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = None
    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object
    req = MagicMock()
    req.scope = {"transport": transport}
    return req


def _issue_leaf(
    *,
    issuer_cert: x509.Certificate,
    issuer_key,
    subject_key,
    role_oid,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phase-b-leaf")])
        )
        .issuer_name(issuer_cert.subject)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.CertificatePolicies(
                [x509.PolicyInformation(role_oid, policy_qualifiers=None)]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(issuer_key, hashes.SHA256())
    )


@pytest.fixture
def ca_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phase-b-ca")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phase-b-ca")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


@pytest.mark.parametrize("mode", ["certification", "production"])
def test_admin_no_tls_denied_outside_test_mode(mode: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", mode)
    clear_settings_cache()
    with pytest.raises(HTTPException) as exc:
        require_admin_certificate(_plain_request())
    assert exc.value.status_code == 403


@pytest.mark.parametrize("mode", ["certification", "production"])
def test_cbsd_no_tls_denied_outside_test_mode(mode: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", mode)
    clear_settings_cache()
    ctx = authorize_cbsd_operation(_plain_request())
    assert ctx.allowed is False
    assert ctx.denial_code == CERT_ERROR


def test_test_mode_allows_plain_testclient(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "test")
    clear_settings_cache()
    require_admin_certificate(_plain_request())
    ctx = authorize_cbsd_operation(_plain_request())
    assert ctx.allowed is True


@pytest.mark.parametrize("mode", ["certification", "production"])
def test_tls_without_peer_cert_denied(mode: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", mode)
    clear_settings_cache()
    with pytest.raises(HTTPException) as exc:
        require_admin_certificate(_tls_request_without_peer_cert())
    assert exc.value.status_code == 403
    ctx = authorize_cbsd_operation(_tls_request_without_peer_cert())
    assert ctx.allowed is False


def test_certification_mode_does_not_inherit_pytest_test_bypass(
    monkeypatch: pytest.MonkeyPatch,
):
    """Critical: setting certification must deny plain ASGI even under pytest."""
    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()
    from config import get_settings

    assert get_settings().sas_execution_mode == "certification"
    with pytest.raises(HTTPException):
        require_admin_certificate(_plain_request())


def test_admin_wrong_role_denied(ca_material, monkeypatch: pytest.MonkeyPatch):
    ca_cert, ca_key = ca_material
    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()
    monkeypatch.setattr(
        "services.certificate_policy.load_runtime_trust_context",
        lambda: ([ca_cert], None),
    )
    monkeypatch.setattr(
        "services.certificate_policy.load_admin_allowed_fingerprints",
        lambda: set(),
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_CBSD,
    )
    with pytest.raises(HTTPException) as exc:
        require_admin_certificate(_tls_request_with_cert(leaf))
    assert exc.value.status_code == 403


def test_admin_valid_role_allowed(ca_material, monkeypatch: pytest.MonkeyPatch):
    ca_cert, ca_key = ca_material
    monkeypatch.setenv("SAS_EXECUTION_MODE", "production")
    clear_settings_cache()
    monkeypatch.setattr(
        "services.certificate_policy.load_runtime_trust_context",
        lambda: ([ca_cert], None),
    )
    monkeypatch.setattr(
        "services.certificate_policy.load_admin_allowed_fingerprints",
        lambda: set(),
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_SAS,
    )
    require_admin_certificate(_tls_request_with_cert(leaf))


def test_cbsd_valid_role_allowed(ca_material, monkeypatch: pytest.MonkeyPatch):
    ca_cert, ca_key = ca_material
    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()
    monkeypatch.setattr(
        "services.cbsd_auth.load_runtime_trust_context",
        lambda: ([ca_cert], None),
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_CBSD,
    )
    ctx = authorize_cbsd_operation(_tls_request_with_cert(leaf))
    assert ctx.allowed is True
    assert ctx.certificate_hash == sha1_fingerprint_colon(leaf)
