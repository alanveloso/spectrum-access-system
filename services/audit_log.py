"""Unified AdminInjectedData audit helper.

Domain-specific kinds (dpa_audit, cpas_pipeline_audit, …) remain valid; this
helper standardizes shape and attaches correlation IDs when present.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from services.request_context import context_as_dict

KIND_ADMIN_AUDIT = "admin_audit"
KIND_RF_DECISION_AUDIT = "rf_decision_audit"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def redact_audit_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Drop or mask fields that must not land in durable audit rows."""
    from services.logging_redaction import redact_mapping

    return redact_mapping(detail)


def append_audit(
    db: Session,
    *,
    kind: str,
    event: str,
    detail: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """Persist one audit event. Never stores raw secrets/PEM/passwords."""
    payload: dict[str, Any] = {
        "event": event,
        "at": _utc_now_iso(),
        **context_as_dict(),
        **redact_audit_detail(dict(detail or {})),
    }
    db.add(AdminInjectedData(kind=kind, data_json=json.dumps(payload, default=str)))
    if commit:
        db.commit()


def append_admin_audit(
    db: Session,
    event: str,
    detail: dict[str, Any] | None = None,
    *,
    commit: bool = False,
) -> None:
    append_audit(
        db, kind=KIND_ADMIN_AUDIT, event=event, detail=detail, commit=commit
    )


def append_rf_decision_audit(
    db: Session,
    *,
    procedure: str,
    response_code: int,
    detail: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """Record non-success protocol outcomes (rate-limited by caller)."""
    body = {"procedure": procedure, "responseCode": int(response_code)}
    if detail:
        body.update(detail)
    append_audit(
        db,
        kind=KIND_RF_DECISION_AUDIT,
        event="protocol_item_non_success",
        detail=body,
        commit=commit,
    )
