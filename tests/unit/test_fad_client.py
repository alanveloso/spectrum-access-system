"""secure peer FAD client — TLS hostname, SSRF, checksum, atomic purge."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from config import clear_settings_cache, get_settings
from models.models import PeerFadRecord, PeerSas
from services import fad_client_service as fad_client
from services.fad_client_service import (
    FadClientError,
    assert_url_allowed_for_peer,
    fad_client_check_hostname,
    run_peer_fad_sync,
    sync_one_peer,
    validate_manifest,
)


PEER_BASE = "https://localhost/v1.3"
GEN = "2026-08-07T12:00:00Z"


def _sha1(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def _envelope(records: list[dict[str, Any]]) -> bytes:
    body = {
        "startTime": GEN,
        "endTime": GEN,
        "recordData": records,
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _file_entry(record_type: str, content: bytes, *, page: int = 0) -> dict[str, Any]:
    path = f"/v1.3/{record_type}/activity_dump_file_{record_type}{page}.json"
    url = f"https://localhost{path}"
    return {
        "url": url,
        "checksum": _sha1(content),
        "size": len(content),
        "version": "v1.3",
        "recordType": record_type,
        "_content": content,
        "_path": path,
    }


def _build_generation(
    *,
    cbsd_records: list[dict[str, Any]] | None = None,
    zone_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    files_meta: list[dict[str, Any]] = []
    bodies: dict[str, bytes] = {}
    specs = [
        ("cbsd", cbsd_records or []),
        ("zone", zone_records or []),
        ("esc_sensor", []),
        ("coordination", []),
    ]
    for record_type, records in specs:
        content = _envelope(records)
        entry = _file_entry(record_type, content)
        bodies[entry["_path"]] = content
        files_meta.append({k: entry[k] for k in ("url", "checksum", "size", "version", "recordType")})
    manifest = {
        "files": files_meta,
        "generationDateTime": GEN,
        "description": "Full activity dump files",
    }
    return manifest, bodies


def _mock_client(manifest: dict[str, Any], bodies: dict[str, bytes]) -> httpx.Client:
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/dump") or path == "/v1.3/dump":
            return httpx.Response(200, content=manifest_bytes)
        if path in bodies:
            return httpx.Response(200, content=bodies[path])
        if request.url.path in bodies:
            return httpx.Response(200, content=bodies[request.url.path])
        return httpx.Response(404, text=f"missing {path}")

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, follow_redirects=False)


def setup_function() -> None:
    clear_settings_cache()


def teardown_function() -> None:
    clear_settings_cache()


def test_hostname_check_defaults_true():
    assert get_settings().sas_fad_client_check_hostname is True
    assert fad_client_check_hostname() is True


def test_hostname_check_wired_into_ssl_context(monkeypatch, tmp_path):
    """When cert material exists, SSLContext.check_hostname follows the setting."""
    ca = tmp_path / "ca.cert"
    cert = tmp_path / "server.cert"
    key = tmp_path / "server.key"
    # Minimal PEM placeholders are insufficient for load_cert_chain; skip if crypto
    # material is unavailable and only assert the flag plumbing above.
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime as dt

        key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert_obj = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key_obj.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
            .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
            .sign(key_obj, hashes.SHA256())
        )
        key.write_bytes(
            key_obj.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        pem = cert_obj.public_bytes(serialization.Encoding.PEM)
        ca.write_bytes(pem)
        cert.write_bytes(pem)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot build ephemeral certs: {exc}")

    monkeypatch.setenv("CERTS_DIR", str(tmp_path))
    monkeypatch.setenv("SSL_CA_CERTS", str(ca))
    monkeypatch.setenv("SSL_CERTFILE", str(cert))
    monkeypatch.setenv("SSL_KEYFILE", str(key))
    monkeypatch.setenv("CLIENT_CERTFILE", str(cert))
    monkeypatch.setenv("CLIENT_KEYFILE", str(key))
    clear_settings_cache()
    ctx = fad_client._client_ssl_context()
    assert ctx.check_hostname is True
    clear_settings_cache()


def test_hostname_check_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SAS_FAD_CLIENT_CHECK_HOSTNAME", "false")
    clear_settings_cache()
    assert get_settings().sas_fad_client_check_hostname is False
    assert fad_client_check_hostname() is False
    clear_settings_cache()


def test_reject_http_and_foreign_origin():
    with pytest.raises(FadClientError, match="https"):
        assert_url_allowed_for_peer("http://localhost/v1.3/dump", PEER_BASE)
    with pytest.raises(FadClientError, match="origin mismatch"):
        assert_url_allowed_for_peer(
            "https://evil.example/v1.3/cbsd/x.json", PEER_BASE
        )


def test_reject_metadata_host_literal():
    with pytest.raises(FadClientError, match="blocked"):
        assert_url_allowed_for_peer(
            "https://metadata.google.internal/latest",
            "https://metadata.google.internal/v1.3",
        )


def test_reject_metadata_ip_even_as_registered_peer(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(0, 0, 0, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(fad_client.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FadClientError, match="blocked address"):
        assert_url_allowed_for_peer(
            "https://169.254.169.254/latest",
            "https://169.254.169.254/v1.3",
        )


def test_reject_url_userinfo():
    with pytest.raises(FadClientError, match="userinfo"):
        assert_url_allowed_for_peer(
            "https://user:pass@localhost/v1.3/dump",
            PEER_BASE,
        )


def test_validate_manifest_rejects_bad_version_allows_omitted_types():
    with pytest.raises(FadClientError, match="version"):
        validate_manifest(
            {
                "generationDateTime": GEN,
                "description": "x",
                "files": [
                    {
                        "url": f"{PEER_BASE}/{rt}/a.json",
                        "checksum": "a" * 40,
                        "size": 1,
                        "version": "v9.9" if rt == "cbsd" else "v1.3",
                        "recordType": rt,
                    }
                    for rt in ("cbsd", "zone", "esc_sensor", "coordination")
                ],
            }
        )
    # Peer/harness dumps may omit empty coordination (and other empty types).
    gen, _desc, files = validate_manifest(
        {
            "generationDateTime": GEN,
            "description": "x",
            "files": [
                {
                    "url": f"{PEER_BASE}/cbsd/a.json",
                    "checksum": "a" * 40,
                    "size": 1,
                    "version": "v1.3",
                    "recordType": "cbsd",
                },
                {
                    "url": f"{PEER_BASE}/zone/a.json",
                    "checksum": "b" * 40,
                    "size": 1,
                    "version": "v1.3",
                    "recordType": "zone",
                },
                {
                    "url": f"{PEER_BASE}/esc_sensor/a.json",
                    "checksum": "c" * 40,
                    "size": 1,
                    "version": "v1.3",
                    "recordType": "esc_sensor",
                },
            ],
        }
    )
    assert gen == GEN
    assert {f["recordType"] for f in files} == {"cbsd", "zone", "esc_sensor"}
    with pytest.raises(FadClientError, match="unsupported recordType"):
        validate_manifest(
            {
                "generationDateTime": GEN,
                "description": "x",
                "files": [
                    {
                        "url": f"{PEER_BASE}/other/a.json",
                        "checksum": "a" * 40,
                        "size": 1,
                        "version": "v1.3",
                        "recordType": "other",
                    }
                ],
            }
        )


def test_duplicate_record_id_rejected(db_session):
    peer = PeerSas(certificate_hash="peer-dup", url=PEER_BASE)
    db_session.add(peer)
    db_session.commit()
    manifest, bodies = _build_generation(
        cbsd_records=[
            {"id": "cbsd/same", "registration": {}, "grants": []},
            {"id": "cbsd/same", "registration": {}, "grants": []},
        ]
    )
    client = _mock_client(manifest, bodies)
    with pytest.raises(FadClientError, match="duplicate record"):
        sync_one_peer(db_session, peer, client=client)


def test_reject_public_peer_resolving_to_private(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(0, 0, 0, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(fad_client.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FadClientError, match="blocked address"):
        assert_url_allowed_for_peer(
            "https://peer.example.test/v1.3/dump",
            "https://peer.example.test/v1.3",
        )


def test_localhost_lab_peer_allows_ipv6_loopback(monkeypatch):
    """``::1`` is is_reserved+is_loopback; lab localhost peers must still work."""

    def fake_getaddrinfo(host, *args, **kwargs):
        assert host == "localhost"
        return [
            (0, 0, 0, "", ("::1", 0, 0, 0)),
            (0, 0, 0, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(fad_client.socket, "getaddrinfo", fake_getaddrinfo)
    assert_url_allowed_for_peer(f"{PEER_BASE}/dump", PEER_BASE)


def test_sync_persists_records_and_purges_absent(db_session):
    peer = PeerSas(certificate_hash="peer-a", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="cbsd/obsolete",
            data_json=json.dumps({"id": "cbsd/obsolete"}),
        )
    )
    db_session.commit()

    manifest, bodies = _build_generation(
        cbsd_records=[{"id": "cbsd/keep", "registration": {}, "grants": []}]
    )
    client = _mock_client(manifest, bodies)
    sync_one_peer(db_session, peer, client=client)

    rows = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_type="cbsd")
        .all()
    )
    ids = {r.record_id for r in rows}
    assert ids == {"cbsd/keep"}
    assert db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).count() >= 1


def test_checksum_mismatch_preserves_previous_snapshot(db_session):
    peer = PeerSas(certificate_hash="peer-b", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="cbsd/old",
            data_json=json.dumps({"id": "cbsd/old"}),
        )
    )
    db_session.commit()

    manifest, bodies = _build_generation(
        cbsd_records=[{"id": "cbsd/new", "registration": {}, "grants": []}]
    )
    # Corrupt checksum for cbsd file.
    for entry in manifest["files"]:
        if entry["recordType"] == "cbsd":
            entry["checksum"] = "0" * 40

    client = _mock_client(manifest, bodies)
    with pytest.raises(FadClientError, match="checksum"):
        sync_one_peer(db_session, peer, client=client)

    db_session.expire_all()
    rows = db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).all()
    assert len(rows) == 1
    assert rows[0].record_id == "cbsd/old"


def test_redirect_is_refused(db_session):
    peer = PeerSas(certificate_hash="peer-c", url=PEER_BASE)
    db_session.add(peer)
    db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/dump"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(FadClientError, match="redirect"):
        sync_one_peer(db_session, peer, client=client)


def test_run_peer_fad_sync_isolates_peer_failures(db_session):
    good = PeerSas(certificate_hash="good", url=PEER_BASE)
    # Different host → manifest file URLs (localhost) fail origin check.
    bad = PeerSas(certificate_hash="bad", url="https://127.0.0.1/v1.3")
    db_session.add_all([good, bad])
    db_session.commit()

    manifest, bodies = _build_generation(
        cbsd_records=[{"id": "cbsd/ok", "registration": {}, "grants": []}]
    )
    client = _mock_client(manifest, bodies)
    report = run_peer_fad_sync(db_session, client=client)
    assert report["ok"] == 1
    assert report["failed"] == 1
    assert (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=good.id, record_id="cbsd/ok")
        .count()
        == 1
    )
    assert db_session.query(PeerFadRecord).filter_by(peer_sas_id=bad.id).count() == 0
