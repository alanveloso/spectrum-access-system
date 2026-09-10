"""profile id/version/hash freeze into ProfileContext."""

from __future__ import annotations

from primitives.decision import Decision, DecisionAction
from profiles.context import profile_context_from_document, profile_hash
from profiles.parse import parse_profile_document


def _doc() -> dict:
    return {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "example", "version": "1.0.0", "status": "custom"},
        "spectrum": {"ranges": [{"id": "main", "low_hz": 1000, "high_hz": 2000}]},
        "coordination": {"mechanism": "snapshot_evaluate_apply"},
        "rf": {
            "required": True,
            "policy": "path_loss_plus_aggregate",
            "propagation_model": "path_loss",
        },
        "data": {"required_capabilities": ["terrain"]},
        "requirements": {"device_capabilities": ["geolocation"]},
    }


def test_hash_stable_and_changes_with_content():
    parsed = parse_profile_document(_doc())
    again = parse_profile_document(_doc())
    assert profile_hash(parsed) == profile_hash(again)
    other = _doc()
    other["metadata"] = {"id": "example", "version": "1.0.1", "status": "custom"}
    parsed_other = parse_profile_document(other)
    assert profile_hash(parsed) != profile_hash(parsed_other)


def test_context_records_id_version_hash_and_binds_decision():
    parsed = parse_profile_document(_doc())
    ctx = profile_context_from_document(parsed)
    assert ctx.profile_id == "example"
    assert ctx.profile_version == "1.0.0"
    assert len(ctx.profile_hash) == 64
    assert ("terrain", "required") in ctx.dataset_versions
    assert ("snapshot_evaluate_apply", "1.0.0") in ctx.mechanism_versions
    assert ctx.rf_provenance == "path_loss_plus_aggregate/path_loss"
    decision = Decision(
        request_id="r1",
        action=DecisionAction.KEEP,
        profile=ctx,
        reason="ok",
    )
    assert decision.profile.profile_hash == ctx.profile_hash
    assert decision.profile is ctx
