"""Scheduled daily CPAS: enable flag, window tick, audit, anti-dup.

Harness FDB_8 enables the schedule then waits for the agreed CPAS window
(US/Pacific 02:00–04:00 by default). The UUT must run the same
``execute_cpas_pipeline`` / ``trigger_daily_activities`` entrypoint used by
``TriggerDailyActivitiesImmediately`` — not a parallel certification-only path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from services.clock import utc_now
from services.cpas_service import is_cpas_running, trigger_daily_activities
from services.meas_report import admin_flag_set

logger = logging.getLogger(__name__)

FLAG_SCHEDULE_ENABLED = "scheduled_daily_activities"
KIND_AUDIT = "cpas_schedule_audit"
KIND_META = "cpas_schedule_meta"

DEFAULT_TIMEZONE = "US/Pacific"
DEFAULT_START_HOUR = 2
DEFAULT_END_HOUR = 4
DEFAULT_TICK_SECONDS = 30.0

_scheduler_lock = threading.RLock()
_tick_dispatch_lock = threading.RLock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _clamp_hour(value: int) -> int:
    return max(0, min(23, int(value)))


def schedule_timezone_name() -> str:
    return os.environ.get("SAS_CPAS_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def resolve_schedule_zone(tz_name: str | None = None) -> ZoneInfo:
    """Resolve IANA timezone; raises ValueError when unknown."""
    name = (tz_name or schedule_timezone_name()).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError and aliases
        raise ValueError(f"invalid CPAS schedule timezone: {name}") from exc


def schedule_start_hour() -> int:
    return _clamp_hour(_env_int("SAS_CPAS_START_HOUR", DEFAULT_START_HOUR))


def schedule_end_hour() -> int:
    return _clamp_hour(_env_int("SAS_CPAS_END_HOUR", DEFAULT_END_HOUR))


def schedule_tick_seconds() -> float:
    raw = os.environ.get("SAS_CPAS_SCHEDULE_TICK_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TICK_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TICK_SECONDS
    return max(0.1, value)


def _append_audit(db: Session, event: str, detail: dict[str, Any]) -> None:
    db.add(
        AdminInjectedData(
            kind=KIND_AUDIT,
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


def _load_meta(db: Session) -> dict[str, Any]:
    row = db.query(AdminInjectedData).filter_by(kind=KIND_META).first()
    if not row:
        return {}
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_meta(db: Session, meta: dict[str, Any], *, commit: bool = True) -> None:
    existing = db.query(AdminInjectedData).filter_by(kind=KIND_META).first()
    raw = json.dumps(meta, default=str)
    if existing:
        existing.data_json = raw
    else:
        db.add(AdminInjectedData(kind=KIND_META, data_json=raw))
    if commit:
        db.commit()


def is_schedule_enabled(db: Session) -> bool:
    if not admin_flag_set(db, FLAG_SCHEDULE_ENABLED):
        return False
    row = db.query(AdminInjectedData).filter_by(kind=FLAG_SCHEDULE_ENABLED).first()
    if not row:
        return False
    try:
        payload = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return False
    if isinstance(payload, dict) and "enabled" in payload:
        return bool(payload.get("enabled"))
    # Legacy empty / non-object payloads are not treated as enabled.
    return False


def local_now(
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> datetime:
    instant = now or utc_now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    zone = resolve_schedule_zone(tz_name)
    return instant.astimezone(zone)


def is_within_cpas_window(
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
) -> bool:
    """True when local time is in [start_hour, end_hour) in the configured TZ."""
    local = local_now(now=now, tz_name=tz_name)
    start = _clamp_hour(
        schedule_start_hour() if start_hour is None else start_hour
    )
    end = _clamp_hour(schedule_end_hour() if end_hour is None else end_hour)
    hour = local.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # Window crossing midnight (not used by default CPAS policy).
    return hour >= start or hour < end


def enable_scheduled_daily_activities(db: Session) -> dict[str, Any]:
    """Arm the agreed CPAS schedule and ensure the ticker loop is running."""
    # Fail closed on bad TZ before persisting enabled=true.
    resolve_schedule_zone()
    payload = {
        "enabled": True,
        "enabledAt": utc_now().replace(microsecond=0).isoformat(),
        "timezone": schedule_timezone_name(),
        "startHour": schedule_start_hour(),
        "endHour": schedule_end_hour(),
    }
    # Single commit for flag + audit (avoid set_admin_flag's mid-flight commit).
    existing = db.query(AdminInjectedData).filter_by(kind=FLAG_SCHEDULE_ENABLED).first()
    raw = json.dumps(payload, default=str)
    if existing:
        existing.data_json = raw
    else:
        db.add(AdminInjectedData(kind=FLAG_SCHEDULE_ENABLED, data_json=raw))
    _append_audit(db, "schedule_enabled", dict(payload))
    db.commit()
    ensure_scheduler_loop_started()
    return payload


def mark_scheduled_success_if_applicable(
    db: Session, *, now: datetime | None = None
) -> None:
    """Record last successful local date when schedule is enabled and in window.

 Any successful CPAS completion (immediate or scheduled) inside the window
 counts for the local calendar day — same pipeline, one run per day.
 """
    instant = now or utc_now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    if not is_schedule_enabled(db):
        return
    if not is_within_cpas_window(now=instant):
        return
    local = local_now(now=instant)
    meta = _load_meta(db)
    meta["lastSuccessfulLocalDate"] = local.date().isoformat()
    meta["lastSuccessfulAt"] = instant.astimezone(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    _save_meta(db, meta, commit=True)
    _append_audit(
        db,
        "cpas_end_ok",
        {"localDate": meta["lastSuccessfulLocalDate"]},
    )
    db.commit()


def tick_scheduled_cpas(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the schedule once; may dispatch CPAS via the shared entrypoint."""
    instant = now or utc_now()
    with _tick_dispatch_lock:
        if not is_schedule_enabled(db):
            return {"action": "skipped", "reason": "disabled"}
        try:
            in_window = is_within_cpas_window(now=instant)
        except ValueError:
            logger.exception("CPAS schedule timezone invalid; skipping tick")
            return {"action": "skipped", "reason": "invalid_timezone"}
        if not in_window:
            return {"action": "skipped", "reason": "outside_window"}

        local = local_now(now=instant)
        today = local.date().isoformat()
        meta = _load_meta(db)
        if meta.get("lastSuccessfulLocalDate") == today:
            return {"action": "skipped", "reason": "already_succeeded_today"}
        if is_cpas_running(db):
            return {"action": "skipped", "reason": "cpas_running"}

        _append_audit(
            db,
            "cpas_start",
            {
                "localDate": today,
                "timezone": schedule_timezone_name(),
                "localHour": local.hour,
            },
        )
        meta["lastAttemptLocalDate"] = today
        meta["lastAttemptAt"] = utc_now().replace(microsecond=0).isoformat()
        _save_meta(db, meta, commit=False)
        db.commit()

        try:
            trigger_daily_activities(db)
        except Exception:
            logger.exception("Scheduled CPAS dispatch failed")
            _append_audit(db, "cpas_dispatch_error", {"localDate": today})
            db.commit()
            return {"action": "error", "reason": "dispatch_failed"}

        return {"action": "dispatched", "localDate": today}


def ensure_scheduler_loop_started() -> None:
    """Start a daemon ticker if not already running (idempotent)."""
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            name="cpas-schedule-ticker",
            daemon=True,
        )
        _scheduler_thread.start()


def stop_scheduler_loop_for_tests() -> None:
    """Stop the ticker (unit tests only)."""
    global _scheduler_thread
    _scheduler_stop.set()
    thread = _scheduler_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    with _scheduler_lock:
        _scheduler_thread = None


def _scheduler_loop() -> None:
    from database import SessionLocal

    while not _scheduler_stop.is_set():
        session = SessionLocal()
        try:
            if is_schedule_enabled(session):
                tick_scheduled_cpas(session)
        except Exception:
            logger.exception("CPAS schedule tick failed")
            try:
                session.rollback()
            except Exception:
                logger.exception("Rollback after schedule tick failure failed")
        finally:
            session.close()
        _scheduler_stop.wait(schedule_tick_seconds())


def reset_schedule_state(db: Session) -> None:
    """Clear schedule flags/meta/audit (also covered by full reset_db)."""
    for kind in (FLAG_SCHEDULE_ENABLED, KIND_META, KIND_AUDIT):
        db.query(AdminInjectedData).filter_by(kind=kind).delete()
    db.commit()
