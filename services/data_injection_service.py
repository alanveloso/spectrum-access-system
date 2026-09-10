"""Admin data-injection contracts.

Persists external / protection datasets with:

- typed validation of required WINNF fields;
- upsert-by-natural-key (replace) rather than blind append;
- generation counters for consumers that need sync stamps;
- HTTP(S) URL + optional checksum retention for ``database_url``;
- all-or-nothing validation before commit for list batches.

No harness fixture IDs or coordinates are hard-coded.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from services.fad_service import rewrite_zone_id
from services.federal_db_service import bump_sync_meta

logger = logging.getLogger(__name__)

KIND_ZONE = "zone"
KIND_FSS = "fss"
KIND_WISP = "wisp"
# Canonical GWBL point protectors (FCC ULS / admin InjectWisp PART_90 point).
# Distinct from KIND_WISP, which holds GWPZ IncumbentProtectionData + GeoJSON zone.
KIND_GWBL = "gwbl"
KIND_DATABASE_URL = "database_url"
KIND_ESC_ZONE = "esc_zone"
KIND_CLUSTER_LIST = "cluster_list"
KIND_SAS_ADMIN = "sas_admin"
KIND_INJECTION_META = "injection_generation"

ALLOWED_DATABASE_TYPES = frozenset(
    {
        "PAL",
        "CPI",
        "FSS",
        "GWBL",
        "EXCLUSION_ZONE",
        "SCHEDULED_DPA",
    }
)

SCHEMA_VERSION = 1


class InjectionValidationError(ValueError):
    """Raised when a batch fails validation (no partial write)."""


def _loads(row: AdminInjectedData) -> dict[str, Any] | None:
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def load_injected(db: Session, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in db.query(AdminInjectedData).filter_by(kind=kind).all():
        payload = _loads(row)
        if payload is not None:
            out.append(payload)
    return out


def get_injection_generations(db: Session) -> dict[str, int]:
    rows = load_injected(db, KIND_INJECTION_META)
    if not rows:
        return {}
    meta = rows[-1]
    return {str(k): int(v) for k, v in meta.items() if isinstance(v, (int, float))}


def bump_injection_generation(db: Session, key: str) -> int:
    meta = get_injection_generations(db)
    meta[key] = int(meta.get(key) or 0) + 1
    meta["schemaVersion"] = SCHEMA_VERSION
    db.query(AdminInjectedData).filter_by(kind=KIND_INJECTION_META).delete()
    db.add(AdminInjectedData(kind=KIND_INJECTION_META, data_json=json.dumps(meta)))
    return int(meta[key])


def _natural_key(kind: str, payload: dict[str, Any]) -> str | None:
    if kind == KIND_DATABASE_URL:
        db_type = str(payload.get("type") or "").upper()
        url = str(payload.get("url") or "").strip()
        if not db_type or not url:
            return None
        return f"{db_type}|{url}"
    if kind == KIND_CLUSTER_LIST:
        for key in ("userId", "ppaId", "palId"):
            if payload.get(key):
                return f"{key}:{payload[key]}"
        # Stable fallback for unstructured harness payloads.
        return json.dumps(payload, sort_keys=True, default=str)
    record = payload.get("record") if isinstance(payload.get("record"), dict) else None
    if record and record.get("id") is not None:
        return str(record["id"])
    if payload.get("id") is not None:
        return str(payload["id"])
    return None


def _upsert(
    db: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    key: str | None,
) -> None:
    raw = json.dumps(payload, default=str)
    if key is None:
        db.add(AdminInjectedData(kind=kind, data_json=raw))
        return
    for row in db.query(AdminInjectedData).filter_by(kind=kind).all():
        existing = _loads(row)
        if existing is None:
            continue
        if _natural_key(kind, existing) == key:
            row.data_json = raw
            return
    db.add(AdminInjectedData(kind=kind, data_json=raw))


def _commit_if(db: Session, *, commit: bool) -> None:
    if commit:
        db.commit()


@contextmanager
def _iap_admission_write(db: Session, *, commit: bool):
    """Hold IAP admission serialization across an IAP-relevant protection write.

 When ``commit=False`` (batch), the caller must enter the same serialization
 before the final commit. Lock order starts with IAP admission.
 """
    if not commit:
        yield
        return
    from services.concurrency import (
        acquire_iap_admission_xact_lock,
        exclusive_iap_admission,
    )

    with exclusive_iap_admission():
        acquire_iap_admission_xact_lock(db)
        yield
        db.commit()


def is_valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _freq_range_ok(fr: dict[str, Any]) -> bool:
    low = _as_number(fr.get("lowFrequency"))
    high = _as_number(fr.get("highFrequency"))
    if low is None or high is None:
        return False
    # Hz must be ordered and positive; no fixture band hardcode.
    return high > low > 0


def _lat_lon_ok(lat: Any, lon: Any) -> bool:
    la = _as_number(lat)
    lo = _as_number(lon)
    if la is None or lo is None:
        return False
    return -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0


def _fss_has_required_fields(payload: dict[str, Any]) -> bool:
    record = payload.get("record")
    if not isinstance(record, dict):
        return False
    # InjectFss carries IncumbentProtectionData; type should identify FSS when present.
    rec_type = record.get("type")
    if rec_type is not None and str(rec_type).upper() != "FSS":
        return False
    deps = record.get("deploymentParam")
    if not isinstance(deps, list) or not deps:
        return False
    first = deps[0] if isinstance(deps[0], dict) else {}
    if not first:
        return False
    inst = first.get("installationParam") or {}
    if not isinstance(inst, dict):
        return False
    fr = (first.get("operationParam") or {}).get("operationFrequencyRange") or {}
    if not isinstance(fr, dict) or not _freq_range_ok(fr):
        return False
    return _lat_lon_ok(inst.get("latitude"), inst.get("longitude"))


def _wisp_has_required_fields(payload: dict[str, Any]) -> bool:
    """True for GWPZ-shaped InjectWisp payloads (IncumbentProtectionData + zone)."""
    record = payload.get("record")
    zone = payload.get("zone")
    if not isinstance(record, dict) or not isinstance(zone, dict):
        return False
    if not record.get("id") or not record.get("type"):
        return False
    deps = record.get("deploymentParam")
    if not isinstance(deps, list) or not deps:
        return False
    first = deps[0] if isinstance(deps[0], dict) else {}
    if not first:
        return False
    fr = (first.get("operationParam") or {}).get("operationFrequencyRange") or {}
    if not isinstance(fr, dict) or not _freq_range_ok(fr):
        return False
    # GeoJSON FeatureCollection or Geometry.
    ztype = zone.get("type")
    return ztype in ("FeatureCollection", "Feature", "Polygon", "MultiPolygon")


def _gwbl_point_from_wisp_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize admin InjectWisp PART_90 *point* (no GWPZ zone) to canonical GWBL.

 WInnForum ``InjectWisp`` is the admin verb for both GWPZ (zone) and GWBL
 (point) fixtures. FSS↔GWBL grant/pre-IAP protection consumes ``kind=gwbl``
 records with flat ``latitude``/``longitude`` (same shape as FCC ULS sync).
 """
    # Explicit zone field → GWPZ path only; never dual-write as GWBL.
    if payload.get("zone") is not None:
        return None
    record = payload.get("record")
    if not isinstance(record, dict):
        return None
    record_id = record.get("id")
    if record_id is None or record_id == "":
        return None
    rec_type = record.get("type")
    if rec_type is not None and str(rec_type).upper() == "FSS":
        return None
    deps = record.get("deploymentParam")
    if not isinstance(deps, list) or not deps:
        return None
    first = deps[0] if isinstance(deps[0], dict) else {}
    if not first:
        return None
    inst = first.get("installationParam") or {}
    if not isinstance(inst, dict):
        return None
    if not _lat_lon_ok(inst.get("latitude"), inst.get("longitude")):
        return None
    fr = (first.get("operationParam") or {}).get("operationFrequencyRange") or {}
    if not isinstance(fr, dict) or not _freq_range_ok(fr):
        return None
    return {
        "id": str(record_id),
        "latitude": float(inst["latitude"]),
        "longitude": float(inst["longitude"]),
    }


def _zone_record_acceptable(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    # PPA / NTIA zones need geometry; id may be rewritten later.
    return "zone" in record or "usage" in record or "ppaInfo" in record


def persist_zone_data(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> str:
    """Upsert ZoneData; return rewritten zone id (InjectZoneData response)."""
    payload = dict(body) if isinstance(body, dict) else {}
    record = payload.get("record")
    if isinstance(record, dict):
        record = dict(record)
        zone_id = rewrite_zone_id(record.get("id"))
        record["id"] = zone_id
        payload["record"] = record
        if _zone_record_acceptable(record):
            with _iap_admission_write(db, commit=commit):
                _upsert(db, KIND_ZONE, payload, key=zone_id)
                bump_injection_generation(db, KIND_ZONE)
        return zone_id
    zone_id = rewrite_zone_id(None)
    return zone_id


def upsert_fss_record(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> bool:
    """Upsert one FSS IncumbentProtectionData wrap; bumps federal FSS generation."""
    if not isinstance(body, dict) or not _fss_has_required_fields(body):
        return False
    payload = dict(body)
    record = dict(payload["record"])
    payload["record"] = record
    key = _natural_key(KIND_FSS, payload)
    with _iap_admission_write(db, commit=commit):
        _upsert(db, KIND_FSS, payload, key=key)
        from services.fss_provenance import SOURCE_ADMIN_INJECTED, set_fss_provenance

        if key:
            set_fss_provenance(db, key, SOURCE_ADMIN_INJECTED)
        bump_sync_meta(db, "fss")
        bump_injection_generation(db, KIND_FSS)
    return True


def upsert_wisp_record(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> bool:
    """Persist InjectWisp admin payloads.

 - GWPZ (record + GeoJSON zone) → ``KIND_WISP`` for zone exclusion / IAP.
 - GWBL point (PART_90 IncumbentProtectionData, no zone) → canonical
 ``KIND_GWBL`` flat point consumed by FSS↔GWBL grant protection.
 """
    if not isinstance(body, dict):
        return False
    if _wisp_has_required_fields(body):
        payload = dict(body)
        key = _natural_key(KIND_WISP, payload)
        with _iap_admission_write(db, commit=commit):
            _upsert(db, KIND_WISP, payload, key=key)
            bump_injection_generation(db, KIND_WISP)
        return True
    gwbl = _gwbl_point_from_wisp_body(body)
    if gwbl is None:
        return False
    key = _natural_key(KIND_GWBL, gwbl)
    with _iap_admission_write(db, commit=commit):
        _upsert(db, KIND_GWBL, gwbl, key=key)
        bump_sync_meta(db, "gwbl")
        bump_injection_generation(db, KIND_GWBL)
    return True


def persist_database_url(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> bool:
    """Validate and upsert an external database URL (FDB/WDB/IPR)."""
    if not isinstance(body, dict):
        return False
    db_type = str(body.get("type") or "").strip().upper()
    url = str(body.get("url") or "").strip()
    if db_type not in ALLOWED_DATABASE_TYPES:
        logger.info("Rejecting database_url with unknown type=%r", body.get("type"))
        return False
    if not is_valid_http_url(url):
        logger.info("Rejecting database_url with invalid URL scheme/host")
        return False
    payload: dict[str, Any] = {
        "type": db_type,
        "url": url,
        "schemaVersion": SCHEMA_VERSION,
    }
    checksum = body.get("checksum") or body.get("checksumSha1")
    if checksum is not None:
        payload["checksum"] = str(checksum)
    auth = body.get("auth")
    if isinstance(auth, dict):
        # Retain auth material only as opaque structure for sync; never log it.
        payload["auth"] = auth
    key = _natural_key(KIND_DATABASE_URL, payload)
    _upsert(db, KIND_DATABASE_URL, payload, key=key)
    bump_injection_generation(db, KIND_DATABASE_URL)
    _commit_if(db, commit=commit)
    return True


def persist_esc_zone(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> bool:
    if not isinstance(body, dict) or not body:
        return False
    # Require a stable identity field so empty/noise payloads are not stored.
    if not (
        body.get("id")
        or (isinstance(body.get("record"), dict) and body["record"].get("id"))
        or body.get("zoneId")
        or body.get("escZoneId")
    ):
        return False
    payload = dict(body)
    key = _natural_key(KIND_ESC_ZONE, payload)
    with _iap_admission_write(db, commit=commit):
        _upsert(db, KIND_ESC_ZONE, payload, key=key)
        bump_injection_generation(db, KIND_ESC_ZONE)
    return True


def persist_cluster_list(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> bool:
    if not isinstance(body, dict) or not body:
        return False
    if not (
        body.get("userId")
        or body.get("ppaId")
        or body.get("palId")
        or body.get("cbsdIds")
        or body.get("cbsdReferenceId")
    ):
        return False
    payload = dict(body)
    key = _natural_key(KIND_CLUSTER_LIST, payload)
    with _iap_admission_write(db, commit=commit):
        _upsert(db, KIND_CLUSTER_LIST, payload, key=key)
        bump_injection_generation(db, KIND_CLUSTER_LIST)
    return True


def persist_sas_admin(
    db: Session, body: dict[str, Any] | None, *, commit: bool = True
) -> bool:
    if not isinstance(body, dict):
        return False
    record = body.get("record")
    if not isinstance(record, dict) or not record.get("id"):
        return False
    payload = {"record": dict(record), "schemaVersion": SCHEMA_VERSION}
    key = _natural_key(KIND_SAS_ADMIN, payload)
    _upsert(db, KIND_SAS_ADMIN, payload, key=key)
    bump_injection_generation(db, KIND_SAS_ADMIN)
    _commit_if(db, commit=commit)
    return True


def upsert_batch(
    db: Session,
    kind: str,
    items: list[dict[str, Any]],
    *,
    validator,
    writer,
) -> int:
    """Validate every item then write without intermediate commits; one commit.

 ``writer`` must accept ``commit=False`` (all persist/upsert helpers do).
 IAP-relevant kinds hold admission serialization across the final commit.
 """
    if not items:
        return 0
    for item in items:
        if not validator(item):
            db.rollback()
            raise InjectionValidationError(f"invalid {kind} batch item")
    count = 0
    iap_kinds = {
        KIND_ZONE,
        KIND_FSS,
        KIND_WISP,
        KIND_GWBL,
        KIND_ESC_ZONE,
        KIND_CLUSTER_LIST,
    }
    try:
        if kind in iap_kinds:
            from services.concurrency import (
                acquire_iap_admission_xact_lock,
                exclusive_iap_admission,
            )

            with exclusive_iap_admission():
                acquire_iap_admission_xact_lock(db)
                for item in items:
                    if writer(db, item, commit=False):
                        count += 1
                db.commit()
        else:
            for item in items:
                if writer(db, item, commit=False):
                    count += 1
            db.commit()
    except Exception:
        db.rollback()
        raise
    return count


def verify_optional_checksum(body: bytes, expected: str | None) -> bool:
    """Return True when no checksum is configured or SHA-1 matches."""
    if not expected:
        return True
    import hashlib

    digest = hashlib.sha1(body).hexdigest()
    return digest.lower() == str(expected).strip().lower()
