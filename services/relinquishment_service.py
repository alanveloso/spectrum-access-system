"""Relinquishment business logic aligned with WINNF_FT_S_RLQ expectations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103


def _resp(
    code: int,
    *,
    cbsd_id: str | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"response": {"responseCode": code}}
    if cbsd_id is not None:
        out["cbsdId"] = cbsd_id
    if grant_id is not None:
        out["grantId"] = grant_id
    return out


def process_relinquishment(
    db: Session,
    requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
) -> list[dict[str, Any]]:
    from services.cbsd_auth import cbsd_certificate_mismatch
    from services.concurrency import (
        acquire_cbsd_xact_lock,
        acquire_grant_xact_lock,
        exclusive_cbsd_and_grant,
        lock_cbsd_row,
        lock_grant_row,
    )
    from services.lifecycle import GrantEvent, apply_grant_event

    responses: list[dict[str, Any]] = []

    for req in requests:
        cbsd_id = req.get("cbsdId")
        grant_id = req.get("grantId")

 # Missing cbsdId and/or grantId → 102.
 # Echo cbsdId only when provided (RLQ_5); never echo grantId on missing-param.
        if not cbsd_id or not grant_id:
            responses.append(
                _resp(MISSING_PARAM, cbsd_id=cbsd_id if cbsd_id else None)
            )
            continue

        with exclusive_cbsd_and_grant(cbsd_id, grant_id):
            try:
                acquire_cbsd_xact_lock(db, cbsd_id)
                acquire_grant_xact_lock(db, grant_id)
                cbsd = lock_cbsd_row(db, cbsd_id)
                if not cbsd:
 # Unknown CBSD → 103 without echoing identifiers (RLQ_3).
                    responses.append(_resp(INVALID_PARAM))
                    continue

 # Re-check under lock (REG may have rebound certificate_hash).
                if cbsd_certificate_mismatch(cbsd, certificate_hash):
                    responses.append(_resp(INVALID_PARAM))
                    continue

                grant = lock_grant_row(db, grant_id, cbsd_id)
                if not grant or grant.terminated:
 # Unknown / foreign / already relinquished grant → 103, echo cbsdId only.
                    responses.append(_resp(INVALID_PARAM, cbsd_id=cbsd_id))
                    continue

                outcome = apply_grant_event(
                    grant,
                    GrantEvent.RELINQUISH,
                    payload={"cbsdId": cbsd_id, "grantId": grant_id},
                )
                if not outcome.ok:
                    responses.append(_resp(outcome.response_code, cbsd_id=cbsd_id))
                    continue
                responses.append(_resp(SUCCESS, cbsd_id=cbsd_id, grant_id=grant_id))
            finally:
 # Persist before releasing the process lock so peer sessions see the row.
                db.commit()

    return responses
