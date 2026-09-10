"""Certificate / CRL trust material inspection and rotation notes.

App-layer validators call :func:`load_runtime_trust_context` per request and
therefore pick up replaced ``*.crl.pem`` files without restart.

OpenSSL handshake CRL (``VERIFY_CRL_CHECK_CHAIN``) is bound when the TLS
listener starts; replacing server/CA/CRL for the *handshake* requires a
process restart (document in ops runbooks).

OCSP: the selected WInnForum certification target for this UUT uses PEM CRL
files. OCSP checking remains opt-in via ``SAS_SSL_OCSP_MODE`` and is disabled
by default (no network dependency in the harness path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from services.certificate_policy import load_crl_files, load_runtime_trust_context


def trust_material_status() -> dict[str, Any]:
    """Return current on-disk CA/CRL visibility for Admin ops."""
    settings = get_settings()
    crl_dir = Path(settings.resolved_ssl_crl_dir).expanduser()
    ca_path = Path(settings.resolved_ssl_ca_certs).expanduser()
    crl_files = sorted(crl_dir.glob("*.crl.pem")) if crl_dir.is_dir() else []
    roots, crls = load_runtime_trust_context()
    return {
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "caPath": str(ca_path),
        "caPresent": ca_path.is_file(),
        "crlDir": str(crl_dir),
        "crlFiles": [p.name for p in crl_files],
        "crlCountLoaded": len(crls or []),
        "trustRootCount": len(roots or []),
        "ocspMode": settings.sas_ssl_ocsp_mode,
        "notes": [
            "App-layer CRL is re-read from disk on each mTLS validation.",
            "TLS handshake CRL/context requires process restart after file replace.",
            "OCSP is disabled by default for the WInnForum CRL-based target.",
        ],
    }


def reload_trust_material() -> dict[str, Any]:
    """Verify CA/CRL files load; returns the same shape as :func:`trust_material_status`."""
    settings = get_settings()
 # Force a load attempt (raises only on unreadable PEM content inside loader,
 # which skips bad files — still report counts).
    _ = load_crl_files(Path(settings.resolved_ssl_crl_dir).expanduser())
    return trust_material_status()
