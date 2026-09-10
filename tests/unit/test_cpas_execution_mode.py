"""CPAS execution-mode tests ()."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from config import Settings, clear_settings_cache


@pytest.fixture(autouse=True)
def _reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _join_certification_workers(timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    for thread in threading.enumerate():
        if thread.name == "cpas-certification" and thread.is_alive():
            remaining = max(0.0, deadline - time.time())
            thread.join(timeout=remaining)


def test_settings_accepts_certification_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "CERTIFICATION")
    clear_settings_cache()
    from config import get_settings

    assert get_settings().sas_execution_mode == "certification"


def test_settings_rejects_unknown_execution_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "bogus")
    clear_settings_cache()
    with pytest.raises(ValidationError):
        Settings()


def test_certification_mode_runs_pipeline_without_celery(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()

    import services.cpas_service as cpas

    calls: list[str] = []
    running = {"value": False}
    started = threading.Event()
    release = threading.Event()

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
        assert running["value"] is True
        started.set()
        assert release.wait(timeout=2.0)

    delay = MagicMock()
    monkeypatch.setattr(cpas, "set_admin_flag", _set)
    monkeypatch.setattr(cpas, "clear_admin_flags", _clear)
    monkeypatch.setattr(cpas, "execute_cpas_pipeline", _exec)
    monkeypatch.setattr("tasks.run_cpas.delay", delay, raising=False)

    fake_session = MagicMock()
    monkeypatch.setattr(
        "database.SessionLocal", lambda: fake_session, raising=False
    )

    cpas.trigger_daily_activities(MagicMock())
    assert started.wait(timeout=2.0)
    assert running["value"] is True  # async contract: still running after trigger returns
    assert get_daily_activities_completed_safe(cpas) is False
    release.set()
    _join_certification_workers()

    assert calls == ["set", "exec", "clear"]
    delay.assert_not_called()
    assert running["value"] is False
    fake_session.close.assert_called()


def get_daily_activities_completed_safe(cpas_module) -> bool:
    return cpas_module.get_daily_activities_completed(MagicMock())


def test_production_mode_enqueues_celery(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "production")
    clear_settings_cache()

    import services.cpas_service as cpas

    calls: list[str] = []
    running = {"value": False}
    delay = MagicMock()

    monkeypatch.setattr(cpas, "is_cpas_running", lambda db: running["value"])

    def _set(db, flag, payload=None):
        del db, flag, payload
        running["value"] = True
        calls.append("set")

    monkeypatch.setattr(cpas, "set_admin_flag", _set)
    monkeypatch.setattr(
        cpas, "clear_admin_flags", lambda db, flag: calls.append("clear")
    )
    monkeypatch.setattr(
        cpas, "execute_cpas_pipeline", lambda db: calls.append("exec")
    )

    fake_task = MagicMock()
    fake_task.delay = delay
    monkeypatch.setattr("tasks.run_cpas", fake_task)

    cpas.trigger_daily_activities(MagicMock())

    assert calls == ["set"]
    delay.assert_called_once_with()
    assert "exec" not in calls
    assert running["value"] is True


def test_production_enqueue_failure_clears_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "production")
    clear_settings_cache()

    import services.cpas_service as cpas

    running = {"value": False}
    monkeypatch.setattr(cpas, "is_cpas_running", lambda db: running["value"])

    def _set(db, flag, payload=None):
        del db, flag, payload
        running["value"] = True

    def _clear(db, flag):
        del db, flag
        running["value"] = False

    monkeypatch.setattr(cpas, "set_admin_flag", _set)
    monkeypatch.setattr(cpas, "clear_admin_flags", _clear)

    fake_task = MagicMock()
    fake_task.delay.side_effect = ConnectionError("broker down")
    monkeypatch.setattr("tasks.run_cpas", fake_task)

    with pytest.raises(ConnectionError):
        cpas.trigger_daily_activities(MagicMock())

    assert running["value"] is False


def test_certification_pipeline_failure_clears_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()

    import services.cpas_service as cpas

    running = {"value": False}
    monkeypatch.setattr(cpas, "is_cpas_running", lambda db: running["value"])

    def _set(db, flag, payload=None):
        del db, flag, payload
        running["value"] = True

    def _clear(db, flag):
        del db, flag
        running["value"] = False

    monkeypatch.setattr(cpas, "set_admin_flag", _set)
    monkeypatch.setattr(cpas, "clear_admin_flags", _clear)
    monkeypatch.setattr(
        cpas,
        "execute_cpas_pipeline",
        lambda db: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        "database.SessionLocal", lambda: MagicMock(), raising=False
    )

    cpas.trigger_daily_activities(MagicMock())
    _join_certification_workers()

    assert running["value"] is False


def test_duplicate_trigger_while_running_is_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_EXECUTION_MODE", "certification")
    clear_settings_cache()

    import services.cpas_service as cpas

    calls: list[str] = []
    running = {"value": False}
    entered = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(cpas, "is_cpas_running", lambda db: running["value"])

    def _set(db, flag, payload=None):
        del db, flag, payload
        running["value"] = True
        calls.append("set")

    def _clear(db, flag):
        del db, flag
        calls.append("clear")
        running["value"] = False

    def _exec(db):
        del db
        calls.append("exec")
        entered.set()
        assert release.wait(timeout=2.0)
        # Nested call while flag is set must not start a second pipeline.
        cpas.trigger_daily_activities(MagicMock())

    monkeypatch.setattr(cpas, "set_admin_flag", _set)
    monkeypatch.setattr(cpas, "clear_admin_flags", _clear)
    monkeypatch.setattr(cpas, "execute_cpas_pipeline", _exec)
    monkeypatch.setattr(
        "database.SessionLocal", lambda: MagicMock(), raising=False
    )

    cpas.trigger_daily_activities(MagicMock())
    assert entered.wait(timeout=2.0)
    # Second HTTP-style trigger while first CPAS is in-flight.
    cpas.trigger_daily_activities(MagicMock())
    release.set()
    _join_certification_workers()

    assert calls.count("exec") == 1
    assert calls.count("set") == 1
