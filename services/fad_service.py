"""Full Activity Dump generation for SAS↔SAS (v1.3) server role.

Produces a consistent snapshot:

- manifest with url/checksum/size/version/recordType;
- activity files for cbsd, zone, esc_sensor, coordination;
- shared generation timestamp across all files;
- SHA-1 checksum and UTF-8 byte size that match file bodies;
- optional pagination when a record type exceeds the configured page size;
- publication coordinated in PostgreSQL (``pg_advisory_xact_lock``) so only one
 dump is ``published``/current; historical ``ready`` snapshots may coexist.

No harness fixture device IDs or coordinates are hard-coded.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from config import get_settings
from models.models import (
    AdminInjectedData,
    Cbsd,
    EscSensor,
    FadDump,
    FadFile,
    Grant,
)
from services.clock import utc_now
from services.concurrency import acquire_fad_publish_xact_lock

logger = logging.getLogger(__name__)

# Schema FullActivityDump.files maxItems = 101 (4 types + paginated pages).
MANIFEST_MAX_FILES = 101
DEFAULT_MAX_RECORDS_PER_FILE = 500
RECORD_TYPES = ("cbsd", "zone", "esc_sensor", "coordination")

# SQLite / same-process aid only. PostgreSQL multi-worker safety is DB advisory.
_fad_publish_lock = threading.RLock()


def _sas_admin_id() -> str:
    return get_settings().sas_admin_id


def _fad_public_base() -> str:
    return get_settings().fad_public_base.rstrip("/")


def _sas_sas_version() -> str:
    return get_settings().sas_sas_version


# Backwards-compatible module-level names (resolved at import for harness defaults).
SAS_ADMIN_ID = get_settings().sas_admin_id
FAD_PUBLIC_BASE = get_settings().fad_public_base
SAS_SAS_VERSION = get_settings().sas_sas_version

_REGISTRATION_FIELDS = (
    "fccId",
    "cbsdCategory",
    "callSign",
    "airInterface",
    "measCapability",
    "installationParam",
    "groupingParam",
)


def max_records_per_file() -> int:
    raw = os.environ.get("SAS_FAD_MAX_RECORDS_PER_FILE", "").strip()
    if not raw:
        return DEFAULT_MAX_RECORDS_PER_FILE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_RECORDS_PER_FILE
    return max(1, min(value, 10_000))


def cbsd_reference_id(fcc_id: str, serial_number: str) -> str:
    """SAS↔SAS CBSD reference: {fccId}/{sha1(serialNumber)}."""
    digest = hashlib.sha1(serial_number.encode("utf-8")).hexdigest()
    return f"{fcc_id}/{digest}"


def fad_cbsd_id(fcc_id: str, serial_number: str) -> str:
    return f"cbsd/{cbsd_reference_id(fcc_id, serial_number)}"


def rewrite_esc_sensor_id(record_id: str | None) -> str:
    """Force esc_sensor/{AdminId}/... prefix required by FAD_1."""
    admin_id = _sas_admin_id()
    if not record_id:
        return f"esc_sensor/{admin_id}/0"
    parts = record_id.split("/")
    if len(parts) >= 3 and parts[0] == "esc_sensor":
        return f"esc_sensor/{admin_id}/{'/'.join(parts[2:])}"
    if len(parts) >= 2 and parts[0] == "esc_sensor":
        return f"esc_sensor/{admin_id}/{parts[1]}"
    return f"esc_sensor/{admin_id}/{record_id}"


def rewrite_zone_id(zone_id: str | None, *, fallback_suffix: str = "0") -> str:
    """Force zone/ppa/{AdminId}/... prefix required by FAD_1."""
    admin_id = _sas_admin_id()
    if not zone_id:
        return f"zone/ppa/{admin_id}/{fallback_suffix}"
    parts = zone_id.split("/")
    if len(parts) >= 3 and parts[0] == "zone" and parts[1] == "ppa":
        rest = "/".join(parts[3:]) if len(parts) > 3 else fallback_suffix
        return f"zone/ppa/{admin_id}/{rest}"
    return f"zone/ppa/{admin_id}/{fallback_suffix}"


def _fmt_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_registration(reg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _REGISTRATION_FIELDS:
        if key in reg and reg[key] is not None:
            out[key] = copy.deepcopy(reg[key])

    inst = out.get("installationParam")
    if isinstance(inst, dict):
        inst = dict(inst)
        azimuth = inst.get("antennaAzimuth")
        # Omni default when azimuth is absent (FAD_1 / WINNF-TS-0061).
        if azimuth is None:
            inst["antennaBeamwidth"] = 360
        out["installationParam"] = inst
    return out


def _operation_param_from_grant(grant: Grant) -> dict[str, Any]:
    try:
        req = json.loads(grant.grant_json or "{}")
    except json.JSONDecodeError:
        req = {}
    op = req.get("operationParam")
    if isinstance(op, dict) and "operationFrequencyRange" in op:
        return copy.deepcopy(op)
    return {
        "maxEirp": grant.max_eirp,
        "operationFrequencyRange": {
            "lowFrequency": grant.low_frequency,
            "highFrequency": grant.high_frequency,
        },
    }


def _build_grant_record(grant: Grant) -> dict[str, Any]:
    op = _operation_param_from_grant(grant)
    return {
        "id": grant.grant_id,
        "channelType": grant.channel_type,
        "grantExpireTime": _fmt_utc(grant.grant_expire_time),
        "operationParam": op,
        "requestedOperationParam": copy.deepcopy(op),
        "terminated": bool(grant.terminated),
    }


def _build_cbsd_records(db: Session) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    # Only currently registered CBSDs belong in the activity dump.
    cbsds = (
        db.query(Cbsd)
        .filter(Cbsd.lifecycle_state == "REGISTERED")
        .order_by(Cbsd.id)
        .all()
    )
    for cbsd in cbsds:
        try:
            reg = json.loads(cbsd.registration_json or "{}")
        except json.JSONDecodeError:
            reg = {}
        grants = (
            db.query(Grant)
            .filter_by(cbsd_pk=cbsd.id, terminated=False)
            .order_by(Grant.id)
            .all()
        )
        records.append(
            {
                "id": fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number),
                "registration": _build_registration(reg),
                "grants": [_build_grant_record(g) for g in grants],
            }
        )
    return records


def _operational_to_reference_id(ref: str, db: Session) -> str:
    """Convert operational cbsdId ({fcc}/{serial}) to SAS↔SAS reference id."""
    cbsd = db.query(Cbsd).filter_by(cbsd_id=ref).first()
    if cbsd:
        return cbsd_reference_id(cbsd.fcc_id, cbsd.cbsd_serial_number)
    # Already a reference id, or unknown — pass through if it looks hashed.
    parts = ref.split("/")
    if len(parts) == 2 and len(parts[1]) == 40:
        return ref
    if ref.startswith("cbsd/"):
        return ref[len("cbsd/") :]
    # Last resort: treat as fccId/serial and hash serial.
    if len(parts) == 2:
        return cbsd_reference_id(parts[0], parts[1])
    return ref


def _build_zone_records(db: Session) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    rows = (
        db.query(AdminInjectedData)
        .filter_by(kind="zone")
        .order_by(AdminInjectedData.id)
        .all()
    )
    for row in rows:
        try:
            payload = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        record = copy.deepcopy(payload.get("record") or payload)
        if not isinstance(record, dict):
            continue
        if record.get("usage") not in (None, "PPA") and "ppaInfo" not in record:
            continue
        if record.get("terminated") is True:
            continue
        record["id"] = rewrite_zone_id(record.get("id"), fallback_suffix=str(row.id))
        ppa_info = record.get("ppaInfo")
        if isinstance(ppa_info, dict):
            refs = ppa_info.get("cbsdReferenceId") or []
            ppa_info["cbsdReferenceId"] = [
                _operational_to_reference_id(str(r), db) for r in refs
            ]
            record["ppaInfo"] = ppa_info
        records.append(record)
    return records


def _build_esc_records(db: Session) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in db.query(EscSensor).order_by(EscSensor.id).all():
        try:
            record = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record = copy.deepcopy(record)
        record["id"] = row.record_id
        records.append(record)
    return records


def _build_coordination_records(_db: Session) -> list[dict[str, Any]]:
    """Coordination dump body (empty until multi-SAS coordination events exist)."""
    return []


def _chunk_records(
    records: list[dict[str, Any]], *, page_size: int
) -> list[list[dict[str, Any]]]:
    """Split records into pages; always return at least one (possibly empty) page."""
    if not records:
        return [[]]
    return [records[i : i + page_size] for i in range(0, len(records), page_size)]


def _make_dump_file(
    *,
    record_type: str,
    filename: str,
    record_data: list[dict[str, Any]],
    timestamp: str,
) -> tuple[dict[str, Any], str, str]:
    """Return (manifest entry, url_path, content_json)."""
    envelope = {
        "startTime": timestamp,
        "endTime": timestamp,
        "recordData": record_data,
    }
    content = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    version = _sas_sas_version()
    url_path = f"/{version}/{record_type}/{filename}"
    encoded = content.encode("utf-8")
    entry = {
        "url": f"{_fad_public_base()}{url_path}",
        "checksum": hashlib.sha1(encoded).hexdigest(),
        "size": len(encoded),
        "version": version,
        "recordType": record_type,
    }
    return entry, url_path, content


def _assert_entry_matches_content(entry: dict[str, Any], content: str) -> None:
    encoded = content.encode("utf-8")
    digest = hashlib.sha1(encoded).hexdigest()
    if entry.get("checksum") != digest:
        raise RuntimeError("FAD checksum mismatch against file body")
    if int(entry.get("size") or -1) != len(encoded):
        raise RuntimeError("FAD size mismatch against file body")


@dataclass(frozen=True)
class _FadSnapshotPayload:
    """Validated in-memory snapshot ready for a short DB publication section."""

    timestamp: str
    description: str
    manifest_json: str
    file_rows: tuple[tuple[str, str, str, dict[str, Any]], ...]


def _dialect_name(db: Session) -> str:
    bind = db.get_bind()
    if bind is None:
        return ""
    return bind.dialect.name


def _build_snapshot_payload(db: Session) -> _FadSnapshotPayload:
    """Serialize dump bodies outside any publication lock (heavy work)."""
    now = utc_now()
    timestamp = _fmt_utc(now)
    page_size = max_records_per_file()

    builders = {
        "cbsd": _build_cbsd_records,
        "zone": _build_zone_records,
        "esc_sensor": _build_esc_records,
        "coordination": _build_coordination_records,
    }

    files_meta: list[dict[str, Any]] = []
    file_rows: list[tuple[str, str, str, dict[str, Any]]] = []

    for record_type in RECORD_TYPES:
        pages = _chunk_records(builders[record_type](db), page_size=page_size)
        for page_index, page in enumerate(pages):
            filename = f"activity_dump_file_{record_type}{page_index}.json"
            entry, url_path, content = _make_dump_file(
                record_type=record_type,
                filename=filename,
                record_data=page,
                timestamp=timestamp,
            )
            _assert_entry_matches_content(entry, content)
            files_meta.append(entry)
            file_rows.append((record_type, url_path, content, entry))

    if len(files_meta) > MANIFEST_MAX_FILES:
        raise RuntimeError(
            f"FAD would emit {len(files_meta)} files (max {MANIFEST_MAX_FILES}); "
            "increase SAS_FAD_MAX_RECORDS_PER_FILE"
        )

    for _rtype, _path, content, _entry in file_rows:
        body = json.loads(content)
        if body.get("startTime") != timestamp or body.get("endTime") != timestamp:
            raise RuntimeError("FAD snapshot timestamp inconsistency")

    description = "Full activity dump files"
    manifest = {
        "files": files_meta,
        "generationDateTime": timestamp,
        "description": description,
    }
    manifest_json = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
    return _FadSnapshotPayload(
        timestamp=timestamp,
        description=description,
        manifest_json=manifest_json,
        file_rows=tuple(file_rows),
    )


def _publish_snapshot(
    db: Session, payload: _FadSnapshotPayload, *, commit: bool = True
) -> FadDump:
    """Short critical section: advisory lock → persist complete dump → publish.

 When ``commit=False`` the dump is flushed only; the caller must commit while
 still holding any outer authorization locks (CPAS apply+admission stamp).
 """
    try:
        acquire_fad_publish_xact_lock(db)

        # Persist as ready (complete) but not yet current, then flip published.
        dump = FadDump(
            generation_datetime=payload.timestamp,
            description=payload.description,
            manifest_json=payload.manifest_json,
            ready=True,
            published=False,
        )
        db.add(dump)
        db.flush()

        for record_type, url_path, content, entry in payload.file_rows:
            db.add(
                FadFile(
                    dump_id=dump.id,
                    record_type=record_type,
                    url_path=url_path,
                    checksum=entry["checksum"],
                    size=entry["size"],
                    content_json=content,
                )
            )
        db.flush()

        # Final integrity before making this dump observable as current.
        report = verify_ready_dump_integrity(db, dump)
        if not report.get("ok"):
            raise RuntimeError(f"FAD publish integrity failed: {report}")

        db.query(FadDump).filter_by(published=True).update({"published": False})
        dump.published = True
        if commit:
            db.commit()
            db.refresh(dump)
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        logger.exception("FAD publication failed; rolled back")
        raise

    logger.info(
        "FAD published generation=%s dump_id=%s files=%s",
        payload.timestamp,
        dump.id,
        len(payload.file_rows),
    )
    return dump


def create_full_activity_dump(db: Session, *, commit: bool = True) -> FadDump:
    """Generate a complete snapshot then publish it as the current FAD.

 Heavy serialization runs without holding DB advisory locks. Publication is
 coordinated with ``pg_advisory_xact_lock`` on PostgreSQL. SQLite keeps a
 process-local RLock around publish only (test aid; not multi-worker proof).

 ``commit=False`` is for callers (CPAS) that must keep apply + FAD + admission
 stamp in one transaction under IAP admission serialization.
 """
    payload = _build_snapshot_payload(db)
    if _dialect_name(db) == "sqlite":
        with _fad_publish_lock:
            return _publish_snapshot(db, payload, commit=commit)
    return _publish_snapshot(db, payload, commit=commit)


def get_published_dump(db: Session) -> FadDump | None:
    """Return the single current/published Full Activity Dump, if any."""
    return (
        db.query(FadDump)
        .filter_by(published=True)
        .order_by(FadDump.id.desc())
        .first()
    )


def get_latest_ready_dump(db: Session) -> FadDump | None:
    """Alias for the current published dump (SAS↔SAS peers / admin).

    Historical ``ready=True`` dumps may exist; only ``published=True`` is current.
    """
    return get_published_dump(db)


def get_dump_file_by_path(db: Session, url_path: str) -> FadFile | None:
    """Resolve a dump file belonging to the published snapshot only.

    Legacy filename fallback applies only to a bare basename (no directories),
    scoped to ``dump_id`` of the published FAD — never searches other snapshots
    and never treats arbitrary filesystem paths as resolvable URLs.
    """
    dump = get_published_dump(db)
    if dump is None:
        return None
    raw = (url_path or "").strip()
    if not raw or "\\" in raw or ".." in raw.split("/"):
        return None

    normalized = raw if raw.startswith("/") else f"/{raw}"
    row = (
        db.query(FadFile)
        .filter_by(dump_id=dump.id, url_path=normalized)
        .first()
    )
    if row:
        return row

    # Legacy bare-filename lookup (unique within the published dump only).
    segments = [s for s in raw.split("/") if s]
    if len(segments) != 1:
        return None
    filename = segments[0]
    if filename in (".", "..") or "/" in filename or "\\" in filename:
        return None
    matches = (
        db.query(FadFile)
        .filter(FadFile.dump_id == dump.id, FadFile.url_path.endswith("/" + filename))
        .all()
    )
    if len(matches) != 1:
        return None
    return matches[0]


def verify_ready_dump_integrity(db: Session, dump: FadDump | None = None) -> dict[str, Any]:
    """Return integrity report for a dump (defaults to published/current)."""
    target = dump or get_published_dump(db)
    if target is None:
        return {"ok": False, "reason": "no_ready_dump"}
    try:
        manifest = json.loads(target.manifest_json or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "reason": "bad_manifest_json"}
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return {"ok": False, "reason": "missing_files"}
    for key in ("generationDateTime", "description"):
        if key not in manifest:
            return {"ok": False, "reason": f"missing_{key}"}
    gen = manifest["generationDateTime"]
    for entry in files:
        for req in ("url", "checksum", "size", "version", "recordType"):
            if req not in entry:
                return {"ok": False, "reason": f"missing_entry_{req}"}
        path = urlparse(entry["url"]).path or ""
        if not path.startswith("/"):
            path = "/" + path
        row = (
            db.query(FadFile)
            .filter_by(dump_id=target.id, url_path=path)
            .first()
        )
        if row is None:
            return {"ok": False, "reason": "file_row_missing", "path": path}
        try:
            _assert_entry_matches_content(
                {"checksum": entry["checksum"], "size": entry["size"]},
                row.content_json,
            )
        except RuntimeError as exc:
            return {"ok": False, "reason": str(exc), "path": path}
        body = json.loads(row.content_json)
        if body.get("startTime") != gen or body.get("endTime") != gen:
            return {"ok": False, "reason": "timestamp_mismatch", "path": path}
    present_types = {e["recordType"] for e in files}
    missing = [t for t in RECORD_TYPES if t not in present_types]
    if missing:
        return {"ok": False, "reason": "missing_record_types", "missing": missing}
    return {
        "ok": True,
        "generationDateTime": gen,
        "fileCount": len(files),
        "dumpId": target.id,
        "published": bool(target.published),
        "ready": bool(target.ready),
    }
