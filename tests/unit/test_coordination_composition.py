"""generic freeze/evaluate/write-back cycle; CPAS stays a profile adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primitives.coordination import (
    CoordinationCycle,
    FrozenEvaluation,
    run_snapshot_evaluate_apply,
    writeback_decisions,
)
from primitives.decision import Decision, DecisionAction
from primitives.power import PowerDbm
from primitives.profile_context import ProfileContext
from primitives.time import UtcInstant


def _ctx() -> ProfileContext:
    return ProfileContext(profile_id="ex", profile_version="1", profile_hash="h")


class _FakeCycle:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.frozen = FrozenEvaluation(
            frozen_at=UtcInstant(datetime(2026, 8, 18, tzinfo=timezone.utc)),
            subject_ids=("a", "b"),
            profile=_ctx(),
        )

    def snapshot(self) -> FrozenEvaluation:
        self.order.append("snapshot")
        return self.frozen

    def evaluate(self, frozen: FrozenEvaluation) -> tuple[Decision, ...]:
        self.order.append("evaluate")
        assert frozen is self.frozen
        return (
            Decision(request_id="a", action=DecisionAction.KEEP, profile=_ctx()),
            Decision(request_id="b", action=DecisionAction.TERMINATE, profile=_ctx()),
        )

    def apply(self, frozen: FrozenEvaluation, decisions) -> int:
        self.order.append("apply")
        writable = writeback_decisions(tuple(decisions))
        assert len(writable) == 1
        assert writable[0].action is DecisionAction.TERMINATE
        return len(writable)


def test_run_cycle_orders_stages_and_filters_writebacks():
    cycle = _FakeCycle()
    assert isinstance(cycle, CoordinationCycle)
    assert run_snapshot_evaluate_apply(cycle) == 1
    assert cycle.order == ["snapshot", "evaluate", "apply"]


def test_run_cycle_fails_closed_without_freeze():
    class Empty:
        def snapshot(self):
            return None

        def evaluate(self, frozen):
            return ()

        def apply(self, frozen, decisions):
            return 0

    with pytest.raises(ValueError, match="frozen evaluation"):
        run_snapshot_evaluate_apply(Empty())


def test_frozen_evaluation_rejects_blank_subject_id():
    with pytest.raises(ValueError, match="subject_ids"):
        FrozenEvaluation(
            frozen_at=UtcInstant(datetime(2026, 8, 18, tzinfo=timezone.utc)),
            subject_ids=("ok", " "),
            profile=_ctx(),
        )


def test_cpas_adapter_satisfies_cycle_protocol():
    from services.cpas_cycle import CpasCoordinationCycle

    assert issubclass(CpasCoordinationCycle, object)
    methods = ("snapshot", "evaluate", "apply")
    for name in methods:
        assert callable(getattr(CpasCoordinationCycle, name))


def test_reduce_power_is_writeback():
    d = Decision(
        request_id="r",
        action=DecisionAction.REDUCE_POWER,
        profile=_ctx(),
        authorized_power=PowerDbm(1.0),
    )
    assert writeback_decisions((d,)) == (d,)
