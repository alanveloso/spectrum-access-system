"""Role-specific X.509 validators for WInnForum CBSD / Domain Proxy / SAS / Admin.

Implements the application-level checks required by (WINNF-TS-0022 policy
OIDs, EKU, key usage, temporal window, key type, optional chain/CRL/blacklist).
TLS handshake remains the first line of defense; these validators are defense in
depth and role gating for application APIs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AbstractSet, Iterable, Sequence

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, ObjectIdentifier

from services.mtls_auth import (
    OID_ROLE_CBSD,
    OID_ROLE_DOMAIN_PROXY,
    OID_ROLE_SAS,
    OID_ZONE,
    certificate_policy_oids,
    sha1_fingerprint_colon,
)

logger = logging.getLogger(__name__)


class CertRejectReason(str, Enum):
    """Machine-readable reject codes (never leaked to protocol clients)."""

    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    WRONG_ROLE = "wrong_role"
    ZONE_POLICY = "zone_policy"
    MISSING_EKU = "missing_eku"
    BAD_KEY_USAGE = "bad_key_usage"
    WRONG_KEY_TYPE = "wrong_key_type"
    IS_CA = "is_ca"
    UNTRUSTED_CHAIN = "untrusted_chain"
    REVOKED = "revoked"
    BLACKLISTED = "blacklisted"


@dataclass(frozen=True)
class CertValidationResult:
    ok: bool
    reason: CertRejectReason | None = None


def _utc_now(now: datetime | None) -> datetime:
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def _not_before(cert: x509.Certificate) -> datetime:
    try:
        return cert.not_valid_before_utc
    except AttributeError:
        return cert.not_valid_before.replace(tzinfo=timezone.utc)


def _not_after(cert: x509.Certificate) -> datetime:
    try:
        return cert.not_valid_after_utc
    except AttributeError:
        return cert.not_valid_after.replace(tzinfo=timezone.utc)


def _check_temporal(cert: x509.Certificate, now: datetime) -> CertRejectReason | None:
    if now < _not_before(cert):
        return CertRejectReason.NOT_YET_VALID
    if now > _not_after(cert):
        return CertRejectReason.EXPIRED
    return None


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        bc = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS)
    except x509.ExtensionNotFound:
        return False
    value = bc.value
    if not isinstance(value, x509.BasicConstraints):
        return False
    return bool(value.ca)


def _has_client_auth(cert: x509.Certificate) -> bool:
    try:
        eku = cert.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE)
    except x509.ExtensionNotFound:
        return False
    value = eku.value
    if not isinstance(value, x509.ExtendedKeyUsage):
        return False
    return ExtendedKeyUsageOID.CLIENT_AUTH in value


def _has_digital_signature(cert: x509.Certificate) -> bool:
    try:
        ku = cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE)
    except x509.ExtensionNotFound:
        # Harness leaves always carry KeyUsage; absence is treated as failure.
        return False
    value = ku.value
    if not isinstance(value, x509.KeyUsage):
        return False
    return bool(value.digital_signature)


def _key_type_allowed(cert: x509.Certificate) -> bool:
    public_key = cert.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        return public_key.key_size >= 2048
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return isinstance(public_key.curve, ec.SECP256R1)
    # Explicitly reject DSA / Ed25519 for CBRS client roles.
    if isinstance(public_key, (dsa.DSAPublicKey, ed25519.Ed25519PublicKey)):
        return False
    return False


def _load_pem_certificates(paths: Sequence[Path]) -> list[x509.Certificate]:
    certs: list[x509.Certificate] = []
    for path in paths:
        data = path.read_bytes()
        # Support single- or multi-PEM files.
        begin = b"-----BEGIN CERTIFICATE-----"
        if begin not in data:
            continue
        parts = data.split(begin)
        for part in parts[1:]:
            pem = begin + part.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
            try:
                certs.append(x509.load_pem_x509_certificate(pem))
            except ValueError:
                logger.info("Skipping unreadable PEM certificate in %s", path)
    return certs


def _load_crls(paths: Sequence[Path]) -> list[x509.CertificateRevocationList]:
    crls: list[x509.CertificateRevocationList] = []
    for path in paths:
        try:
            crls.append(x509.load_pem_x509_crl(path.read_bytes()))
        except ValueError:
            logger.info("Skipping unreadable CRL %s", path)
    return crls


def _check_revoked(
    cert: x509.Certificate, crls: Sequence[x509.CertificateRevocationList]
) -> bool:
    serial = cert.serial_number
    for crl in crls:
        if crl.get_revoked_certificate_by_serial_number(serial) is not None:
            return True
    return False


def _verify_signature(cert: x509.Certificate, issuer_public_key) -> bool:
    """Verify ``cert`` was signed by ``issuer_public_key`` (RSA/ECDSA)."""
    try:
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric import ec as ec_mod
        from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

        tbs = cert.tbs_certificate_bytes
        signature = cert.signature
        hash_algo = cert.signature_hash_algorithm
        if hash_algo is None:
            return False
        if isinstance(issuer_public_key, rsa_mod.RSAPublicKey):
            issuer_public_key.verify(signature, tbs, padding.PKCS1v15(), hash_algo)
            return True
        if isinstance(issuer_public_key, ec_mod.EllipticCurvePublicKey):
            issuer_public_key.verify(signature, tbs, ec_mod.ECDSA(hash_algo))
            return True
    except Exception:
        return False
    return False


def _check_chain(
    cert: x509.Certificate,
    *,
    trust_roots: Sequence[x509.Certificate],
    intermediates: Sequence[x509.Certificate],
) -> bool:
    """Validate leaf chains to a trusted root (signature + issuer name + CA bit).

    Accepts an unordered intermediate set. Path length is bounded; each issuer
    must present BasicConstraints CA. Trust anchors are ``trust_roots`` only —
    intermediates never become roots by themselves.
    """
    if not trust_roots:
        return True

    max_depth = 8
    current = cert
    pool = list(intermediates)
    for _ in range(max_depth):
        for root in trust_roots:
            if current.issuer != root.subject:
                continue
            if not _is_ca(root):
                continue
            if _verify_signature(current, root.public_key()):
                return True
        matched_idx: int | None = None
        next_cert: x509.Certificate | None = None
        for idx, intermediate in enumerate(pool):
            if current.issuer != intermediate.subject:
                continue
            if not _is_ca(intermediate):
                continue
            if not _verify_signature(current, intermediate.public_key()):
                continue
            matched_idx = idx
            next_cert = intermediate
            break
        if matched_idx is None or next_cert is None:
            return False
        pool.pop(matched_idx)
        current = next_cert
    return False


def _validate_leaf(
    cert: x509.Certificate,
    *,
    required_role: ObjectIdentifier,
    now: datetime | None = None,
    trust_roots: Sequence[x509.Certificate] | None = None,
    intermediates: Sequence[x509.Certificate] | None = None,
    crls: Sequence[x509.CertificateRevocationList] | None = None,
    blacklisted_fingerprints: AbstractSet[str] | None = None,
    allowed_fingerprints: AbstractSet[str] | None = None,
) -> CertValidationResult:
    """Shared leaf checks + role / ZONE policy gate.

    When ``allowed_fingerprints`` is set, a matching SHA-1 fingerprint satisfies
    the role gate even if ``required_role`` is absent (used for Admin clients
    provisioned without ROLE_SAS, e.g. harness admin leaves).
    """
    clock = _utc_now(now)

    temporal = _check_temporal(cert, clock)
    if temporal is not None:
        return CertValidationResult(ok=False, reason=temporal)

    if _is_ca(cert):
        return CertValidationResult(ok=False, reason=CertRejectReason.IS_CA)

    if not _key_type_allowed(cert):
        return CertValidationResult(ok=False, reason=CertRejectReason.WRONG_KEY_TYPE)

    if not _has_client_auth(cert):
        return CertValidationResult(ok=False, reason=CertRejectReason.MISSING_EKU)

    if not _has_digital_signature(cert):
        return CertValidationResult(ok=False, reason=CertRejectReason.BAD_KEY_USAGE)

    policies = certificate_policy_oids(cert)
    if OID_ZONE in policies:
        return CertValidationResult(ok=False, reason=CertRejectReason.ZONE_POLICY)

    fingerprint = sha1_fingerprint_colon(cert).upper()
    role_ok = required_role in policies
    fp_allow = False
    if allowed_fingerprints:
        fp_allow = fingerprint in {item.upper() for item in allowed_fingerprints}
    if not role_ok and not fp_allow:
        return CertValidationResult(ok=False, reason=CertRejectReason.WRONG_ROLE)

    if blacklisted_fingerprints:
        normalized = {item.upper() for item in blacklisted_fingerprints}
        if fingerprint in normalized:
            return CertValidationResult(ok=False, reason=CertRejectReason.BLACKLISTED)

    if crls and _check_revoked(cert, crls):
        return CertValidationResult(ok=False, reason=CertRejectReason.REVOKED)

    if trust_roots is not None:
        if not _check_chain(
            cert,
            trust_roots=trust_roots,
            intermediates=intermediates or (),
        ):
            return CertValidationResult(ok=False, reason=CertRejectReason.UNTRUSTED_CHAIN)

    return CertValidationResult(ok=True)


def validate_cbsd_certificate(
    cert: x509.Certificate,
    *,
    now: datetime | None = None,
    trust_roots: Sequence[x509.Certificate] | None = None,
    intermediates: Sequence[x509.Certificate] | None = None,
    crls: Sequence[x509.CertificateRevocationList] | None = None,
    blacklisted_fingerprints: AbstractSet[str] | None = None,
) -> CertValidationResult:
    """Validate a CBSD client certificate (ROLE_CBSD)."""
    return _validate_leaf(
        cert,
        required_role=OID_ROLE_CBSD,
        now=now,
        trust_roots=trust_roots,
        intermediates=intermediates,
        crls=crls,
        blacklisted_fingerprints=blacklisted_fingerprints,
    )


def validate_domain_proxy_certificate(
    cert: x509.Certificate,
    *,
    now: datetime | None = None,
    trust_roots: Sequence[x509.Certificate] | None = None,
    intermediates: Sequence[x509.Certificate] | None = None,
    crls: Sequence[x509.CertificateRevocationList] | None = None,
    blacklisted_fingerprints: AbstractSet[str] | None = None,
) -> CertValidationResult:
    """Validate a Domain Proxy / OPERATOR client certificate (ROLE_OPERATOR)."""
    return _validate_leaf(
        cert,
        required_role=OID_ROLE_DOMAIN_PROXY,
        now=now,
        trust_roots=trust_roots,
        intermediates=intermediates,
        crls=crls,
        blacklisted_fingerprints=blacklisted_fingerprints,
    )


def validate_sas_certificate(
    cert: x509.Certificate,
    *,
    now: datetime | None = None,
    trust_roots: Sequence[x509.Certificate] | None = None,
    intermediates: Sequence[x509.Certificate] | None = None,
    crls: Sequence[x509.CertificateRevocationList] | None = None,
    blacklisted_fingerprints: AbstractSet[str] | None = None,
) -> CertValidationResult:
    """Validate a SAS↔SAS client certificate (ROLE_SAS; no ZONE)."""
    return _validate_leaf(
        cert,
        required_role=OID_ROLE_SAS,
        now=now,
        trust_roots=trust_roots,
        intermediates=intermediates,
        crls=crls,
        blacklisted_fingerprints=blacklisted_fingerprints,
    )


def validate_admin_certificate(
    cert: x509.Certificate,
    *,
    now: datetime | None = None,
    trust_roots: Sequence[x509.Certificate] | None = None,
    intermediates: Sequence[x509.Certificate] | None = None,
    crls: Sequence[x509.CertificateRevocationList] | None = None,
    blacklisted_fingerprints: AbstractSet[str] | None = None,
    allowed_fingerprints: AbstractSet[str] | None = None,
) -> CertValidationResult:
    """Validate an Admin API client certificate.

 Prefer ROLE_SAS (no dedicated Admin OID in WINNF-TS-0022). Additional SHA-1
 fingerprints from ``allowed_fingerprints`` or ``SAS_ADMIN_CERT_SHA1`` may
 authorize a leaf that passes PKI hygiene without ROLE_SAS (harness admin
 clients are often issued under another role OID).
 """
    fingerprints = allowed_fingerprints
    if fingerprints is None:
        fingerprints = load_admin_allowed_fingerprints() or None
    return _validate_leaf(
        cert,
        required_role=OID_ROLE_SAS,
        now=now,
        trust_roots=trust_roots,
        intermediates=intermediates,
        crls=crls,
        blacklisted_fingerprints=blacklisted_fingerprints,
        allowed_fingerprints=fingerprints,
    )


def load_admin_allowed_fingerprints() -> set[str]:
    """Parse ``SAS_ADMIN_CERT_SHA1`` (comma-separated ``AA:BB:...`` digests)."""
    from config import get_settings

    raw = (get_settings().sas_admin_cert_sha1 or "").strip()
    if not raw:
        return set()
    out: set[str] = set()
    for part in raw.split(","):
        token = part.strip().upper().replace(" ", "")
        if not token:
            continue
        hex_digits = token.replace(":", "")
        if len(hex_digits) != 40 or any(c not in "0123456789ABCDEF" for c in hex_digits):
            logger.warning("Ignoring invalid SAS_ADMIN_CERT_SHA1 token")
            continue
        # Canonical colon form for comparison with sha1_fingerprint_colon().
        canon = ":".join(hex_digits[i : i + 2] for i in range(0, 40, 2))
        out.add(canon)
    return out


def fingerprint_from_cert_pem(path: Path) -> str:
    """Return colon-hex SHA-1 fingerprint for a PEM certificate file."""
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    return sha1_fingerprint_colon(cert)


def load_trust_roots(paths: Iterable[Path]) -> list[x509.Certificate]:
    """Load trusted CA PEMs from paths (files or directories of ``*.cert``/``*.pem``)."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.cert")))
            files.extend(sorted(path.glob("*.pem")))
        elif path.is_file():
            files.append(path)
    return _load_pem_certificates(files)


def load_crl_files(crl_dir: Path | None) -> list[x509.CertificateRevocationList]:
    if crl_dir is None or not crl_dir.is_dir():
        return []
    return _load_crls(sorted(crl_dir.glob("*.crl.pem")))


def load_runtime_trust_context() -> tuple[
    list[x509.Certificate] | None,
    list[x509.CertificateRevocationList] | None,
]:
    """Load CA (+ CRL) from settings when present for app-layer chain checks.

    Returns ``(None, None)`` when the configured CA file is absent so local
    TestClient suites without ``./certs`` keep working. When the CA file exists,
    callers must pass the roots into validators (fail closed on untrusted leaves).
    """
    from config import get_settings

    settings = get_settings()
    ca_path = Path(settings.resolved_ssl_ca_certs).expanduser()
    if not ca_path.is_file():
        return None, None
    roots = load_trust_roots([ca_path])
    if not roots:
        return None, None
    crls = load_crl_files(Path(settings.resolved_ssl_crl_dir).expanduser())
    return roots, crls
