"""scheduled daily CPAS enable, window tick, anti-dup, audit."""

from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services import clock
from services.cpas_schedule_service import (
    FLAG_SCHEDULE_ENABLED,
    KIND_AUDIT,
    KIND_META,
    enable_scheduled_daily_activities,
    is_schedule_enabled,
    is_within_cpas_window,
    mark_scheduled_success_if_applicable,
    schedule_end_hour,
    schedule_start_hour,
    stop_scheduler_loop_for_tests,
    tick_scheduled_cpas,
)
from services.meas_report import set_admin_flag

client = TestClient(app)

PACIFIC = ZoneInfo("US/Pacific")


def _at_pacific(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


def setup_function() -> None:
    clock.reset_clock_provider()
    stop_scheduler_loop_for_tests()


def teardown_function() -> None:
    clock.reset_clock_provider()
    stop_scheduler_loop_for_tests()


def test_window_boundaries_pacific(monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    monkeypatch.setenv("SAS_CPAS_START_HOUR", "2")
    monkeypatch.setenv("SAS_CPAS_END_HOUR", "4")
    assert is_within_cpas_window(now=_at_pacific(2026, 8, 7, 1, 59)) is False
    assert is_within_cpas_window(now=_at_pacific(2026, 8, 7, 2, 0)) is True
    assert is_within_cpas_window(now=_at_pacific(2026, 8, 7, 3, 30)) is True
    assert is_within_cpas_window(now=_at_pacific(2026, 8, 7, 4, 0)) is False


def test_hour_env_clamped_to_0_23(monkeypatch):
    monkeypatch.setenv("SAS_CPAS_START_HOUR", "25")
    monkeypatch.setenv("SAS_CPAS_END_HOUR", "-3")
    assert schedule_start_hour() == 23
    assert schedule_end_hour() == 0


def test_enable_rejects_invalid_timezone(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "Not/A_Real_Zone")
    with pytest.raises(ValueError, match="invalid CPAS schedule timezone"):
        enable_scheduled_daily_activities(db_session)
    assert (
        db_session.query(AdminInjectedData)
        .filter_by(kind=FLAG_SCHEDULE_ENABLED)
        .first()
        is None
    )


def test_corrupt_flag_json_is_not_enabled(db_session):
    db_session.add(
        AdminInjectedData(kind=FLAG_SCHEDULE_ENABLED, data_json="{not-json")
    )
    db_session.commit()
    assert is_schedule_enabled(db_session) is False


def test_legacy_flag_without_enabled_key_is_not_enabled(db_session):
    set_admin_flag(db_session, FLAG_SCHEDULE_ENABLED, {"armed": True})
    assert is_schedule_enabled(db_session) is False


def test_enable_persists_config_and_audit(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    stop_scheduler_loop_for_tests()
    payload = enable_scheduled_daily_activities(db_session)
    assert payload["enabled"] is True
    assert payload["timezone"] == "US/Pacific"
    assert payload["startHour"] == 2
    assert payload["endHour"] == 4
    row = db_session.query(AdminInjectedData).filter_by(kind=FLAG_SCHEDULE_ENABLED).one()
    assert '"enabled": true' in row.data_json.replace("True", "true") or "enabled" in row.data_json
    audits = db_session.query(AdminInjectedData).filter_by(kind=KIND_AUDIT).all()
    assert any("schedule_enabled" in (a.data_json or "") for a in audits)
    stop_scheduler_loop_for_tests()


def test_tick_outside_window_does_not_dispatch(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    enable_scheduled_daily_activities(db_session)
    stop_scheduler_loop_for_tests()
    calls: list[str] = []

    def _trigger(db):
        calls.append("trigger")

    monkeypatch.setattr(
        "services.cpas_schedule_service.trigger_daily_activities", _trigger
    )
    result = tick_scheduled_cpas(db_session, now=_at_pacific(2026, 8, 7, 1, 59))
    assert result["action"] == "skipped"
    assert result["reason"] == "outside_window"
    assert calls == []


def test_tick_disabled_does_not_dispatch(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    calls: list[str] = []
    monkeypatch.setattr(
        "services.cpas_schedule_service.trigger_daily_activities",
        lambda db: calls.append("trigger"),
    )
    result = tick_scheduled_cpas(db_session, now=_at_pacific(2026, 8, 7, 2, 5))
    assert result == {"action": "skipped", "reason": "disabled"}
    assert calls == []


def test_tick_in_window_dispatches_once_per_day(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    enable_scheduled_daily_activities(db_session)
    stop_scheduler_loop_for_tests()
    calls: list[str] = []

    def _trigger(db):
        calls.append("trigger")

    monkeypatch.setattr(
        "services.cpas_schedule_service.trigger_daily_activities", _trigger
    )
    now = _at_pacific(2026, 8, 7, 2, 5)
    first = tick_scheduled_cpas(db_session, now=now)
    assert first == {"action": "dispatched", "localDate": "2026-08-07"}
    assert calls == ["trigger"]
    # Simulate successful pipeline completion in-window.
    mark_scheduled_success_if_applicable(db_session, now=now)
    second = tick_scheduled_cpas(db_session, now=_at_pacific(2026, 8, 7, 3, 0))
    assert second["action"] == "skipped"
    assert second["reason"] == "already_succeeded_today"
    assert calls == ["trigger"]
    meta = db_session.query(AdminInjectedData).filter_by(kind=KIND_META).one()
    assert "2026-08-07" in (meta.data_json or "")


def test_tick_skips_while_cpas_running(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    enable_scheduled_daily_activities(db_session)
    stop_scheduler_loop_for_tests()
    set_admin_flag(db_session, "cpas_running", {"running": True})
    calls: list[str] = []
    monkeypatch.setattr(
        "services.cpas_schedule_service.trigger_daily_activities",
        lambda db: calls.append("trigger"),
    )
    result = tick_scheduled_cpas(db_session, now=_at_pacific(2026, 8, 7, 2, 10))
    assert result == {"action": "skipped", "reason": "cpas_running"}
    assert calls == []


def test_tick_retries_after_failed_dispatch_same_day(db_session, monkeypatch):
    """No lastSuccessfulLocalDate → second tick in window may dispatch again."""
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    enable_scheduled_daily_activities(db_session)
    stop_scheduler_loop_for_tests()
    calls: list[str] = []

    def _trigger(db):
        calls.append("trigger")
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        "services.cpas_schedule_service.trigger_daily_activities", _trigger
    )
    first = tick_scheduled_cpas(db_session, now=_at_pacific(2026, 8, 7, 2, 1))
    assert first["action"] == "error"
    # Fix trigger and retry — recovery within window.
    monkeypatch.setattr(
        "services.cpas_schedule_service.trigger_daily_activities",
        lambda db: calls.append("retry"),
    )
    second = tick_scheduled_cpas(db_session, now=_at_pacific(2026, 8, 7, 2, 30))
    assert second["action"] == "dispatched"
    assert "retry" in calls


def test_admin_http_enable_scheduled(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "US/Pacific")
    stop_scheduler_loop_for_tests()
    resp = client.post("/admin/trigger/enable_scheduled_daily_activities")
    assert resp.status_code == 200
    assert resp.content == b""
    row = db_session.query(AdminInjectedData).filter_by(kind=FLAG_SCHEDULE_ENABLED).one()
    assert "enabled" in (row.data_json or "")
    stop_scheduler_loop_for_tests()


def test_admin_http_enable_invalid_timezone_fails_closed(db_session, monkeypatch):
    monkeypatch.setenv("SAS_CPAS_TIMEZONE", "Not/A_Real_Zone")
    stop_scheduler_loop_for_tests()
    resp = client.post("/admin/trigger/enable_scheduled_daily_activities")
    assert resp.status_code == 500
    assert (
        db_session.query(AdminInjectedData)
        .filter_by(kind=FLAG_SCHEDULE_ENABLED)
        .first()
        is None
    )
