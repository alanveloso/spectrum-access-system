"""CPAS reevaluation request flag after federal / WDB generation bumps.

Ingest commits generation N+1, then marks reevaluation required.
The next ``execute_cpas_pipeline`` freezes N+1; an in-flight frozen N is unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData

KIND_CPAS_REEVAL = "cpas_reevaluation_required"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mark_cpas_reevaluation_required(
    db: Session, *, reason: str, generation: dict[str, Any] | None = None
) -> None:
    payload = {
        "required": True,
        "reason": reason,
        "markedAt": _utc_now_iso(),
        "generation": generation or {},
    }
    row = db.query(AdminInjectedData).filter_by(kind=KIND_CPAS_REEVAL).first()
    raw = json.dumps(payload)
    if row:
        row.data_json = raw
    else:
        db.add(AdminInjectedData(kind=KIND_CPAS_REEVAL, data_json=raw))


def clear_cpas_reevaluation_required(db: Session) -> None:
    db.query(AdminInjectedData).filter_by(kind=KIND_CPAS_REEVAL).delete()


def cpas_reevaluation_required(db: Session) -> dict[str, Any] | None:
    row = db.query(AdminInjectedData).filter_by(kind=KIND_CPAS_REEVAL).first()
    if not row:
        return None
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("required"):
        return data
    return None
