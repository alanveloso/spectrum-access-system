"""mTLS helpers for SAS↔SAS (v1.3) authorization."""

from __future__ import annotations

import hashlib
import logging
import ssl
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtensionOID, ObjectIdentifier
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models.models import PeerSas
from services import winnf_role_oids

# Canonical WInnForum role OIDs (re-exported for existing importers).
OID_ROLE_SAS = winnf_role_oids.OID_ROLE_SAS
OID_ROLE_INSTALLER = winnf_role_oids.OID_ROLE_INSTALLER
OID_ROLE_CBSD = winnf_role_oids.OID_ROLE_CBSD
OID_ROLE_DOMAIN_PROXY = winnf_role_oids.OID_ROLE_DOMAIN_PROXY
OID_ZONE = winnf_role_oids.OID_ZONE

logger = logging.getLogger(__name__)

# Ciphers allowed on the SAS↔SAS / CBSD interface (mirrors Fake SAS).
# ECDHE-RSA-AES256-GCM-SHA384 is intentionally excluded (SSS_14).
RSA_CIPHERS = [
    "AES128-GCM-SHA256",
    "AES256-GCM-SHA384",
    "ECDHE-RSA-AES128-GCM-SHA256",
]
ECC_CIPHERS = [
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
]
ALLOWED_CIPHERS = RSA_CIPHERS + ECC_CIPHERS
# SSS_14 and related negatives — must never be enabled on either listener.
FORBIDDEN_CIPHERS = [
    "ECDHE-RSA-AES256-GCM-SHA384",
]


def sha1_fingerprint_colon(cert: x509.Certificate) -> str:
    """SHA-1 fingerprint in OpenSSL digest format: ``AA:BB:CC:...`` (uppercase)."""
    der = cert.public_bytes(Encoding.DER)
    digest = hashlib.sha1(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def load_client_certificate(request: Request) -> Optional[x509.Certificate]:
    """Extract the peer (client) X.509 certificate from the TLS connection."""
    transport = request.scope.get("transport")
    if transport is None:
        return None
    try:
        ssl_object = transport.get_extra_info("ssl_object")
    except Exception:
        return None
    if ssl_object is None:
        return None
    try:
        der = ssl_object.getpeercert(binary_form=True)
    except Exception:
        return None
    if not der:
        return None
    try:
        return x509.load_der_x509_certificate(der)
    except Exception:
        logger.debug("Failed to parse client certificate", exc_info=True)
        return None


def certificate_policy_oids(cert: x509.Certificate) -> set[ObjectIdentifier]:
    """Return certificatePolicies OIDs present on ``cert`` (empty if missing)."""
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.CERTIFICATE_POLICIES)
    except x509.ExtensionNotFound:
        return set()
    value = ext.value
    if not isinstance(value, x509.CertificatePolicies):
        return set()
    oids: set[ObjectIdentifier] = set()
    for policy in value:
        oids.add(policy.policy_identifier)
    return oids


def _policy_oids(cert: x509.Certificate) -> set[ObjectIdentifier]:
    """Backward-compatible alias for ``certificate_policy_oids``."""
    return certificate_policy_oids(cert)


def is_valid_sas_client_certificate(cert: x509.Certificate) -> bool:
    """
    Application-level SAS client cert checks for SSS_10 / SSS_15.

    Delegates to :func:`services.certificate_policy.validate_sas_certificate`
    (ROLE_SAS, clientAuth, temporal, key usage/type, no ZONE, CA/CRL when configured).
    """
    from services.certificate_policy import (
        load_runtime_trust_context,
        validate_sas_certificate,
    )

    trust_roots, crls = load_runtime_trust_context()
    return validate_sas_certificate(cert, trust_roots=trust_roots, crls=crls).ok


def require_admin_certificate(request: Request) -> None:
    """FastAPI dependency: Admin API requires ROLE_SAS (or allowlisted) client certs.

    Explicit ``SAS_EXECUTION_MODE=test`` may omit TLS entirely for in-process
    TestClient. A TLS channel without a peer certificate is always denied.
    Certification and production also deny plain non-TLS requests.
    """
    from services.certificate_policy import (
        load_runtime_trust_context,
        validate_admin_certificate,
    )
    from services.transport_auth_policy import (
        allows_non_tls_clients,
        deny_missing_client_certificate,
        request_has_tls_channel,
    )

    cert = load_client_certificate(request)
    if cert is None:
        if request_has_tls_channel(request) or not allows_non_tls_clients():
            deny_missing_client_certificate(boundary="admin")
            raise HTTPException(status_code=403, detail="Client certificate required")
        return

    trust_roots, crls = load_runtime_trust_context()
    if not validate_admin_certificate(cert, trust_roots=trust_roots, crls=crls).ok:
        logger.info("mtls_denied boundary=admin reason=certificate_role_invalid_or_untrusted")
        raise HTTPException(status_code=403, detail="Invalid admin client certificate")


def require_peer_sas(
    request: Request,
    db: Session = Depends(get_db),
) -> str:
    """
 FastAPI dependency: authorize SAS↔SAS access via mTLS peer fingerprint.

 Returns the authorized certificate hash, or raises HTTP 403.
 """
    cert = load_client_certificate(request)
    if cert is None:
        raise HTTPException(status_code=403, detail="Client certificate required")

    if not is_valid_sas_client_certificate(cert):
        raise HTTPException(status_code=403, detail="Invalid SAS client certificate")

    cert_hash = sha1_fingerprint_colon(cert)
    peer = db.query(PeerSas).filter_by(certificate_hash=cert_hash).first()
    if peer is None:
        # Harness may store lower-case; compare case-insensitively.
        peer = (
            db.query(PeerSas)
            .filter(PeerSas.certificate_hash.ilike(cert_hash))
            .first()
        )
    if peer is None:
        raise HTTPException(status_code=403, detail="Peer SAS not authorized")
    return cert_hash


def _patch_request_response_cycle(module_path: str, *, transport_arg: str) -> bool:
    """Patch a uvicorn HTTP ``RequestResponseCycle`` so ``scope['transport']`` is set.

 ``uvicorn[standard]`` defaults to httptools; the historical h11-only patch left
 mTLS fingerprint binding inert under the default HTTP implementation.
 """
    try:
        import importlib

        module = importlib.import_module(module_path)
        cycle_cls = module.RequestResponseCycle
    except Exception:
        logger.warning("Could not import %s for client cert access", module_path)
        return False

    if getattr(cycle_cls.__init__, "_sas_mtls_patched", False):
        return True

    original_init = cycle_cls.__init__

    def patched_init(self, *args, **kwargs):
        scope = args[0] if args else kwargs.get("scope")
        transport = kwargs.get("transport")
        if transport is None:
            # h11: (scope, conn, transport, ...); httptools: (scope, transport, ...)
            if transport_arg == "h11" and len(args) >= 3:
                transport = args[2]
            elif transport_arg == "httptools" and len(args) >= 2:
                transport = args[1]
        if transport is not None and isinstance(scope, dict):
            scope["transport"] = transport
        return original_init(self, *args, **kwargs)

    patched_init._sas_mtls_patched = True  # type: ignore[attr-defined]
    cycle_cls.__init__ = patched_init  # type: ignore[method-assign]
    logger.info("Patched %s.RequestResponseCycle for mTLS client cert access", module_path)
    return True


def patch_uvicorn_for_client_cert() -> None:
    """Expose the asyncio transport on the ASGI scope (uvicorn does not by default)."""
    patched_any = False
    patched_any |= _patch_request_response_cycle(
        "uvicorn.protocols.http.h11_impl", transport_arg="h11"
    )
    patched_any |= _patch_request_response_cycle(
        "uvicorn.protocols.http.httptools_impl", transport_arg="httptools"
    )
    if not patched_any:
        logger.warning("Could not patch uvicorn HTTP implementations for client cert access")


def _load_crl_pems(crl_dir: Path, ctx: ssl.SSLContext) -> None:
    """Load PEM-encoded CRLs so revoked / blacklisted certs fail the handshake."""
    if not crl_dir.is_dir():
        return
    loaded = 0
    for pem in sorted(crl_dir.glob("*.crl.pem")):
        try:
            ctx.load_verify_locations(cafile=str(pem))
            loaded += 1
        except ssl.SSLError as exc:
            logger.warning("Skipping CRL %s: %s", pem.name, exc)
    if loaded:
        ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_CHAIN
        logger.info("Loaded %d CRL PEM file(s) from %s", loaded, crl_dir)


def create_mtls_ssl_context(
    *,
    certfile: Path,
    keyfile: Path,
    ca_certs: Path,
    crl_dir: Path | None = None,
    ciphers: list[str] | None = None,
) -> ssl.SSLContext:
    """
 Build a TLS 1.2-only server context with client-certificate verification (mTLS).

 Mirrors Fake SAS: CERT_REQUIRED, WInnForum CA, restricted cipher list.
 Rejects any cipher listed in ``FORBIDDEN_CIPHERS`` (e.g. SSS_14).
 """
    selected = list(ciphers) if ciphers is not None else list(RSA_CIPHERS)
    blocked = [name for name in selected if name in FORBIDDEN_CIPHERS]
    if blocked:
        raise ValueError(
            "forbidden TLS cipher(s) requested for mTLS context: "
            + ", ".join(blocked)
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_certs))
    if crl_dir is not None:
        _load_crl_pems(crl_dir, ctx)
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    ctx.set_ciphers(":".join(selected))
    return ctx
