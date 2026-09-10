"""Closed decision actions (D17). Protocol codes live in adapters, not here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from primitives.power import PowerDbm
from primitives.profile_context import ProfileContext


class DecisionAction(StrEnum):
    REJECT = "reject"
    KEEP = "keep"
    REDUCE_POWER = "reduce_power"
    SUSPEND = "suspend"
    TERMINATE = "terminate"


_APPLY_WRITES = frozenset(
    {
        DecisionAction.REDUCE_POWER,
        DecisionAction.SUSPEND,
        DecisionAction.TERMINATE,
    }
)


def is_apply_write(action: DecisionAction) -> bool:
    """Apply-stage writes; ``keep`` is evaluate-only; ``reject`` is admission-time."""
    return action in _APPLY_WRITES


@dataclass(frozen=True, slots=True)
class Decision:
    request_id: str
    action: DecisionAction
    profile: ProfileContext
    reason: str = ""
    authorized_power: PowerDbm | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id is required")
        if self.action == DecisionAction.REDUCE_POWER and self.authorized_power is None:
            raise ValueError("reduce_power requires authorized_power")
        if (
            self.action in (DecisionAction.REJECT, DecisionAction.SUSPEND, DecisionAction.TERMINATE)
            and self.authorized_power is not None
        ):
            raise ValueError(f"{self.action} must not carry authorized_power")
