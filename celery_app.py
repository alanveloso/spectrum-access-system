"""Celery application wired to RabbitMQ (and optional result backend)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from celery import Celery
from celery.signals import worker_process_init, worker_ready

from config import get_settings

_settings = get_settings()

celery_app = Celery(
    "sas",
    broker=_settings.broker_url,
    include=["tasks"],
)

_conf: dict = {
    "task_serializer": "json",
    "accept_content": ["json"],
    "result_serializer": "json",
    "timezone": "UTC",
    "enable_utc": True,
    "task_acks_late": _settings.celery_task_acks_late,
    "worker_prefetch_multiplier": _settings.celery_worker_prefetch_multiplier,
    "task_default_queue": _settings.celery_task_default_queue,
    "task_track_started": True,
    "broker_connection_retry_on_startup": True,
}

if _settings.result_backend:
    _conf["result_backend"] = _settings.result_backend
else:
    # Status is persisted in the application DB (cpas_running flag).
    _conf["task_ignore_result"] = True

celery_app.conf.update(**_conf)


def _ensure_worker_runtime_composition() -> None:
    """Resolve RuntimeComposition once for the current worker OS process."""
    from protection_data.loader import set_data_root
    from runtime.bootstrap import (
        get_process_runtime_composition,
        initialize_process_runtime_composition,
    )

    if get_process_runtime_composition() is not None:
        return
    settings = get_settings()
    set_data_root(settings.resolved_protection_data_root)
    initialize_process_runtime_composition()


@worker_process_init.connect
def _initialize_runtime_composition_on_worker_process(**kwargs) -> None:
    """Prefork pool: each child process initializes its own composition."""
    del kwargs
    _ensure_worker_runtime_composition()


@worker_ready.connect
def _initialize_runtime_composition_on_worker_ready(**kwargs) -> None:
    """Solo/threads pools: ensure composition exists before accepting tasks."""
    del kwargs
    _ensure_worker_runtime_composition()
