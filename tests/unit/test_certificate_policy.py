"""role-specific certificate policy validators."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import (
    ExtendedKeyUsageOID,
    NameOID,
    ObjectIdentifier,
)
from fastapi import HTTPException
from unittest.mock import MagicMock

from services.certificate_policy import (
    CertRejectReason,
    validate_admin_certificate,
    validate_cbsd_certificate,
    validate_domain_proxy_certificate,
    validate_sas_certificate,
)
from services.mtls_auth import (
    OID_ROLE_CBSD,
    OID_ROLE_DOMAIN_PROXY,
    OID_ROLE_INSTALLER,
    OID_ROLE_SAS,
    OID_ZONE,
    require_admin_certificate,
    sha1_fingerprint_colon,
)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _issue_leaf(
    *,
    issuer_cert: x509.Certificate,
    issuer_key,
    subject_key,
    role_oid: ObjectIdentifier,
    extra_policies: list[ObjectIdentifier] | None = None,
    with_client_auth: bool = True,
    with_digital_signature: bool = True,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    is_ca: bool = False,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    policies = [role_oid, *(extra_policies or [])]
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name("leaf"))
        .issuer_name(issuer_cert.subject)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (now - timedelta(minutes=1)))
        .not_valid_after(not_after or (now + timedelta(days=1)))
        .add_extension(
            x509.BasicConstraints(ca=is_ca, path_length=None), critical=True
        )
        .add_extension(
            x509.CertificatePolicies(
                [
                    x509.PolicyInformation(policy_identifier=oid, policy_qualifiers=None)
                    for oid in policies
                ]
            ),
            critical=False,
        )
    )
    if with_client_auth:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
    if with_digital_signature:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=is_ca,
                crl_sign=is_ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def ca_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("test-ca"))
        .issuer_name(_name("test-ca"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


@pytest.mark.parametrize(
    "validator,role",
    [
        (validate_cbsd_certificate, OID_ROLE_CBSD),
        (validate_domain_proxy_certificate, OID_ROLE_DOMAIN_PROXY),
        (validate_sas_certificate, OID_ROLE_SAS),
        (validate_admin_certificate, OID_ROLE_SAS),
    ],
)
@pytest.mark.parametrize("key_type", ["rsa", "ecc"])
def test_validators_accept_matching_role_rsa_and_ecc(ca_material, validator, role, key_type):
    ca_cert, ca_key = ca_material
    if key_type == "rsa":
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=role
    )
    result = validator(leaf, trust_roots=[ca_cert])
    assert result.ok, result.reason


@pytest.mark.parametrize(
    "validator,good_role,bad_role",
    [
        (validate_cbsd_certificate, OID_ROLE_CBSD, OID_ROLE_SAS),
        (validate_domain_proxy_certificate, OID_ROLE_DOMAIN_PROXY, OID_ROLE_CBSD),
        (validate_sas_certificate, OID_ROLE_SAS, OID_ROLE_CBSD),
        (validate_admin_certificate, OID_ROLE_SAS, OID_ROLE_INSTALLER),
    ],
)
def test_validators_reject_wrong_role(ca_material, validator, good_role, bad_role):
    del good_role
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=bad_role
    )
    result = validator(leaf, trust_roots=[ca_cert])
    assert not result.ok
    assert result.reason is CertRejectReason.WRONG_ROLE


def test_validators_reject_zone_policy(ca_material):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_SAS,
        extra_policies=[OID_ZONE],
    )
    result = validate_sas_certificate(leaf, trust_roots=[ca_cert])
    assert not result.ok
    assert result.reason is CertRejectReason.ZONE_POLICY


def test_validators_reject_expired(ca_material):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_CBSD,
        not_before=now - timedelta(days=10),
        not_after=now - timedelta(days=1),
    )
    result = validate_cbsd_certificate(leaf, trust_roots=[ca_cert], now=now)
    assert not result.ok
    assert result.reason is CertRejectReason.EXPIRED


def test_validators_reject_missing_eku(ca_material):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_CBSD,
        with_client_auth=False,
    )
    result = validate_cbsd_certificate(leaf, trust_roots=[ca_cert])
    assert not result.ok
    assert result.reason is CertRejectReason.MISSING_EKU


def test_validators_reject_missing_digital_signature(ca_material):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=leaf_key,
        role_oid=OID_ROLE_DOMAIN_PROXY,
        with_digital_signature=False,
    )
    result = validate_domain_proxy_certificate(leaf, trust_roots=[ca_cert])
    assert not result.ok
    assert result.reason is CertRejectReason.BAD_KEY_USAGE


def test_validators_reject_untrusted_chain(ca_material):
    ca_cert, ca_key = ca_material
    other_ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    other_ca = (
        x509.CertificateBuilder()
        .subject_name(_name("other-ca"))
        .issuer_name(_name("other-ca"))
        .public_key(other_ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(other_ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=OID_ROLE_SAS
    )
    result = validate_sas_certificate(leaf, trust_roots=[other_ca])
    assert not result.ok
    assert result.reason is CertRejectReason.UNTRUSTED_CHAIN


def test_validators_reject_revoked_serial(ca_material, tmp_path: Path):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=OID_ROLE_CBSD
    )
    now = datetime.now(timezone.utc)
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - timedelta(minutes=1))
        .next_update(now + timedelta(days=1))
        .add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(leaf.serial_number)
            .revocation_date(now - timedelta(hours=1))
            .build()
        )
        .sign(ca_key, hashes.SHA256())
    )
    crl_path = tmp_path / "revoked.crl.pem"
    crl_path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    result = validate_cbsd_certificate(
        leaf,
        trust_roots=[ca_cert],
        crls=[x509.load_pem_x509_crl(crl_path.read_bytes())],
    )
    assert not result.ok
    assert result.reason is CertRejectReason.REVOKED


def test_validators_reject_dynamic_blacklist(ca_material):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=OID_ROLE_SAS
    )
    fp = sha1_fingerprint_colon(leaf)
    result = validate_admin_certificate(
        leaf, trust_roots=[ca_cert], blacklisted_fingerprints={fp}
    )
    assert not result.ok
    assert result.reason is CertRejectReason.BLACKLISTED


def test_require_admin_allows_testclient_without_tls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "test")
    from config import clear_settings_cache

    clear_settings_cache()
    req = MagicMock()
    req.scope = {}
    require_admin_certificate(req)  # does not raise


def test_require_admin_rejects_cbsd_role_under_tls(ca_material, monkeypatch: pytest.MonkeyPatch):
    ca_cert, ca_key = ca_material
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
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=OID_ROLE_CBSD
    )
    der = leaf.public_bytes(serialization.Encoding.DER)
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = der
    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object
    req = MagicMock()
    req.scope = {"transport": transport}
    with pytest.raises(HTTPException) as exc:
        require_admin_certificate(req)
    assert exc.value.status_code == 403


def test_require_admin_accepts_allowlisted_fingerprint_without_role_sas(
    ca_material, monkeypatch: pytest.MonkeyPatch
):
    """Harness admin leaves may lack ROLE_SAS; fingerprint allowlist authorizes them."""
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=OID_ROLE_CBSD
    )
    fp = sha1_fingerprint_colon(leaf)
    monkeypatch.setattr(
        "services.certificate_policy.load_runtime_trust_context",
        lambda: ([ca_cert], None),
    )
    monkeypatch.setattr(
        "services.certificate_policy.load_admin_allowed_fingerprints",
        lambda: {fp},
    )
    der = leaf.public_bytes(serialization.Encoding.DER)
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = der
    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object
    req = MagicMock()
    req.scope = {"transport": transport}
    require_admin_certificate(req)  # does not raise


def test_validate_admin_accepts_allowlisted_cbsd_role(ca_material):
    ca_cert, ca_key = ca_material
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue_leaf(
        issuer_cert=ca_cert, issuer_key=ca_key, subject_key=leaf_key, role_oid=OID_ROLE_CBSD
    )
    fp = sha1_fingerprint_colon(leaf)
    denied = validate_admin_certificate(leaf, trust_roots=[ca_cert], allowed_fingerprints=set())
    assert not denied.ok
    assert denied.reason is CertRejectReason.WRONG_ROLE
    allowed = validate_admin_certificate(
        leaf, trust_roots=[ca_cert], allowed_fingerprints={fp}
    )
    assert allowed.ok, allowed.reason


def test_load_admin_allowed_fingerprints_normalizes_and_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
):
    from config import get_settings
    from services.certificate_policy import load_admin_allowed_fingerprints

    monkeypatch.setenv(
        "SAS_ADMIN_CERT_SHA1",
        "5672624859c96704399136974a89ed19ac1d33f3, not-a-fp, AA:BB",
    )
    get_settings.cache_clear()
    try:
        fps = load_admin_allowed_fingerprints()
        assert fps == {"56:72:62:48:59:C9:67:04:39:91:36:97:4A:89:ED:19:AC:1D:33:F3"}
    finally:
        get_settings.cache_clear()


def test_load_runtime_trust_context_none_without_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from config import get_settings
    from services.certificate_policy import load_runtime_trust_context

    monkeypatch.setenv("CERTS_DIR", str(tmp_path / "missing-certs"))
    get_settings.cache_clear()
    try:
        roots, crls = load_runtime_trust_context()
        assert roots is None
        assert crls is None
    finally:
        get_settings.cache_clear()
