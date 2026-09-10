"""mTLS readiness probe against the UUT Admin API."""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HealthcheckResult:
    ok: bool
    detail: str
    attempts: int


def _ssl_context(
    *,
    ca_certs: Path,
    client_cert: Path,
    client_key: Path,
) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(ca_certs))
    ctx.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
    return ctx


def wait_for_mtls_admin(
    *,
    base_url: str,
    ca_certs: Path,
    client_cert: Path,
    client_key: Path,
    path: str = "/admin/get_daily_activities_status",
    timeout_seconds: float = 60.0,
    interval_seconds: float = 1.0,
) -> HealthcheckResult:
    """POST an empty JSON body to an explicit Admin endpoint until TLS+HTTP succeed.

 HTTP 4xx from the application still counts as reachable (TLS/mTLS OK).
 Connection/TLS failures are retried until timeout.
 """
    url = base_url.rstrip("/") + path
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    last = "not attempted"
    ctx = _ssl_context(
        ca_certs=ca_certs, client_cert=client_cert, client_key=client_key
    )
    while time.monotonic() < deadline:
        attempts += 1
        try:
            req = Request(
                url,
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, context=ctx, timeout=5) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                return HealthcheckResult(
                    ok=True,
                    detail=f"HTTP {code} from {url}",
                    attempts=attempts,
                )
        except HTTPError as exc:
            # Application responded over mTLS.
            return HealthcheckResult(
                ok=True,
                detail=f"HTTP {exc.code} from {url}",
                attempts=attempts,
            )
        except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(interval_seconds)
    return HealthcheckResult(
        ok=False,
        detail=f"timeout after {attempts} attempts; last={last}",
        attempts=attempts,
    )
