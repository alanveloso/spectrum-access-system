"""Central transport/auth policy for mTLS fail-closed behavior.

Security decisions consume the configured ``SAS_EXECUTION_MODE`` only.
Absence of an ASGI TLS ``ssl_object`` must not imply a trusted test client
unless the mode is explicitly ``test``.
"""

from __future__ import annotations

import logging
from typing import Literal

from config import get_settings

logger = logging.getLogger(__name__)

ExecutionMode = Literal["production", "certification", "test"]


def current_execution_mode() -> str:
    """Resolved execution mode from Settings (startup/config authority)."""
    return str(get_settings().sas_execution_mode)


def allows_non_tls_clients() -> bool:
    """True only for explicit local/in-process test execution."""
    return current_execution_mode() == "test"


def request_has_tls_channel(request) -> bool:
    """True when the ASGI scope exposes a TLS ssl_object (mTLS-capable channel)."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return False
    transport = scope.get("transport")
    if transport is None:
        return False
    try:
        return transport.get_extra_info("ssl_object") is not None
    except Exception:
        return False


def deny_missing_client_certificate(*, boundary: str) -> None:
    """Log a structured deny for missing TLS/client certificate."""
    mode = current_execution_mode()
    logger.info(
        "mtls_denied boundary=%s reason=client_certificate_missing mode=%s",
        boundary,
        mode,
    )
