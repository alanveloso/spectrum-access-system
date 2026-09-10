"""Multi-SAS resilience — peers, conflicts, clocks, concurrency."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from models.models import AdminInjectedData, Cbsd, Grant, PeerFadRecord, PeerSas
from services.cpas_service import (
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.fad_client_service import (
    FadClientError,
    run_peer_fad_sync,
    sync_one_peer,
)
from services.fad_service import fad_cbsd_id


PEER_BASE = "https://localhost/v1.3"
GEN_A = "2020-01-01T00:00:00Z"  # far from "now" → clocks diferentes
GEN_B = "2020-01-02T00:00:00Z"


def _sha1(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def _envelope(records: list[dict[str, Any]], *, gen: str) -> bytes:
    body = {"startTime": gen, "endTime": gen, "recordData": records}
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _build_generation(
    *,
    gen: str,
    cbsd_records: list[dict[str, Any]] | None = None,
    zone_records: list[dict[str, Any]] | None = None,
    esc_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    files_meta: list[dict[str, Any]] = []
    bodies: dict[str, bytes] = {}
    specs = [
        ("cbsd", cbsd_records or []),
        ("zone", zone_records or []),
        ("esc_sensor", esc_records or []),
        ("coordination", []),
    ]
    for record_type, records in specs:
        content = _envelope(records, gen=gen)
        path = f"/v1.3/{record_type}/activity_dump_file_{record_type}0.json"
        url = f"https://localhost{path}"
        entry = {
            "url": url,
            "checksum": _sha1(content),
            "size": len(content),
            "version": "v1.3",
            "recordType": record_type,
        }
        bodies[path] = content
        files_meta.append(entry)
    manifest = {
        "files": files_meta,
        "generationDateTime": gen,
        "description": "Full activity dump files",
    }
    return manifest, bodies


def _mock_client(
    manifest: dict[str, Any],
    bodies: dict[str, bytes],
    *,
    fail_connect: bool = False,
) -> httpx.Client:
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_connect:
            raise httpx.ConnectError("simulated peer down", request=request)
        path = request.url.path
        if path.endswith("/dump"):
            return httpx.Response(200, content=manifest_bytes)
        if path in bodies:
            return httpx.Response(200, content=bodies[path])
        return httpx.Response(404, text=f"missing {path}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _add_cbsd_grant(db, *, fcc: str, serial: str, grant_id: str, lat=39.0, lon=-100.0):
    cbsd = Cbsd(
        cbsd_id=f"{fcc}/{serial}",
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id="u",
        registration_json=json.dumps(
            {
                "fccId": fcc,
                "cbsdSerialNumber": serial,
                "cbsdCategory": "A",
                "airInterface": {"radioTechnology": "E_UTRA"},
                "measCapability": [],
                "installationParam": {
                    "latitude": lat,
                    "longitude": lon,
                    "height": 10,
                    "heightType": "AGL",
                },
            }
        ),
    )
    db.add(cbsd)
    db.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id=grant_id,
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        low_frequency=3550000000,
        high_frequency=3560000000,
        max_eirp=20.0,
        channel_type="GAA",
        grant_expire_time=expire.replace(tzinfo=None),
        terminated=False,
        grant_json="{}",
    )
    db.add(grant)
    db.flush()
    return cbsd, grant


def test_peer_inaccessible_preserves_previous_records(db_session):
    peer = PeerSas(certificate_hash="peer-down", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="cbsd/keep",
            data_json=json.dumps({"id": "cbsd/keep"}),
        )
    )
    peer.last_fad_generation = GEN_A
    db_session.commit()

    manifest, bodies = _build_generation(gen=GEN_B)
    client = _mock_client(manifest, bodies, fail_connect=True)
    with pytest.raises(FadClientError, match="peer unreachable"):
        sync_one_peer(db_session, peer, client=client)

    db_session.expire_all()
    rows = db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).all()
    assert len(rows) == 1
    assert rows[0].record_id == "cbsd/keep"
    assert peer.last_fad_generation == GEN_A


def test_invalid_fad_checksum_preserves_previous(db_session):
    peer = PeerSas(certificate_hash="peer-bad", url=PEER_BASE)
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
        gen=GEN_A, cbsd_records=[{"id": "cbsd/new", "grants": []}]
    )
    for entry in manifest["files"]:
        if entry["recordType"] == "cbsd":
            entry["checksum"] = "0" * 40
    client = _mock_client(manifest, bodies)
    with pytest.raises(FadClientError, match="checksum"):
        sync_one_peer(db_session, peer, client=client)
    assert (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="cbsd/old")
        .count()
        == 1
    )


def test_same_generation_is_idempotent_noop(db_session):
    peer = PeerSas(certificate_hash="peer-idem", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.commit()

    manifest, bodies = _build_generation(
        gen=GEN_A, cbsd_records=[{"id": "cbsd/one", "grants": []}]
    )
    client = _mock_client(manifest, bodies)
    first = sync_one_peer(db_session, peer, client=client)
    assert first is not None
    db_session.refresh(peer)
    assert peer.last_fad_generation == GEN_A
    count_after_first = (
        db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).count()
    )
    assert count_after_first >= 1

    # Identical generation + matching durable records → skip apply.
    second = sync_one_peer(db_session, peer, client=client)
    assert second is not None
    assert second.generation_datetime == GEN_A
    assert (
        db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).count()
        == count_after_first
    )
    report = run_peer_fad_sync(db_session, client=client)
    assert report["skipped_same_generation"] >= 1


def test_same_generation_recovers_after_local_wipe(db_session):
    peer = PeerSas(certificate_hash="peer-recover", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.commit()

    manifest, bodies = _build_generation(
        gen=GEN_A, cbsd_records=[{"id": "cbsd/recover", "grants": []}]
    )
    client = _mock_client(manifest, bodies)
    sync_one_peer(db_session, peer, client=client)
    db_session.refresh(peer)
    assert peer.last_fad_generation == GEN_A

    db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).delete()
    db_session.commit()
    assert db_session.query(PeerFadRecord).filter_by(peer_sas_id=peer.id).count() == 0

    # Same generationDateTime but empty local store must re-apply (not skip).
    recovered = sync_one_peer(db_session, peer, client=client)
    assert recovered is not None
    assert (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="cbsd/recover")
        .count()
        == 1
    )


def test_same_generation_content_change_reapplies(db_session):
    peer = PeerSas(certificate_hash="peer-content", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.commit()

    manifest_a, bodies_a = _build_generation(
        gen=GEN_A, cbsd_records=[{"id": "cbsd/x", "grants": [{"id": "g1"}]}]
    )
    sync_one_peer(db_session, peer, client=_mock_client(manifest_a, bodies_a))
    assert (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="cbsd/x")
        .one()
        .data_json
        .find("g1")
        >= 0
    )

    # Reused generationDateTime with different payload must replace durable state.
    manifest_b, bodies_b = _build_generation(
        gen=GEN_A, cbsd_records=[{"id": "cbsd/x", "grants": [{"id": "g2"}]}]
    )
    sync_one_peer(db_session, peer, client=_mock_client(manifest_b, bodies_b))
    row = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="cbsd/x")
        .one()
    )
    assert "g2" in row.data_json
    assert "g1" not in row.data_json


def test_clock_skew_generation_accepted(db_session):
    """Peer generation far from local wall clock is OK if internally consistent."""
    peer = PeerSas(certificate_hash="peer-clock", url=PEER_BASE)
    db_session.add(peer)
    db_session.commit()
    manifest, bodies = _build_generation(
        gen=GEN_A, cbsd_records=[{"id": "cbsd/old-clock", "grants": []}]
    )
    client = _mock_client(manifest, bodies)
    snap = sync_one_peer(db_session, peer, client=client)
    assert snap is not None
    assert snap.generation_datetime == GEN_A
    assert (
        db_session.query(PeerFadRecord)
        .filter_by(record_id="cbsd/old-clock")
        .count()
        == 1
    )


def test_grant_conflict_terminates_local_grant(db_session, monkeypatch):
    cbsd, grant = _add_cbsd_grant(db_session, fcc="fcc-g", serial="sn-g", grant_id="G1")
    peer = PeerSas(certificate_hash="peer-g", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    rid = fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number)
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id=rid,
            data_json=json.dumps({"id": rid, "grants": [{"id": "pg", "terminated": False}]}),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 1, "ok": 1, "failed": 0, "errors": []},
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated is True
    assert any(d["reason"] == "peer_same_cbsd_grant" for d in result["decisions"])


def test_ppa_conflict_terminates_overlapping_grant(db_session, monkeypatch):
    cbsd, grant = _add_cbsd_grant(db_session, fcc="fcc-p", serial="sn-p", grant_id="GP")
    peer = PeerSas(certificate_hash="peer-p", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    # Square around CBSD location.
    ring = [
        [-100.1, 38.9],
        [-99.9, 38.9],
        [-99.9, 39.1],
        [-100.1, 39.1],
        [-100.1, 38.9],
    ]
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="zone",
            record_id="zone/ppa/peer/1",
            data_json=json.dumps(
                {
                    "id": "zone/ppa/peer/1",
                    "usage": "PPA",
                    "terminated": False,
                    "ppaInfo": {"palId": ["PAL-1"]},
                    "zone": {"type": "Polygon", "coordinates": [ring]},
                }
            ),
        )
    )
    db_session.add(
        AdminInjectedData(
            kind="pal",
            data_json=json.dumps(
                {
                    "palId": "PAL-1",
                    "channelAssignment": {
                        "primaryAssignment": {
                            "lowFrequency": 3550000000,
                            "highFrequency": 3560000000,
                        }
                    },
                }
            ),
        )
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snap)
    assert any(d.reason == "peer_ppa" and d.grant_pk == grant.id for d in decisions)

    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 0, "ok": 0, "failed": 0, "errors": []},
    )
    execute_cpas_pipeline(db_session)
    db_session.refresh(grant)
    assert grant.terminated is True


def test_esc_peer_conflict_terminates_nearby_grant(db_session, monkeypatch):
    from services.iap import coupling as coupling_mod
    from services.iap.aggregate import dbm_to_mw

    cbsd, grant = _add_cbsd_grant(db_session, fcc="fcc-e", serial="sn-e", grant_id="GE")
    peer = PeerSas(certificate_hash="peer-e", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="esc_sensor",
            record_id="esc_sensor/peer/1",
            data_json=json.dumps(
                {
                    "id": "esc_sensor/peer/1",
                    "installationParam": {
                        "latitude": 39.0,
                        "longitude": -100.0,
                        "height": 3.0,
                        "heightType": "AGL",
                        "antennaAzimuth": 0.0,
                        "azimuthRadiationPattern": [
                            {"angle": i, "gain": 30.0} for i in range(360)
                        ],
                    },
                }
            ),
        )
    )
    db_session.commit()
    # Peer ESC also yields an IAP ProtectionPoint (C4); stub coupling so boolean
    # peer_esc terminate remains the assertion under test without ITM/ENV.
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: (lambda g, p, ch, eirp: dbm_to_mw(eirp) * 1e-20),
    )
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snap)
    assert any(d.reason == "peer_esc" and d.grant_pk == grant.id for d in decisions)

    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 0, "ok": 0, "failed": 0, "errors": []},
    )
    execute_cpas_pipeline(db_session)
    db_session.refresh(grant)
    assert grant.terminated is True


def test_one_peer_fail_does_not_block_other_peer(db_session):
    good = PeerSas(certificate_hash="good", url=PEER_BASE)
    bad = PeerSas(certificate_hash="bad", url="https://127.0.0.1/v1.3")
    db_session.add_all([good, bad])
    db_session.commit()
    manifest, bodies = _build_generation(
        gen=GEN_A, cbsd_records=[{"id": "cbsd/ok", "grants": []}]
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


def test_concurrent_cpas_second_trigger_is_noop(monkeypatch):
    from config import clear_settings_cache

    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()
    import services.cpas_service as cpas

    running = {"value": False}
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    monkeypatch.setattr(cpas, "is_cpas_running", lambda db: running["value"])

    def _set(db, flag, payload=None):
        del db, flag, payload
        running["value"] = True
        calls.append("set")

    def _clear(db, flag):
        del db, flag
        running["value"] = False
        calls.append("clear")

    def _exec(db):
        del db
        calls.append("exec")
        started.set()
        assert release.wait(timeout=2.0)

    monkeypatch.setattr(cpas, "set_admin_flag", _set)
    monkeypatch.setattr(cpas, "clear_admin_flags", _clear)
    monkeypatch.setattr(cpas, "execute_cpas_pipeline", _exec)
    monkeypatch.setattr("database.SessionLocal", lambda: type("S", (), {"close": lambda self: None})())

    cpas.trigger_daily_activities(object())
    assert started.wait(timeout=2.0)
    # Second trigger while running must be a no-op.
    cpas.trigger_daily_activities(object())
    release.set()
    for thread in threading.enumerate():
        if thread.name == "cpas-certification" and thread.is_alive():
            thread.join(timeout=2.0)
    assert calls.count("set") == 1
    assert calls.count("exec") == 1
    clear_settings_cache()


def test_sync_one_peer_skips_empty_url(db_session):
    """Without a persisted peer URL, CPAS cannot ingest FAD coordination state."""
    peer = PeerSas(certificate_hash="peer-empty-url", url="")
    db_session.add(peer)
    db_session.commit()
    assert sync_one_peer(db_session, peer) is None


def test_fad_client_allows_loopback_peer_origin_for_outbound(monkeypatch):
    """Outbound peer pull may use lab loopback; generic egress SSRF stays fail-closed."""
    from services.fad_client_service import assert_url_allowed_for_peer
    from services.ssrf import SsrfError, assert_https_egress_url_allowed

    base = "https://127.0.0.1:19003/v1.2"
    dump = f"{base}/dump"

    def _loopback(*_a, **_k):
        return [(None, None, None, None, ("127.0.0.1", 0))]

    monkeypatch.setattr("services.fad_client_service.socket.getaddrinfo", _loopback)
    assert_url_allowed_for_peer(dump, base)

    with pytest.raises(SsrfError):
        assert_https_egress_url_allowed(dump, allow_lab_private=False)


def test_peer_only_terminal_resolution_skips_unavailable_iap(
    db_session, monkeypatch
):
    """A: every active grant terminally resolved by peer → CPAS completes.

    RF coupling may be unavailable; no unresolved grant remains that requires it.
    """
    from services.iap.coupling import IapCouplingUnavailable
    import services.mcp_protection as mcp

    cbsd_esc, grant_esc = _add_cbsd_grant(
        db_session, fcc="fcc-e2", serial="sn-e2", grant_id="GE2", lat=39.5, lon=-100.0
    )
    cbsd_ppa, grant_ppa = _add_cbsd_grant(
        db_session, fcc="fcc-p2", serial="sn-p2", grant_id="GP2", lat=39.0, lon=-100.0
    )
    peer = PeerSas(certificate_hash="peer-fad2-iap", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="esc_sensor",
            record_id="esc_sensor/peer/fad2",
            data_json=json.dumps(
                {
                    "id": "esc_sensor/peer/fad2",
                    "installationParam": {
                        "latitude": 39.5,
                        "longitude": -100.0,
                        "height": 3.0,
                        "heightType": "AGL",
                        "antennaAzimuth": 0.0,
                        "azimuthRadiationPattern": [
                            {"angle": i, "gain": 30.0} for i in range(360)
                        ],
                    },
                }
            ),
        )
    )
    ring = [
        [-100.1, 38.9],
        [-99.9, 38.9],
        [-99.9, 39.1],
        [-100.1, 39.1],
        [-100.1, 38.9],
    ]
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="zone",
            record_id="zone/ppa/peer/fad2",
            data_json=json.dumps(
                {
                    "id": "zone/ppa/peer/fad2",
                    "usage": "PPA",
                    "terminated": False,
                    "ppaInfo": {"palId": ["PAL-FAD2"]},
                    "zone": {"type": "Polygon", "coordinates": [ring]},
                }
            ),
        )
    )
    db_session.add(
        AdminInjectedData(
            kind="pal",
            data_json=json.dumps(
                {
                    "palId": "PAL-FAD2",
                    "channelAssignment": {
                        "primaryAssignment": {
                            "lowFrequency": 3550000000,
                            "highFrequency": 3560000000,
                        }
                    },
                }
            ),
        )
    )
    grant_ppa.low_frequency = 3550000000
    grant_ppa.high_frequency = 3555000000
    grant_esc.low_frequency = 3620000000
    grant_esc.high_frequency = 3630000000
    db_session.commit()

    def _boom(*_a, **_k):
        raise IapCouplingUnavailable("forced unavailable for regression")

    monkeypatch.setattr(mcp, "resolve_iap_context", _boom)
    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 1, "ok": 1, "failed": 0, "errors": []},
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant_esc)
    db_session.refresh(grant_ppa)
    assert grant_esc.terminated is True
    assert grant_ppa.terminated is True
    reasons = {d["reason"] for d in result["decisions"]}
    assert "peer_esc" in reasons
    assert "peer_ppa" in reasons
    audits = [
        json.loads(r.data_json)
        for r in db_session.query(AdminInjectedData)
        .filter_by(kind="cpas_pipeline_audit")
        .all()
    ]
    assert any(a.get("event") == "cpas_completed" for a in audits)
    assert not any(a.get("event") == "cpas_failed" for a in audits)


def test_mixed_peer_and_rf_unavailable_fails_closed_atomic(
    db_session, monkeypatch
):
    """B: peer terminate on A + unresolved B needing IAP + RF down → atomic fail.

    No grant mutations from the cycle; cpas_failed only ().
    """
    from services.cpas_service import CpasRfEvaluationError, KIND_CPAS_AUDIT
    from services.iap.coupling import IapCouplingUnavailable
    import services.mcp_protection as mcp

    cbsd_a, grant_a = _add_cbsd_grant(
        db_session, fcc="fcc-mix-a", serial="sn-mix-a", grant_id="G-MIX-A"
    )
    cbsd_b, grant_b = _add_cbsd_grant(
        db_session,
        fcc="fcc-mix-b",
        serial="sn-mix-b",
        grant_id="G-MIX-B",
        lat=40.0,
        lon=-105.0,
    )
    peer = PeerSas(certificate_hash="peer-mix", url=PEER_BASE)
    db_session.add(peer)
    db_session.flush()
    # Peer same-CBSD conflict for A only.
    rid = fad_cbsd_id(cbsd_a.fcc_id, cbsd_a.cbsd_serial_number)
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id=rid,
            data_json=json.dumps(
                {"id": rid, "grants": [{"id": "pg-a", "terminated": False}]}
            ),
        )
    )
    # Peer ESC far from B but still yields IAP ProtectionPoints (required RF).
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="esc_sensor",
            record_id="esc_sensor/peer/mix",
            data_json=json.dumps(
                {
                    "id": "esc_sensor/peer/mix",
                    "installationParam": {
                        "latitude": 39.0,
                        "longitude": -100.0,
                        "height": 3.0,
                        "heightType": "AGL",
                        "antennaAzimuth": 0.0,
                        "azimuthRadiationPattern": [
                            {"angle": i, "gain": 30.0} for i in range(360)
                        ],
                    },
                }
            ),
        )
    )
    db_session.commit()

    def _boom(*_a, **_k):
        raise IapCouplingUnavailable("forced unavailable for mixed-case")

    monkeypatch.setattr(mcp, "resolve_iap_context", _boom)
    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 1, "ok": 1, "failed": 0, "errors": []},
    )
    with pytest.raises(CpasRfEvaluationError, match="forced unavailable"):
        execute_cpas_pipeline(db_session)

    db_session.expire_all()
    db_session.refresh(grant_a)
    db_session.refresh(grant_b)
    assert grant_a.terminated is False
    assert grant_b.terminated is False
    audits = [
        json.loads(r.data_json)
        for r in db_session.query(AdminInjectedData)
        .filter_by(kind=KIND_CPAS_AUDIT)
        .all()
    ]
    assert any(a.get("event") == "cpas_failed" for a in audits)
    assert not any(a.get("event") == "cpas_completed" for a in audits)
