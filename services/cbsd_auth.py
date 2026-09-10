"""CBSD-SAS mTLS identity binding (fingerprint + WInnForum role).

Authorizes CBSD and Domain Proxy (OPERATOR) client certificates for the
CBSD↔SAS interface and binds subsequent ``cbsdId`` operations to the
fingerprint stored at registration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cryptography import x509
from fastapi import Request

from models.models import Cbsd
from services.certificate_policy import (
    load_runtime_trust_context,
    validate_cbsd_certificate,
    validate_domain_proxy_certificate,
)
from services.error_handlers import CERT_ERROR
from services.mtls_auth import (
    OID_ROLE_CBSD,
    OID_ROLE_DOMAIN_PROXY,
    OID_ROLE_INSTALLER,
    OID_ROLE_SAS,
    certificate_policy_oids,
    load_client_certificate,
    sha1_fingerprint_colon,
)

# Roles permitted on the CBSD↔SAS application API.
_CBSD_API_ROLES = frozenset({"cbsd", "domain_proxy"})


class CbsdClientRole(str, Enum):
    ABSENT = "absent"
    CBSD = "cbsd"
    DOMAIN_PROXY = "domain_proxy"
    SAS = "sas"
    INSTALLER = "installer"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CbsdAuthContext:
    """Resolved client identity for one CBSD↔SAS request."""

    certificate_hash: str | None
    role: CbsdClientRole
    allowed: bool
    denial_code: int | None = None


def classify_cbsd_client_role(cert: x509.Certificate) -> CbsdClientRole:
    """Map certificate policy OIDs to a CBSD-API role.

 Domain Proxy is the OPERATOR role (1.3.6.1.4.1.46609.1.1.4). When multiple
 role policies appear, prefer Domain Proxy, then CBSD, then SAS/Installer.
 """
    policies = certificate_policy_oids(cert)
    if OID_ROLE_DOMAIN_PROXY in policies:
        return CbsdClientRole.DOMAIN_PROXY
    if OID_ROLE_CBSD in policies:
        return CbsdClientRole.CBSD
    if OID_ROLE_SAS in policies:
        return CbsdClientRole.SAS
    if OID_ROLE_INSTALLER in policies:
        return CbsdClientRole.INSTALLER
    return CbsdClientRole.UNKNOWN


def _role_certificate_ok(cert: x509.Certificate, role: CbsdClientRole) -> bool:
    """Apply leaf policy for the classified CBSD-API role."""
    trust_roots, crls = load_runtime_trust_context()
    if role is CbsdClientRole.CBSD:
        return validate_cbsd_certificate(
            cert, trust_roots=trust_roots, crls=crls
        ).ok
    if role is CbsdClientRole.DOMAIN_PROXY:
        return validate_domain_proxy_certificate(
            cert, trust_roots=trust_roots, crls=crls
        ).ok
    return False


def authorize_cbsd_operation(request: Request) -> CbsdAuthContext:
    """Extract peer certificate, classify role, and decide CBSD-API access.

    Explicit ``SAS_EXECUTION_MODE=test`` may omit TLS entirely for in-process
    TestClient (null fingerprint, role ABSENT, allowed). If a TLS transport is
    present without a usable peer certificate, access is denied (``104``) in
    every mode. Certification and production also deny plain non-TLS requests.
    """
    from services.transport_auth_policy import (
        allows_non_tls_clients,
        deny_missing_client_certificate,
        request_has_tls_channel,
    )

    cert = load_client_certificate(request)
    if cert is None:
        if request_has_tls_channel(request):
            deny_missing_client_certificate(boundary="cbsd")
            return CbsdAuthContext(
                certificate_hash=None,
                role=CbsdClientRole.ABSENT,
                allowed=False,
                denial_code=CERT_ERROR,
            )
        if allows_non_tls_clients():
            return CbsdAuthContext(
                certificate_hash=None,
                role=CbsdClientRole.ABSENT,
                allowed=True,
            )
        deny_missing_client_certificate(boundary="cbsd")
        return CbsdAuthContext(
            certificate_hash=None,
            role=CbsdClientRole.ABSENT,
            allowed=False,
            denial_code=CERT_ERROR,
        )

    role = classify_cbsd_client_role(cert)
    cert_hash = sha1_fingerprint_colon(cert)
    if role.value not in _CBSD_API_ROLES or not _role_certificate_ok(cert, role):
        return CbsdAuthContext(
            certificate_hash=cert_hash,
            role=role,
            allowed=False,
            denial_code=CERT_ERROR,
        )
    return CbsdAuthContext(
        certificate_hash=cert_hash,
        role=role,
        allowed=True,
    )


def cbsd_certificate_mismatch(
    cbsd: Cbsd, certificate_hash: str | None
) -> bool:
    """True when the request cert does not own the CBSD that holds ``cbsdId``.

 Legacy rows without a stored fingerprint are not rejected (no binding yet).
 Domain Proxy and CBSD share the same rule: the fingerprint stored at
 registration must match the peer fingerprint for SIQ/GRA/HBT/RLQ/DRG
 (and for re-registration takeover prevention).
 """
    stored = cbsd.certificate_hash
    if not stored:
        return False
    if not certificate_hash:
        return True
    return stored.upper() != certificate_hash.upper()
