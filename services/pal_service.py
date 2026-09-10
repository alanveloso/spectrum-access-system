"""PAL (Priority Access License) persistence and lookup for marketplace integration."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData, PalRecord

ACTIVE_LICENSE_STATUSES = frozenset({"VALID"})


def _pal_frequencies(record: dict[str, Any]) -> tuple[int, int] | None:
    """Extract low/high Hz from channelAssignment or legacy palBlock."""
    assignment = (record.get("channelAssignment") or {}).get("primaryAssignment") or {}
    low = assignment.get("lowFrequency")
    high = assignment.get("highFrequency")
    if low is None or high is None:
        block = record.get("palBlock") or {}
        low = block.get("lowFrequency")
        high = block.get("highFrequency")
    if low is None or high is None:
        return None
    return int(low), int(high)


def _is_active_pal(record: dict[str, Any]) -> bool:
    status = (record.get("licenseStatus") or "VALID").upper()
    return status in ACTIVE_LICENSE_STATUSES


def pal_record_to_dict(row: PalRecord) -> dict[str, Any]:
    """Deserialize a stored PAL row to the WINNF JSON shape."""
    try:
        data = json.loads(row.record_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("palId", row.pal_id)
    data.setdefault("userId", row.user_id)
    if "channelAssignment" not in data:
        data["channelAssignment"] = {
            "primaryAssignment": {
                "lowFrequency": row.low_frequency,
                "highFrequency": row.high_frequency,
            }
        }
    data.setdefault("licenseStatus", row.license_status)
    if row.license_expiration and "license" not in data:
        data["license"] = {"licenseExpiration": row.license_expiration}
    elif row.license_expiration:
        lic = data.get("license")
        if isinstance(lic, dict):
            lic.setdefault("licenseExpiration", row.license_expiration)
    return data


def upsert_pal_record(
    db: Session, record: dict[str, Any], *, commit: bool = False
) -> PalRecord | None:
    """Insert or update a PAL record keyed by palId."""
    pal_id = record.get("palId")
    if not pal_id:
        return None

    freqs = _pal_frequencies(record)
    if freqs is None:
        return None

    user_id = str(record.get("userId") or "")
    license_status = str(record.get("licenseStatus") or "VALID").upper()
    license_exp = (record.get("license") or {}).get("licenseExpiration")

    row = db.query(PalRecord).filter_by(pal_id=pal_id).first()
    if row is None:
        row = PalRecord(pal_id=pal_id)
        db.add(row)

    row.user_id = user_id
    row.low_frequency = freqs[0]
    row.high_frequency = freqs[1]
    row.license_status = license_status
    row.license_expiration = license_exp
    row.record_json = json.dumps(record)
    if commit:
        db.commit()
    return row


def upsert_pal_records(
    db: Session, payload: Any, *, commit: bool = True
) -> int:
    """Persist one or many PAL records; returns count of upserted rows."""
    if payload is None:
        return 0
    records = payload if isinstance(payload, list) else [payload]
    count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if upsert_pal_record(db, rec, commit=False) is not None:
            count += 1
    if count and commit:
        db.commit()
    return count


def replace_pal_records(
    db: Session, payload: Any, *, commit: bool = True
) -> int:
    """Full-replace PAL table from a feed (insert/update + remove absent ids).

 Records present in the feed are upserted. Existing rows whose ``palId`` is
 absent from the feed are deleted (normative removal/expiration path).
 Invalid feed entries are skipped; an empty list clears all PALs.
 """
    if payload is None:
        raise ValueError("pal_payload_required")
    records = payload if isinstance(payload, list) else [payload]
    if not isinstance(records, list):
        raise ValueError("pal_payload_must_be_list_or_object")

    from services.data_injection_service import bump_injection_generation

    def _mutate() -> int:
        keep_ids: set[str] = set()
        count = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            row = upsert_pal_record(db, rec, commit=False)
            if row is None:
                continue
            keep_ids.add(row.pal_id)
            count += 1

        for row in list(db.query(PalRecord).all()):
            if row.pal_id not in keep_ids:
                db.delete(row)

        bump_injection_generation(db, "pal")
        return count

    if commit:
        from services.concurrency import (
            acquire_iap_admission_xact_lock,
            exclusive_iap_admission,
        )

        with exclusive_iap_admission():
            acquire_iap_admission_xact_lock(db)
            count = _mutate()
            db.commit()
        return count
    return _mutate()


def revoke_pal_record(db: Session, pal_id: str, *, commit: bool = True) -> bool:
    """Mark a PAL inactive or delete it; returns True when a row was affected."""
    row = db.query(PalRecord).filter_by(pal_id=pal_id).first()
    if row is None:
        return False
    from services.data_injection_service import bump_injection_generation

    if commit:
        from services.concurrency import (
            acquire_iap_admission_xact_lock,
            exclusive_iap_admission,
        )

        with exclusive_iap_admission():
            acquire_iap_admission_xact_lock(db)
            # Re-load under lock in case of concurrent revoke.
            row = db.query(PalRecord).filter_by(pal_id=pal_id).first()
            if row is None:
                return False
            db.delete(row)
            bump_injection_generation(db, "pal")
            db.commit()
        return True
    db.delete(row)
    bump_injection_generation(db, "pal")
    return True


def load_pal_records(db: Session, *, active_only: bool = False) -> list[dict[str, Any]]:
    """Load PAL records from the dedicated table, with legacy fallback."""
    pals: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in db.query(PalRecord).all():
        rec = pal_record_to_dict(row)
        pal_id = rec.get("palId")
        if not pal_id or (active_only and not _is_active_pal(rec)):
            continue
        pals.append(rec)
        seen.add(pal_id)

    # Legacy rows injected before PalRecord existed.
    for inj in db.query(AdminInjectedData).filter_by(kind="pal").all():
        try:
            rec = json.loads(inj.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        pal_id = rec.get("palId")
        if not pal_id or pal_id in seen:
            continue
        if active_only and not _is_active_pal(rec):
            continue
        pals.append(rec)

    return pals


def known_pal_ids(db: Session) -> set[str]:
    """Return all palId values known to the SAS."""
    return {str(p.get("palId")) for p in load_pal_records(db) if p.get("palId")}


def active_pals_for_user(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Return VALID PAL licenses owned by the given userId."""
    return [
        p
        for p in load_pal_records(db, active_only=True)
        if p.get("userId") == user_id
    ]
