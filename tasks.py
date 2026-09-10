"""Celery tasks for long-running SAS workloads (CPAS / daily activities)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from celery_app import celery_app
from database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.run_cpas", bind=True)
def run_cpas(self) -> dict[str, str]:
    """Execute the full CPAS pipeline in a worker process.

    Opens its own SQLAlchemy session so the FastAPI event loop stays free.
    Always clears the ``cpas_running`` flag in ``finally``.
    """
    from services.cpas_service import execute_cpas_pipeline

    db = SessionLocal()
    try:
        execute_cpas_pipeline(db)
        return {"status": "succeeded", "task_id": self.request.id or ""}
    except Exception:
        logger.exception("CPAS Celery task failed (task_id=%s)", self.request.id)
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback CPAS session after error")
        raise
    finally:
        from services.cpas_service import FLAG_CPAS_RUNNING
        from services.meas_report import clear_admin_flags

        try:
            clear_admin_flags(db, FLAG_CPAS_RUNNING)
        except Exception:
            logger.exception("Failed to clear CPAS running flag")
            try:
                db.rollback()
            except Exception:
                pass
        db.close()
