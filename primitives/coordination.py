"""Immutable freeze → evaluate → write-back cycle (catalog snapshot_evaluate_apply).

Regime-specific protection and publication stay outside this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from primitives.decision import Decision, is_apply_write
from primitives.profile_context import ProfileContext
from primitives.time import UtcInstant

S = TypeVar("S")
D = TypeVar("D")
R = TypeVar("R", covariant=True)


@dataclass(frozen=True, slots=True)
class FrozenEvaluation:
    """Opaque freeze metadata. Item ids are tokens, not protocol nouns."""

    frozen_at: UtcInstant
    subject_ids: tuple[str, ...]
    profile: ProfileContext

    def __post_init__(self) -> None:
        for item in self.subject_ids:
            if not str(item).strip():
                raise ValueError("subject_ids must be non-empty tokens")


@runtime_checkable
class CoordinationCycle(Protocol[S, D, R]):
    """Three-stage periodic/event coordination. Implementations own persistence."""

    def snapshot(self) -> S: ...

    def evaluate(self, frozen: S) -> Sequence[D]: ...

    def apply(self, frozen: S, decisions: Sequence[D]) -> R: ...


def writeback_decisions(decisions: Sequence[Decision]) -> tuple[Decision, ...]:
    """Keep only evaluate-time actions that mutate authorization state."""
    return tuple(d for d in decisions if is_apply_write(d.action))


def run_snapshot_evaluate_apply(cycle: CoordinationCycle[S, D, R]) -> R:
    """Run freeze, then evaluate, then write-back. Missing freeze fails closed."""
    frozen = cycle.snapshot()
    if frozen is None:
        raise ValueError("coordination cycle requires a frozen evaluation")
    decisions = cycle.evaluate(frozen)
    if decisions is None:
        raise ValueError("coordination evaluate must return a sequence")
    return cycle.apply(frozen, decisions)
