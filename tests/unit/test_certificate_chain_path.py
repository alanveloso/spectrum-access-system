"""Application-layer certificate path building (root → intermediate → leaf)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from services.certificate_policy import CertRejectReason, validate_cbsd_certificate
from services.mtls_auth import OID_ROLE_CBSD


def _ca(*, cn: str, issuer_cert=None, issuer_key=None, path_length=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    if issuer_cert is None:
        issuer_name = subject
        signer = key
    else:
        issuer_name = issuer_cert.subject
        signer = issuer_key
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length),
            critical=True,
        )
        .sign(signer, hashes.SHA256())
    )
    return cert, key


def _leaf(*, issuer_cert, issuer_key):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "device-leaf")])
        )
        .issuer_name(issuer_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.CertificatePolicies(
                [x509.PolicyInformation(OID_ROLE_CBSD, policy_qualifiers=None)]
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
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )
    return cert


def test_root_signed_leaf_trusted():
    root, root_key = _ca(cn="root")
    leaf = _leaf(issuer_cert=root, issuer_key=root_key)
    result = validate_cbsd_certificate(leaf, trust_roots=[root])
    assert result.ok


def test_intermediate_path_requires_intermediate_argument():
    root, root_key = _ca(cn="root", path_length=1)
    intermediate, int_key = _ca(
        cn="intermediate", issuer_cert=root, issuer_key=root_key, path_length=0
    )
    leaf = _leaf(issuer_cert=intermediate, issuer_key=int_key)

    missing = validate_cbsd_certificate(leaf, trust_roots=[root], intermediates=())
    assert not missing.ok
    assert missing.reason is CertRejectReason.UNTRUSTED_CHAIN

    ok = validate_cbsd_certificate(
        leaf, trust_roots=[root], intermediates=[intermediate]
    )
    assert ok.ok


def test_untrusted_root_denied():
    root_a, _ = _ca(cn="root-a")
    root_b, root_b_key = _ca(cn="root-b")
    leaf = _leaf(issuer_cert=root_b, issuer_key=root_b_key)
    result = validate_cbsd_certificate(leaf, trust_roots=[root_a])
    assert not result.ok
    assert result.reason is CertRejectReason.UNTRUSTED_CHAIN
