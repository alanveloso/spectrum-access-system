"""Secure SAS↔SAS Full Activity Dump client.

Fetches peer FADs with:

- hostname verification enabled by default;
- peer URL allowlist (only ``PeerSas`` rows from admin injection);
- SSRF controls (scheme, same-origin files, blocked resolved IPs, no open redirects);
 - checksum / size / version / schema validation before persistence;
- atomic replace of peer records per generation (purge absent IDs);
- previous snapshot preserved when fetch or validation fails;
- omitted empty ``recordType`` entries allowed (peer/harness may skip
 empty coordination dumps; absent type ≡ empty set).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from config import get_settings
from models.models import PeerFadRecord, PeerSas
from services.mtls_auth import ALLOWED_CIPHERS

logger = logging.getLogger(__name__)

RECORD_TYPES = frozenset({"cbsd", "zone", "esc_sensor", "coordination"})
_BLOCKED_LITERAL_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)


class FadClientError(Exception):
    """Raised when a peer FAD cannot be fetched or validated safely."""


@dataclass(frozen=True)
class FadFilePayload:
    record_type: str
    url: str
    checksum: str
    size: int
    version: str
    content: bytes
    envelope: dict[str, Any]


@dataclass(frozen=True)
class FadGenerationSnapshot:
    peer_sas_id: int
    generation_datetime: str
    description: str
    files: tuple[FadFilePayload, ...]
    records: tuple[tuple[str, str, dict[str, Any]], ...] = field(default_factory=tuple)
    # records: (record_type, record_id, record_dict)


def fad_client_check_hostname() -> bool:
    """Whether TLS hostname verification is enforced (default True)."""
    return bool(get_settings().sas_fad_client_check_hostname)


def _client_ssl_context() -> ssl.SSLContext:
    settings = get_settings()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = fad_client_check_hostname()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(settings.resolved_ssl_ca_certs))
    ctx.load_cert_chain(
        certfile=str(settings.resolved_client_certfile),
        keyfile=str(settings.resolved_client_keyfile),
    )
    ctx.set_ciphers(":".join(ALLOWED_CIPHERS))
    return ctx


def build_fad_httpx_client(**overrides: Any) -> httpx.Client:
    """HTTP client for peer FAD pulls (no automatic redirects)."""
    settings = get_settings()
    # Security-critical defaults cannot be weakened via overrides.
    overrides.pop("follow_redirects", None)
    kwargs: dict[str, Any] = {
        "verify": _client_ssl_context(),
        "timeout": settings.http_timeout_seconds,
        "follow_redirects": False,
    }
    kwargs.update(overrides)
    kwargs["follow_redirects"] = False
    return httpx.Client(**kwargs)


def _parse_https_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise FadClientError(f"FAD URL must be https, got scheme={parsed.scheme!r}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise FadClientError("FAD URL missing host")
    if host in _BLOCKED_LITERAL_HOSTS:
        raise FadClientError(f"FAD host blocked: {host}")
    port = parsed.port or 443
    path = parsed.path or "/"
    return parsed.scheme.lower(), host, port, path


def _origin(scheme: str, host: str, port: int) -> str:
    default = 443 if scheme == "https" else 80
    if port == default:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def peer_base_origin(peer_url: str) -> str:
    scheme, host, port, _path = _parse_https_url(peer_url)
    return _origin(scheme, host, port)


def _host_allows_loopback_or_private(host: str) -> bool:
    """Peer was explicitly registered on loopback/private host (lab/harness)."""
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or ip.is_private)
    except ValueError:
        return host in {"localhost", "localhost.localdomain"}


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_lab: bool) -> bool:
    # Cloud metadata addresses are never acceptable, even for "lab" peers.
    if str(ip) in {"169.254.169.254", "169.254.169.253"}:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # Lab peers may use loopback / RFC1918 / link-local. Check those before
    # ``is_reserved``: Python flags IPv6 ``::1`` as both loopback and reserved,
    # which otherwise breaks ``localhost`` on dual-stack CI runners (GHA).
    if ip.is_link_local:
        return not allow_lab
    if ip.is_loopback:
        return not allow_lab
    if ip.is_private:
        return not allow_lab
    if ip.is_reserved:
        return True
    return False


def assert_resolved_host_allowed(host: str, *, peer_host: str) -> None:
    """Resolve ``host`` and reject SSRF-sensitive addresses."""
    allow_lab = _host_allows_loopback_or_private(peer_host) and host.lower() == peer_host.lower()
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FadClientError(f"FAD host DNS resolution failed: {host}") from exc
    if not infos:
        raise FadClientError(f"FAD host DNS resolution empty: {host}")
    seen_ip = False
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        seen_ip = True
        if _ip_blocked(ip, allow_lab=allow_lab):
            raise FadClientError(
                f"FAD host resolves to blocked address {ip} (host={host})"
            )
    if not seen_ip:
        raise FadClientError(f"FAD host DNS resolution produced no IPs: {host}")


def assert_url_allowed_for_peer(url: str, peer_url: str) -> None:
    """Ensure ``url`` is https and same-origin as the allowlisted peer base."""
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise FadClientError("FAD URL must not include userinfo credentials")
    peer_parsed = urlparse(peer_url)
    if peer_parsed.username is not None or peer_parsed.password is not None:
        raise FadClientError("peer URL must not include userinfo credentials")
    peer_scheme, peer_host, peer_port, _ = _parse_https_url(peer_url)
    scheme, host, port, _path = _parse_https_url(url)
    if (scheme, host, port) != (peer_scheme, peer_host, peer_port):
        raise FadClientError(
            f"FAD URL origin mismatch: url={url!r} peer_origin="
            f"{_origin(peer_scheme, peer_host, peer_port)!r}"
        )
    assert_resolved_host_allowed(host, peer_host=peer_host)


def peer_dump_url(peer_url: str) -> str:
    """Build ``{peer.url}/dump`` preserving any path prefix (e.g. ``/v1.3``)."""
    base = peer_url.strip().rstrip("/")
    _parse_https_url(base)
    return f"{base}/dump"


def _expected_sas_version() -> str:
    return get_settings().sas_sas_version.strip()


def _sha1_hex(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def validate_manifest(manifest: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(manifest, dict):
        raise FadClientError("FAD manifest must be a JSON object")
    for key in ("files", "generationDateTime", "description"):
        if key not in manifest:
            raise FadClientError(f"FAD manifest missing {key}")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise FadClientError("FAD manifest files must be a non-empty list")
    if len(files) > 101:
        raise FadClientError("FAD manifest exceeds max files (101)")
    gen = str(manifest["generationDateTime"])
    description = str(manifest["description"])
    expected_version = _expected_sas_version()
    normalized: list[dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise FadClientError("FAD manifest file entry must be an object")
        for req in ("url", "checksum", "size", "version", "recordType"):
            if req not in entry:
                raise FadClientError(f"FAD file entry missing {req}")
        record_type = str(entry["recordType"])
        if record_type not in RECORD_TYPES:
            raise FadClientError(f"FAD unsupported recordType={record_type!r}")
        version = str(entry["version"])
        if version != expected_version:
            raise FadClientError(
                f"FAD version mismatch: got {version!r} expected {expected_version!r}"
            )
        try:
            size = int(entry["size"])
        except (TypeError, ValueError) as exc:
            raise FadClientError("FAD file size must be an integer") from exc
        if size < 0:
            raise FadClientError("FAD file size must be non-negative")
        max_bytes = int(get_settings().sas_fad_max_file_bytes)
        if max_bytes > 0 and size > max_bytes:
            raise FadClientError(
                f"FAD file size {size} exceeds sas_fad_max_file_bytes={max_bytes}"
            )
        checksum = str(entry["checksum"]).lower()
        if len(checksum) != 40 or any(c not in "0123456789abcdef" for c in checksum):
            raise FadClientError("FAD checksum must be 40-char lowercase hex SHA-1")
        normalized.append(
            {
                "url": str(entry["url"]),
                "checksum": checksum,
                "size": size,
                "version": version,
                "recordType": record_type,
            }
        )
    # Absent recordTypes are treated as empty (WINNF peers / harness may omit
    # empty coordination dumps). Unknown types already rejected above.
    return gen, description, normalized


def validate_activity_envelope(
    envelope: Any, *, generation_datetime: str
) -> list[dict[str, Any]]:
    if not isinstance(envelope, dict):
        raise FadClientError("FAD activity file must be a JSON object")
    for key in ("startTime", "endTime", "recordData"):
        if key not in envelope:
            raise FadClientError(f"FAD activity file missing {key}")
    if envelope["startTime"] != generation_datetime or envelope["endTime"] != generation_datetime:
        raise FadClientError("FAD activity file timestamp does not match generationDateTime")
    records = envelope["recordData"]
    if not isinstance(records, list):
        raise FadClientError("FAD recordData must be a list")
    out: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise FadClientError("FAD record must be an object")
        record_id = str(record.get("id") or "").strip()
        if not record_id:
            raise FadClientError("FAD record missing id")
        out.append(record)
    return out


def _http_get_bytes(client: httpx.Client, url: str) -> bytes:
    try:
        resp = client.get(url)
    except httpx.RequestError as exc:
        raise FadClientError(f"peer unreachable: {exc}") from exc
    if resp.is_redirect:
        raise FadClientError(f"FAD redirect refused: {url} -> {resp.headers.get('location')}")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FadClientError(
            f"peer HTTP {exc.response.status_code} for {url}"
        ) from exc
    return resp.content


def fetch_peer_generation(
    client: httpx.Client, peer: PeerSas
) -> FadGenerationSnapshot:
    """Download and validate one peer generation without writing to the database."""
    base = (peer.url or "").strip()
    if not base:
        raise FadClientError("peer URL empty")
    dump_url = peer_dump_url(base)
    assert_url_allowed_for_peer(dump_url, base)

    raw_manifest = _http_get_bytes(client, dump_url)
    try:
        manifesto = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FadClientError("FAD manifest is not valid UTF-8 JSON") from exc

    generation, description, file_entries = validate_manifest(manifesto)
    files: list[FadFilePayload] = []
    records: list[tuple[str, str, dict[str, Any]]] = []

    for entry in file_entries:
        file_url = entry["url"]
        assert_url_allowed_for_peer(file_url, base)
        content = _http_get_bytes(client, file_url)
        max_bytes = int(get_settings().sas_fad_max_file_bytes)
        if max_bytes > 0 and len(content) > max_bytes:
            raise FadClientError(
                f"FAD file body {len(content)} exceeds sas_fad_max_file_bytes={max_bytes}"
            )
        digest = _sha1_hex(content)
        if digest != entry["checksum"]:
            raise FadClientError(
                f"FAD checksum mismatch for {file_url}: got {digest} expected {entry['checksum']}"
            )
        if len(content) != entry["size"]:
            raise FadClientError(
                f"FAD size mismatch for {file_url}: got {len(content)} expected {entry['size']}"
            )
        try:
            envelope = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FadClientError(f"FAD activity file not JSON: {file_url}") from exc
        parsed_records = validate_activity_envelope(
            envelope, generation_datetime=generation
        )
        files.append(
            FadFilePayload(
                record_type=entry["recordType"],
                url=file_url,
                checksum=entry["checksum"],
                size=entry["size"],
                version=entry["version"],
                content=content,
                envelope=envelope if isinstance(envelope, dict) else {},
            )
        )
        for record in parsed_records:
            records.append((entry["recordType"], str(record["id"]), record))

    seen_keys: set[tuple[str, str]] = set()
    for record_type, record_id, _record in records:
        key = (record_type, record_id)
        if key in seen_keys:
            raise FadClientError(
                f"FAD duplicate record id in generation: {record_type}/{record_id}"
            )
        seen_keys.add(key)

    return FadGenerationSnapshot(
        peer_sas_id=peer.id,
        generation_datetime=generation,
        description=description,
        files=tuple(files),
        records=tuple(records),
    )


def _record_data_json(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False)


def _snapshot_record_fingerprint(
    snapshot: FadGenerationSnapshot,
) -> frozenset[tuple[str, str, str]]:
    """Stable set of (record_type, record_id, canonical JSON) for equality checks."""
    return frozenset(
        (record_type, record_id, _record_data_json(record))
        for record_type, record_id, record in snapshot.records
    )


def _local_peer_record_fingerprint(
    db: Session, peer_sas_id: int
) -> frozenset[tuple[str, str, str]]:
    rows = db.query(PeerFadRecord).filter_by(peer_sas_id=peer_sas_id).all()
    return frozenset((row.record_type, row.record_id, row.data_json) for row in rows)


def peer_generation_already_applied(
    db: Session, peer: PeerSas, snapshot: FadGenerationSnapshot
) -> bool:
    """True when durable peer state already matches this validated generation.

 Matching ``generationDateTime`` alone is not enough: local wipe or content
 change under a reused timestamp must trigger re-apply.
 """
    if not peer.last_fad_generation:
        return False
    if peer.last_fad_generation != snapshot.generation_datetime:
        return False
    return _local_peer_record_fingerprint(db, peer.id) == _snapshot_record_fingerprint(
        snapshot
    )


def apply_peer_generation(db: Session, snapshot: FadGenerationSnapshot) -> None:
    """Replace all stored records for a peer with the validated generation (atomic)."""
    db.query(PeerFadRecord).filter_by(peer_sas_id=snapshot.peer_sas_id).delete(
        synchronize_session=False
    )
    for record_type, record_id, record in snapshot.records:
        db.add(
            PeerFadRecord(
                peer_sas_id=snapshot.peer_sas_id,
                record_type=record_type,
                record_id=record_id,
                data_json=_record_data_json(record),
            )
        )
    peer = db.query(PeerSas).filter_by(id=snapshot.peer_sas_id).first()
    if peer is not None:
        peer.last_fad_generation = snapshot.generation_datetime
    db.flush()


def _lock_peer_row(db: Session, peer_id: int) -> PeerSas | None:
    """Serialize publication for one peer (row lock when dialect supports it)."""
    query = db.query(PeerSas).filter_by(id=peer_id)
    bind = db.get_bind()
    if bind is not None and bind.dialect.name != "sqlite":
        query = query.with_for_update()
    return query.first()


def sync_one_peer(
    db: Session, peer: PeerSas, *, client: httpx.Client | None = None
) -> FadGenerationSnapshot | None:
    """Fetch, validate and atomically apply one peer FAD.

 Heavy network I/O runs before the DB critical section. Apply serializes
 against IAP grant admission (process + PG advisory) so Grants cannot mix
 marker generation N with peer records N+1. On failure the session is
 rolled back so the previous peer snapshot remains. Repeating the same
 generation is a no-op only when durable peer records already match.
 """
    if not (peer.url or "").strip():
        return None

    owns_client = client is None
    http = client or build_fad_httpx_client()
    try:
        snapshot = fetch_peer_generation(http, peer)
        from services.concurrency import (
            acquire_iap_admission_xact_lock,
            exclusive_iap_admission,
        )

        # Lock order: IAP admission → peer row (never take peer then IAP).
        with exclusive_iap_admission():
            acquire_iap_admission_xact_lock(db)
            locked = _lock_peer_row(db, peer.id)
            if locked is None:
                raise FadClientError(f"peer SAS id={peer.id} disappeared during sync")
            if peer_generation_already_applied(db, locked, snapshot):
                logger.info(
                    "Peer FAD generation unchanged peer_id=%s generation=%s; skip apply",
                    peer.id,
                    snapshot.generation_datetime,
                )
                db.commit()  # release row + IAP advisory
                return snapshot
            apply_peer_generation(db, snapshot)
            db.commit()
        logger.info(
            "Peer FAD synced peer_id=%s generation=%s records=%s files=%s",
            peer.id,
            snapshot.generation_datetime,
            len(snapshot.records),
            len(snapshot.files),
        )
        return snapshot
    except Exception:
        db.rollback()
        logger.exception(
            "Peer FAD sync failed; preserved previous snapshot peer_id=%s url=%s",
            peer.id,
            peer.url,
        )
        raise
    finally:
        if owns_client:
            http.close()


def run_peer_fad_sync(
    db: Session, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Sync every allowlisted ``PeerSas`` independently (one failure does not wipe others)."""
    peers = db.query(PeerSas).order_by(PeerSas.id).all()
    report: dict[str, Any] = {
        "peers": len(peers),
        "ok": 0,
        "failed": 0,
        "skipped_same_generation": 0,
        "errors": [],
    }
    if not peers:
        return report

    owns_client = client is None
    http = client or build_fad_httpx_client()
    try:
        for peer in peers:
            try:
                # Capture durable fingerprint before sync for skip accounting.
                prior_gen = peer.last_fad_generation
                prior_fp = (
                    _local_peer_record_fingerprint(db, peer.id) if prior_gen else None
                )
                snapshot = sync_one_peer(db, peer, client=http)
                report["ok"] += 1
                if (
                    snapshot is not None
                    and prior_gen
                    and prior_gen == snapshot.generation_datetime
                    and prior_fp is not None
                    and prior_fp == _snapshot_record_fingerprint(snapshot)
                ):
                    report["skipped_same_generation"] += 1
            except Exception as exc:  # noqa: BLE001 — continue other peers
                report["failed"] += 1
                report["errors"].append(
                    {"peer_id": peer.id, "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        if owns_client:
            http.close()
    return report
