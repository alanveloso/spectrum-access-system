"""ESC Admin triggers (detection / reset / disconnect) for IPR and related suites.

Persists verifiable control-plane state consumed by heartbeat / DPA protection
and frozen into CPAS ``protection_records`` for EPR consistency.

No harness fixture DPA names or device IDs are hard-coded.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from services.clock import utc_now
from services.meas_report import admin_flag_set

FLAG_ESC_DETECTION = "esc_detection"
FLAG_ESC_DISCONNECTED = "esc_disconnected"
FLAG_ESC_ABSENT = "esc_absent"
KIND_ESC_AUDIT = "esc_admin_audit"
KIND_ESC_STATE = "esc_state"


class EscConnectivityState(str, Enum):
    """ESC network / presence state for EPR (not truthiness)."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class EscConnectivityError(ValueError):
    """Invalid ESC connectivity payload."""


def _append_audit(db: Session, event: str, detail: dict[str, Any]) -> None:
    db.add(
        AdminInjectedData(
            kind=KIND_ESC_AUDIT,
            data_json=json.dumps(
                {
                    "event": event,
                    "at": utc_now().replace(microsecond=0).isoformat(),
                    **detail,
                },
                default=str,
            ),
        )
    )


def _write_flag(db: Session, kind: str, payload: dict[str, Any]) -> None:
    """Upsert flag row without committing (caller owns the transaction)."""
    existing = db.query(AdminInjectedData).filter_by(kind=kind).first()
    raw = json.dumps(payload)
    if existing:
        existing.data_json = raw
    else:
        db.add(AdminInjectedData(kind=kind, data_json=raw))


def apply_esc_detection(db: Session, body: dict[str, Any] | None) -> dict[str, Any]:
    """Record ESC detection event (TriggerEscZone)."""
    payload = dict(body) if isinstance(body, dict) else {}
    payload.setdefault("detected", True)
    payload["updatedAt"] = utc_now().replace(microsecond=0).isoformat()
    _write_flag(db, FLAG_ESC_DETECTION, payload)
    _append_audit(db, "esc_detection", {"keys": sorted(str(k) for k in payload.keys())})
    db.commit()
    return payload


def reset_esc_zone(db: Session) -> None:
    """Clear ESC detection state (ResetEscZone)."""
    db.query(AdminInjectedData).filter_by(kind=FLAG_ESC_DETECTION).delete()
    _append_audit(db, "esc_reset", {})
    db.commit()


def disconnect_esc(db: Session) -> dict[str, Any]:
    """Mark ESC-DE disconnected (TriggerEscDisconnect / IPR)."""
    payload = {
        "disconnected": True,
        "disconnectedAt": utc_now().replace(microsecond=0).isoformat(),
    }
    _write_flag(db, FLAG_ESC_DISCONNECTED, payload)
    _append_audit(db, "esc_disconnect", {})
    db.commit()
    return payload


def is_esc_disconnected(db: Session) -> bool:
    if not admin_flag_set(db, FLAG_ESC_DISCONNECTED):
        return False
    row = db.query(AdminInjectedData).filter_by(kind=FLAG_ESC_DISCONNECTED).first()
    if not row:
        return False
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        # Fail closed for RF: treat corrupt disconnect flag as disconnected.
        return True
    if isinstance(data, dict) and "disconnected" in data:
        return bool(data.get("disconnected"))
    return True


def set_esc_absent(db: Session, *, absent: bool = True) -> dict[str, Any]:
    """Mark that no ESC is present (IPR.1 — protect all ESC-monitored DPAs)."""
    if not absent:
        db.query(AdminInjectedData).filter_by(kind=FLAG_ESC_ABSENT).delete()
        _append_audit(db, "esc_absent_cleared", {})
        db.commit()
        return {"absent": False}
    payload = {
        "absent": True,
        "updatedAt": utc_now().replace(microsecond=0).isoformat(),
    }
    _write_flag(db, FLAG_ESC_ABSENT, payload)
    _append_audit(db, "esc_absent", {})
    db.commit()
    return payload


def is_esc_absent(db: Session) -> bool:
    if not admin_flag_set(db, FLAG_ESC_ABSENT):
        return False
    row = db.query(AdminInjectedData).filter_by(kind=FLAG_ESC_ABSENT).first()
    if not row:
        return False
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return True
    if isinstance(data, dict) and "absent" in data:
        return bool(data.get("absent"))
    return True


def esc_detection_active(db: Session) -> bool:
    return bool(admin_flag_set(db, FLAG_ESC_DETECTION))


def resolve_esc_connectivity(db: Session) -> EscConnectivityState:
    """Resolve ESC connectivity without truthiness shortcuts.

 Precedence: absent > disconnected > connected (default when no flags).
 Corrupt flag payloads → INVALID (fail-closed consumers).
 """
    if admin_flag_set(db, FLAG_ESC_ABSENT):
        row = db.query(AdminInjectedData).filter_by(kind=FLAG_ESC_ABSENT).first()
        if row is None:
            return EscConnectivityState.INVALID
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            return EscConnectivityState.INVALID
        if not isinstance(data, dict) or "absent" not in data:
            return EscConnectivityState.INVALID
        if bool(data.get("absent")):
            return EscConnectivityState.ABSENT
    if admin_flag_set(db, FLAG_ESC_DISCONNECTED):
        row = db.query(AdminInjectedData).filter_by(kind=FLAG_ESC_DISCONNECTED).first()
        if row is None:
            return EscConnectivityState.INVALID
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            return EscConnectivityState.INVALID
        if not isinstance(data, dict) or "disconnected" not in data:
            return EscConnectivityState.INVALID
        if bool(data.get("disconnected")):
            return EscConnectivityState.DISCONNECTED
    return EscConnectivityState.CONNECTED


def parse_frozen_esc_connectivity(payload: dict[str, Any]) -> EscConnectivityState:
    """Parse frozen esc_state record; missing/invalid → domain error."""
    if not isinstance(payload, dict):
        raise EscConnectivityError("esc_state payload must be an object")
    raw = payload.get("state")
    if raw is None:
        raise EscConnectivityError("esc_state missing state")
    try:
        return EscConnectivityState(str(raw))
    except ValueError as exc:
        raise EscConnectivityError(f"esc_state invalid: {raw!r}") from exc


def capture_esc_connectivity_for_freeze(db: Session) -> tuple[str, str, str]:
    """Freeze ESC connectivity into protection_records generation N."""
    state = resolve_esc_connectivity(db)
    payload = {
        "state": state.value,
        "disconnected": state is EscConnectivityState.DISCONNECTED,
        "absent": state is EscConnectivityState.ABSENT,
    }
    return (
        KIND_ESC_STATE,
        "connectivity",
        json.dumps(payload, sort_keys=True),
    )
