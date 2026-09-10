"""Shared egress / SSRF guards for Admin-injected HTTP pulls.

FAD peer pulls keep their same-origin / resolved-IP checks in
``fad_client_service``. This module covers generic https URLs (federal DB sync)
and structural peer-URL checks at ``InjectPeerSas`` time (no DNS at inject).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_LITERAL_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)


class SsrfError(ValueError):
    """URL rejected by SSRF controls."""


def assert_peer_sas_inject_url_structurally_allowed(url: str) -> None:
    """Validate peer base URL shape for Admin InjectPeerSas (no DNS / no connect).

 Peer identity registration must not depend on resolving the peer URL.
 DNS and resolved-IP SSRF remain fail-closed on outbound FAD pulls
 (``fad_client_service.assert_url_allowed_for_peer``).
 """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise SsrfError(f"url must be https, got scheme={parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise SsrfError("url must not include userinfo credentials")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SsrfError("url missing host")
    if host in _BLOCKED_LITERAL_HOSTS:
        raise SsrfError(f"host blocked: {host}")


def _host_allows_loopback_or_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or ip.is_private)
    except ValueError:
        return host in {"localhost", "localhost.localdomain"}


def _ip_blocked(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_lab: bool
) -> bool:
    if str(ip) in {"169.254.169.254", "169.254.169.253"}:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip.is_link_local:
        return not allow_lab
    if ip.is_loopback:
        return not allow_lab
    if ip.is_private:
        return not allow_lab
    if ip.is_reserved:
        return True
    return False


def allow_lab_private_egress() -> bool:
    """Whether loopback/RFC1918 egress is permitted for Admin/WDB pulls.

 Enabled only when ``SAS_EXECUTION_MODE=certification`` (harness lab) or when
 ``SAS_SSRF_ALLOW_LAB_PRIVATE`` is explicitly true. Production defaults to
 fail-closed.
 """
    from config import get_settings

    settings = get_settings()
    if settings.sas_execution_mode == "certification":
        return True
    return bool(settings.sas_ssrf_allow_lab_private)


def assert_https_egress_url_allowed(
    url: str, *, allow_lab_private: bool = False
) -> None:
    """Reject non-https, userinfo, metadata hosts, and blocked resolved IPs.

 Fail-closed by default: loopback/RFC1918/link-local are blocked.
 Pass ``allow_lab_private=True`` only for explicit lab/harness inject paths
 where the hostname itself is loopback/private.
 """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise SsrfError(f"url must be https, got scheme={parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise SsrfError("url must not include userinfo credentials")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SsrfError("url missing host")
    if host in _BLOCKED_LITERAL_HOSTS:
        raise SsrfError(f"host blocked: {host}")

    allow_lab = bool(allow_lab_private and _host_allows_loopback_or_private(host))
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SsrfError(f"DNS resolution failed: {host}") from exc
    if not infos:
        raise SsrfError(f"DNS resolution empty: {host}")
    seen = False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        seen = True
        if _ip_blocked(ip, allow_lab=allow_lab):
            raise SsrfError(f"host resolves to blocked address {ip} (host={host})")
    if not seen:
        raise SsrfError(f"DNS produced no IPs: {host}")
