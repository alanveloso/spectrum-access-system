"""Central CBSD / Grant lifecycle state machine (WInnForum-oriented).

Defines allowed transitions, response codes, required fields, persistence
effects, idempotency and isolation hints. Services call helpers here instead
of ad-hoc boolean flags alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from models.models import Cbsd, Grant
from primitives.refresh import open_until
from primitives.time import UtcInstant

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103
TERMINATED_GRANT = 500
SUSPENDED_GRANT = 501
UNSYNC_OP_PARAM = 502


class CbsdState(str, Enum):
    UNREGISTERED = "UNREGISTERED"
    REGISTERED = "REGISTERED"
    DEREGISTERED = "DEREGISTERED"


class GrantState(str, Enum):
    REQUESTED = "REQUESTED"
    GRANTED = "GRANTED"
    AUTHORIZED = "AUTHORIZED"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"
    RELINQUISHED = "RELINQUISHED"


class CbsdEvent(str, Enum):
    REGISTER = "register"
    REREGISTER = "reregister"
    DEREGISTER = "deregister"


class GrantEvent(str, Enum):
    APPROVE = "approve"
    AUTHORIZE = "authorize"
    SUSPEND = "suspend"
    TERMINATE = "terminate"
    EXPIRE = "expire"
    RELINQUISH = "relinquish"


TERMINAL_GRANT_STATES: frozenset[GrantState] = frozenset(
    {
        GrantState.TERMINATED,
        GrantState.EXPIRED,
        GrantState.RELINQUISHED,
    }
)


@dataclass(frozen=True)
class TransitionSpec:
    """Declarative rule for one lifecycle event."""

    event: str
    sources: frozenset[str]
    target: str
    success_code: int
    required_fields: frozenset[str]
    persistent_effect: str
    idempotent: bool
    isolation: str  # "row_lock" | "none"
    failure_code: int = INVALID_PARAM


@dataclass(frozen=True)
class TransitionOutcome:
    ok: bool
    response_code: int
    from_state: str
    to_state: str | None
    idempotent_hit: bool = False
    detail: str | None = None


# --- CBSD transition table -------------------------------------------------

CBSD_TRANSITIONS: dict[CbsdEvent, TransitionSpec] = {
    CbsdEvent.REGISTER: TransitionSpec(
        event=CbsdEvent.REGISTER.value,
        sources=frozenset({CbsdState.UNREGISTERED.value, CbsdState.DEREGISTERED.value}),
        target=CbsdState.REGISTERED.value,
        success_code=SUCCESS,
        required_fields=frozenset({"fccId", "cbsdSerialNumber", "userId"}),
        persistent_effect="insert_or_restore_cbsd",
        idempotent=False,
        isolation="row_lock",
    ),
    CbsdEvent.REREGISTER: TransitionSpec(
        event=CbsdEvent.REREGISTER.value,
        sources=frozenset({CbsdState.REGISTERED.value}),
        target=CbsdState.REGISTERED.value,
        success_code=SUCCESS,
        required_fields=frozenset({"fccId", "cbsdSerialNumber", "userId"}),
        persistent_effect="update_cbsd_registration",
        idempotent=True,
        isolation="row_lock",
    ),
    CbsdEvent.DEREGISTER: TransitionSpec(
        event=CbsdEvent.DEREGISTER.value,
        sources=frozenset({CbsdState.REGISTERED.value}),
        target=CbsdState.DEREGISTERED.value,
        success_code=SUCCESS,
        required_fields=frozenset({"cbsdId"}),
        persistent_effect="delete_cbsd_and_grants",
        idempotent=False,
        isolation="row_lock",
    ),
}

# --- Grant transition table ------------------------------------------------

GRANT_TRANSITIONS: dict[GrantEvent, TransitionSpec] = {
    GrantEvent.APPROVE: TransitionSpec(
        event=GrantEvent.APPROVE.value,
        sources=frozenset({GrantState.REQUESTED.value}),
        target=GrantState.GRANTED.value,
        success_code=SUCCESS,
        required_fields=frozenset({"cbsdId", "operationParam"}),
        persistent_effect="insert_grant_row",
        idempotent=False,
        isolation="row_lock",
    ),
    GrantEvent.AUTHORIZE: TransitionSpec(
        event=GrantEvent.AUTHORIZE.value,
        sources=frozenset(
            {
                GrantState.GRANTED.value,
                GrantState.AUTHORIZED.value,
                GrantState.SUSPENDED.value,
            }
        ),
        target=GrantState.AUTHORIZED.value,
        success_code=SUCCESS,
        required_fields=frozenset({"cbsdId", "grantId", "operationState"}),
        persistent_effect="set_authorized_true",
        idempotent=True,
        isolation="row_lock",
    ),
    GrantEvent.SUSPEND: TransitionSpec(
        event=GrantEvent.SUSPEND.value,
        sources=frozenset(
            {
                GrantState.GRANTED.value,
                GrantState.AUTHORIZED.value,
                GrantState.SUSPENDED.value,
            }
        ),
        target=GrantState.SUSPENDED.value,
        success_code=SUSPENDED_GRANT,
        required_fields=frozenset({"cbsdId", "grantId"}),
        persistent_effect="set_suspended",
        idempotent=True,
        isolation="row_lock",
    ),
    GrantEvent.TERMINATE: TransitionSpec(
        event=GrantEvent.TERMINATE.value,
        sources=frozenset(
            {
                GrantState.GRANTED.value,
                GrantState.AUTHORIZED.value,
                GrantState.SUSPENDED.value,
            }
        ),
        target=GrantState.TERMINATED.value,
        success_code=TERMINATED_GRANT,
        required_fields=frozenset({"cbsdId", "grantId"}),
        persistent_effect="set_terminated",
        idempotent=False,
        isolation="row_lock",
    ),
    GrantEvent.EXPIRE: TransitionSpec(
        event=GrantEvent.EXPIRE.value,
        sources=frozenset(
            {
                GrantState.GRANTED.value,
                GrantState.AUTHORIZED.value,
                GrantState.SUSPENDED.value,
            }
        ),
        target=GrantState.EXPIRED.value,
        success_code=INVALID_PARAM,
        required_fields=frozenset({"cbsdId", "grantId"}),
        persistent_effect="set_expired",
        idempotent=True,
        isolation="none",
    ),
    GrantEvent.RELINQUISH: TransitionSpec(
        event=GrantEvent.RELINQUISH.value,
        sources=frozenset(
            {
                GrantState.GRANTED.value,
                GrantState.AUTHORIZED.value,
                GrantState.SUSPENDED.value,
            }
        ),
        target=GrantState.RELINQUISHED.value,
        success_code=SUCCESS,
        required_fields=frozenset({"cbsdId", "grantId"}),
        persistent_effect="set_relinquished",
        idempotent=False,
        isolation="row_lock",
    ),
}


def missing_required_fields(
    payload: dict[str, Any], required: frozenset[str]
) -> list[str]:
    return [name for name in sorted(required) if not payload.get(name)]


def resolve_cbsd_state(cbsd: Cbsd | None) -> CbsdState:
    """Map ORM row (or absence) to a CBSD lifecycle state."""
    if cbsd is None:
        return CbsdState.UNREGISTERED
    raw = getattr(cbsd, "lifecycle_state", None)
    if raw:
        try:
            return CbsdState(raw)
        except ValueError:
            pass
    return CbsdState.REGISTERED


def _grant_expired(grant: Grant, *, now: datetime | None = None) -> bool:
    from services.clock import ensure_utc, utc_now

    wall = ensure_utc(now or utc_now()).replace(microsecond=0)
    expire = grant.grant_expire_time
    if expire is None:
        return False
    end = ensure_utc(expire).replace(microsecond=0)
    return not open_until(UtcInstant(end), UtcInstant(wall))


def resolve_grant_state(
    grant: Grant, *, now: datetime | None = None
) -> GrantState:
    """Derive Grant lifecycle state from explicit column and legacy flags."""
    raw = getattr(grant, "lifecycle_state", None)
    if raw:
        try:
            state = GrantState(raw)
        except ValueError:
            pass
        else:
            if (
                state
                in (
                    GrantState.GRANTED,
                    GrantState.AUTHORIZED,
                    GrantState.SUSPENDED,
                )
                and _grant_expired(grant, now=now)
            ):
                return GrantState.EXPIRED
            return state

    if grant.terminated:
        return GrantState.TERMINATED
    if _grant_expired(grant, now=now):
        return GrantState.EXPIRED
    if grant.authorized:
        return GrantState.AUTHORIZED
    return GrantState.GRANTED


def evaluate_cbsd_transition(
    event: CbsdEvent,
    *,
    current: CbsdState,
    payload: dict[str, Any] | None = None,
) -> TransitionOutcome:
    """Validate a CBSD transition without mutating persistence."""
    spec = CBSD_TRANSITIONS[event]
    payload = payload or {}
    missing = missing_required_fields(payload, spec.required_fields)
    if missing:
        return TransitionOutcome(
            ok=False,
            response_code=MISSING_PARAM,
            from_state=current.value,
            to_state=None,
            detail=f"missing:{','.join(missing)}",
        )
    if current.value not in spec.sources:
        return TransitionOutcome(
            ok=False,
            response_code=spec.failure_code,
            from_state=current.value,
            to_state=None,
            detail="illegal_source",
        )
    idempotent_hit = bool(spec.idempotent and current.value == spec.target)
    return TransitionOutcome(
        ok=True,
        response_code=spec.success_code,
        from_state=current.value,
        to_state=spec.target,
        idempotent_hit=idempotent_hit,
    )


def evaluate_grant_transition(
    event: GrantEvent,
    *,
    current: GrantState,
    payload: dict[str, Any] | None = None,
) -> TransitionOutcome:
    """Validate a Grant transition without mutating persistence."""
    spec = GRANT_TRANSITIONS[event]
    payload = payload or {}
    missing = missing_required_fields(payload, spec.required_fields)
    if missing:
        return TransitionOutcome(
            ok=False,
            response_code=MISSING_PARAM,
            from_state=current.value,
            to_state=None,
            detail=f"missing:{','.join(missing)}",
        )
    if current in TERMINAL_GRANT_STATES and event != GrantEvent.APPROVE:
        # Already terminal: relinquish/terminate/expire of a dead grant → 103.
        if spec.idempotent and current.value == spec.target:
            return TransitionOutcome(
                ok=True,
                response_code=spec.success_code,
                from_state=current.value,
                to_state=spec.target,
                idempotent_hit=True,
            )
        return TransitionOutcome(
            ok=False,
            response_code=INVALID_PARAM,
            from_state=current.value,
            to_state=None,
            detail="terminal_state",
        )
    if current.value not in spec.sources:
        return TransitionOutcome(
            ok=False,
            response_code=spec.failure_code,
            from_state=current.value,
            to_state=None,
            detail="illegal_source",
        )
    idempotent_hit = bool(spec.idempotent and current.value == spec.target)
    return TransitionOutcome(
        ok=True,
        response_code=spec.success_code,
        from_state=current.value,
        to_state=spec.target,
        idempotent_hit=idempotent_hit,
    )


def apply_cbsd_state(cbsd: Cbsd, state: CbsdState) -> None:
    cbsd.lifecycle_state = state.value


def apply_grant_state(grant: Grant, state: GrantState) -> None:
    """Persist lifecycle_state and keep legacy boolean flags in sync."""
    grant.lifecycle_state = state.value
    if state is GrantState.AUTHORIZED:
        grant.authorized = True
        grant.terminated = False
    elif state is GrantState.GRANTED:
        grant.authorized = False
        grant.terminated = False
    elif state is GrantState.SUSPENDED:
        grant.terminated = False
    elif state in (
        GrantState.TERMINATED,
        GrantState.EXPIRED,
        GrantState.RELINQUISHED,
    ):
        grant.terminated = True


def apply_grant_event(
    grant: Grant,
    event: GrantEvent,
    *,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> TransitionOutcome:
    """Validate and, on success, persist the Grant transition."""
    current = resolve_grant_state(grant, now=now)
    outcome = evaluate_grant_transition(event, current=current, payload=payload)
    if not outcome.ok or outcome.to_state is None:
        return outcome
    apply_grant_state(grant, GrantState(outcome.to_state))
    return outcome


def lock_grant_row(db: Session, grant_id: str, cbsd_id: str) -> Grant | None:
    """Load a grant with a row lock when the dialect supports it.

 Delegates to ``services.concurrency.lock_grant_row`` (FOR UPDATE only).
 Callers must also hold the process-level exclusive lock / advisory lock.
 """
    from services.concurrency import lock_grant_row as _lock

    return _lock(db, grant_id, cbsd_id)


def heartbeat_operation_allowed(
    grant: Grant,
    *,
    operation_state: str,
    now: datetime | None = None,
) -> TransitionOutcome:
    """Gate heartbeat against lifecycle + client operationState sync rules."""
    current = resolve_grant_state(grant, now=now)
    # Known TERMINATED grant (CPAS/lifecycle): heartbeat reports 500, not 103.
    # RELINQUISHED remains invalid-value (103). Do not collapse D/E into "invalid".
    if current is GrantState.TERMINATED:
        return TransitionOutcome(
            ok=False,
            response_code=TERMINATED_GRANT,
            from_state=current.value,
            to_state=GrantState.TERMINATED.value,
            detail="already_terminated",
        )
    if current is GrantState.RELINQUISHED:
        return TransitionOutcome(
            ok=False,
            response_code=INVALID_PARAM,
            from_state=current.value,
            to_state=None,
            detail="terminal_state",
        )
    if operation_state == "AUTHORIZED" and current is GrantState.GRANTED:
        return TransitionOutcome(
            ok=False,
            response_code=UNSYNC_OP_PARAM,
            from_state=current.value,
            to_state=None,
            detail="authorized_before_granted_heartbeat",
        )
    if current is GrantState.SUSPENDED:
        return TransitionOutcome(
            ok=False,
            response_code=SUSPENDED_GRANT,
            from_state=current.value,
            to_state=GrantState.SUSPENDED.value,
            detail="suspended",
        )
    return TransitionOutcome(
        ok=True,
        response_code=SUCCESS,
        from_state=current.value,
        to_state=current.value,
    )
