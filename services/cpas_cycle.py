"""CBRS CPAS as a CoordinationCycle. Pipeline extras (FAD, generation) stay here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from services.cpas_service import (
    CpasDecision,
    CpasSnapshot,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    freeze_cpas_snapshot,
)


@dataclass(frozen=True, slots=True)
class CpasCycleResult:
    snapshot: CpasSnapshot
    decisions: tuple[CpasDecision, ...]
    writes: int


class CpasCoordinationCycle:
    """Adapts freeze/evaluate/apply functions to the generic cycle protocol."""

    def __init__(
        self,
        db: Session,
        *,
        peer_sync_report: dict[str, Any] | None = None,
        evaluate_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self._peer_sync_report = peer_sync_report
        self._evaluate_kwargs = dict(evaluate_kwargs or {})

    def snapshot(self) -> CpasSnapshot:
        return freeze_cpas_snapshot(self._db, self._peer_sync_report)

    def evaluate(self, frozen: CpasSnapshot) -> list[CpasDecision]:
        return evaluate_cpas_protections(self._db, frozen, **self._evaluate_kwargs)

    def apply(
        self, frozen: CpasSnapshot, decisions: Sequence[CpasDecision]
    ) -> CpasCycleResult:
        writes = apply_cpas_decisions(self._db, list(decisions))
        return CpasCycleResult(
            snapshot=frozen,
            decisions=tuple(decisions),
            writes=writes,
        )
