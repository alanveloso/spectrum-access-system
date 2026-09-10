"""generic SpectrumRequest / Constraint / Decision contract."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primitives.constraint import Constraint, ConstraintKind
from primitives.decision import Decision, DecisionAction, is_apply_write
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint
from primitives.power import PowerDbm
from primitives.profile_context import ProfileContext
from primitives.request import SpectrumRequest, TransmissionFootprint
from primitives.time import UtcInstant

_BANNED = (
    "cbsd",
    "pal",
    "gaa",
    "incumbent",
    "dpa",
    "esc",
    "ppa",
    "cpas",
    "fss",
    "grant",
    "winnforum",
    "fcc",
    "brasil",
    "brazil",
    "canada",
)


def _ctx() -> ProfileContext:
    return ProfileContext(
        profile_id="example",
        profile_version="1.0.0",
        profile_hash="abc",
        dataset_versions=(("terrain", "1"),),
        rf_provenance="none",
    )


def _request() -> SpectrumRequest:
    fp = TransmissionFootprint(
        frequency=FrequencyRange(1000, 2000),
        power=PowerDbm(20.0),
        location=GeoPoint(0.0, 0.0),
    )
    return SpectrumRequest(
        request_id="r1",
        holder_id="h1",
        footprints=(fp,),
        requested_at=UtcInstant(datetime(2026, 8, 15, tzinfo=timezone.utc)),
    )


def test_request_requires_holder_and_footprint():
    with pytest.raises(ValueError):
        SpectrumRequest(
            request_id="r1",
            holder_id=" ",
            footprints=_request().footprints,
            requested_at=_request().requested_at,
        )
    with pytest.raises(ValueError):
        SpectrumRequest(
            request_id="r1",
            holder_id="h1",
            footprints=(),
            requested_at=_request().requested_at,
        )


def test_profile_context_required_identity():
    with pytest.raises(ValueError):
        ProfileContext(profile_id="", profile_version="1", profile_hash="h")
    ctx = _ctx()
    assert ctx.profile_id == "example"
    assert ctx.dataset_versions == (("terrain", "1"),)


def test_constraint_kinds_fail_closed():
    rng = FrequencyRange(1000, 1500)
    allow = Constraint(kind=ConstraintKind.FREQUENCY_ALLOW, frequency=rng)
    deny = Constraint(kind=ConstraintKind.FREQUENCY_DENY, frequency=rng)
    cap = Constraint(kind=ConstraintKind.MAX_POWER, max_power=PowerDbm(10.0))
    fp = _request().footprints[0]
    assert allow.frequency_overlaps(fp) is True
    assert deny.frequency_overlaps(fp) is True
    assert cap.frequency_overlaps(fp) is True
    with pytest.raises(ValueError):
        Constraint(kind=ConstraintKind.FREQUENCY_DENY)
    with pytest.raises(ValueError):
        Constraint(kind=ConstraintKind.MAX_POWER)


def test_decision_closed_actions_and_power_invariants():
    ctx = _ctx()
    keep = Decision(request_id="r1", action=DecisionAction.KEEP, profile=ctx)
    assert is_apply_write(keep.action) is False
    reject = Decision(request_id="r1", action=DecisionAction.REJECT, profile=ctx)
    assert is_apply_write(reject.action) is False
    term = Decision(request_id="r1", action=DecisionAction.TERMINATE, profile=ctx)
    assert is_apply_write(term.action) is True
    reduced = Decision(
        request_id="r1",
        action=DecisionAction.REDUCE_POWER,
        profile=ctx,
        authorized_power=PowerDbm(5.0),
    )
    assert reduced.authorized_power is not None
    with pytest.raises(ValueError):
        Decision(request_id="r1", action=DecisionAction.REDUCE_POWER, profile=ctx)
    with pytest.raises(ValueError):
        Decision(
            request_id="r1",
            action=DecisionAction.REJECT,
            profile=ctx,
            authorized_power=PowerDbm(1.0),
        )
    assert "reassign" not in {a.value for a in DecisionAction}


def test_g2_002_modules_have_no_regime_nouns_or_service_imports():
    root = Path(__file__).resolve().parents[2] / "primitives"
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        for token in _BANNED:
            assert token not in lowered, f"{path.name} contains banned token {token!r}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("services")
                    assert not alias.name.startswith("models")
                    assert not alias.name.startswith("routes")
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("services")
                assert not node.module.startswith("models")
                assert not node.module.startswith("routes")
