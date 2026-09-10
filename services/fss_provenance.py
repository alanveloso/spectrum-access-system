"""Explicit FSS source provenance for grant-time IAP admission ().

Distinguishes federal database-update synchronization from administrative
InjectFss without encoding source into WInnForum FSS payloads or FAD export.

Unlabelled FSS records are treated as admin-injected (IAP-eligible).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from models.models import AdminInjectedData

KIND_FSS = "fss"
KIND_FSS_PROVENANCE = "fss_provenance"

SOURCE_FEDERAL_DB_SYNC = "federal_db_sync"
SOURCE_ADMIN_INJECTED = "admin_injected"

_FSS_IAP_KINDS = frozenset({"fss_cochannel", "fss_blocking"})


def fss_record_id(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
    if not isinstance(record, dict):
        return None
    rid = record.get("id")
    if rid is None or str(rid).strip() == "":
        return None
    return str(rid).strip()


def _map_from_row(row: AdminInjectedData) -> dict[str, str]:
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return {}
    by_id = data.get("by_id") if isinstance(data, dict) else None
    if not isinstance(by_id, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in by_id.items():
        if value in (SOURCE_FEDERAL_DB_SYNC, SOURCE_ADMIN_INJECTED):
            out[str(key)] = str(value)
    return out


def _live_provenance_rows(db: Session) -> list[AdminInjectedData]:
    rows = (
        db.query(AdminInjectedData)
        .filter_by(kind=KIND_FSS_PROVENANCE)
        .order_by(AdminInjectedData.id.asc())
        .all()
    )
    return [row for row in rows if not inspect(row).deleted]


def load_fss_provenance_map(db: Session) -> dict[str, str]:
    """Merge every sidecar row (defensive against a split map)."""
    out: dict[str, str] = {}
    for row in _live_provenance_rows(db):
        out.update(_map_from_row(row))
    return out


def persist_fss_provenance_map(db: Session, mapping: dict[str, str]) -> None:
    rows = _live_provenance_rows(db)
    if not mapping:
        for row in rows:
            db.delete(row)
        db.flush()
        return
    payload = json.dumps({"by_id": mapping}, sort_keys=True)
    if not rows:
        db.add(AdminInjectedData(kind=KIND_FSS_PROVENANCE, data_json=payload))
        db.flush()
        return
    rows[0].data_json = payload
    for row in rows[1:]:
        db.delete(row)
    db.flush()


def set_fss_provenance(db: Session, fss_id: str, source: str) -> None:
    if source not in (SOURCE_FEDERAL_DB_SYNC, SOURCE_ADMIN_INJECTED):
        raise ValueError(f"unsupported FSS provenance {source!r}")
    mapping = load_fss_provenance_map(db)
    mapping[str(fss_id)] = source
    persist_fss_provenance_map(db, mapping)


def fss_id_from_protection_point(point: Any) -> str | None:
    """Map an IAP FSS point to its underlying FSS record id.

 Uses ``source_entity_id`` when present (canonical). Falls back to the
 existing ``fss-cc:`` / ``fss-bl:`` point_id convention from
 ``protection_points_from_fss_payload``.
 """
    sid = getattr(point, "source_entity_id", None)
    if sid:
        return str(sid)
    kind = getattr(point, "entity_kind", None)
    kind_val = getattr(kind, "value", kind)
    if str(kind_val) not in _FSS_IAP_KINDS:
        return None
    pid = str(getattr(point, "point_id", "") or "")
    if pid.startswith("fss-cc:") or pid.startswith("fss-bl:"):
        return pid.split(":", 1)[1] or None
    return None


def is_federal_sync_fss(db: Session, fss_id: str | None) -> bool:
    if not fss_id:
        return False
    return load_fss_provenance_map(db).get(fss_id) == SOURCE_FEDERAL_DB_SYNC


def upsert_fss_payload(db: Session, payload: dict[str, Any]) -> str | None:
    """Replace FSS rows with the same record id, or insert. No provenance tag.

 Extra rows sharing the id are deleted so FAD leftovers cannot remain
 IAP-eligible beside the federal record.
 """
    rid = fss_record_id(payload)
    raw = json.dumps(payload, default=str)
    if rid is not None:
        kept: AdminInjectedData | None = None
        rows = db.query(AdminInjectedData).filter_by(kind=KIND_FSS).all()
        for row in rows:
            try:
                existing = json.loads(row.data_json or "{}")
            except json.JSONDecodeError:
                continue
            if fss_record_id(existing if isinstance(existing, dict) else None) != rid:
                continue
            if kept is None:
                row.data_json = raw
                kept = row
            else:
                db.delete(row)
        if kept is not None:
            db.flush()
            return rid
    db.add(AdminInjectedData(kind=KIND_FSS, data_json=raw))
    db.flush()
    return rid


def delete_federal_sync_fss_rows(db: Session) -> None:
    """Remove only FSS rows tagged federal_db_sync (and their provenance keys)."""
    mapping = load_fss_provenance_map(db)
    federal_ids = {k for k, v in mapping.items() if v == SOURCE_FEDERAL_DB_SYNC}
    if not federal_ids:
        return
    rows = db.query(AdminInjectedData).filter_by(kind=KIND_FSS).all()
    for row in rows:
        try:
            payload = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        rid = fss_record_id(payload if isinstance(payload, dict) else None)
        if rid is not None and rid in federal_ids:
            db.delete(row)
    for fid in federal_ids:
        mapping.pop(fid, None)
    persist_fss_provenance_map(db, mapping)


def exclude_federal_sync_fss_from_grant_admission(
    db: Session, points: list[Any]
) -> list[Any]:
    """Drop FSS_COCHANNEL/FSS_BLOCKING points whose FSS is federal-sync.

 Other entity kinds (ESC, PPA, GWPZ, injected FSS) are unchanged.
 """
    if not points:
        return list(points)
    mapping = load_fss_provenance_map(db)
    if not mapping:
        return list(points)
    out: list[Any] = []
    for point in points:
        kind = getattr(point, "entity_kind", None)
        kind_val = getattr(kind, "value", kind)
        if str(kind_val) not in _FSS_IAP_KINDS:
            out.append(point)
            continue
        fid = fss_id_from_protection_point(point)
        if fid is not None and mapping.get(fid) == SOURCE_FEDERAL_DB_SYNC:
            continue
        out.append(point)
    return out
