"""CBRS Profile composition (canonical document)."""

from __future__ import annotations

from profiles.context import profile_context_from_document
from profiles.parse import load_profile


def test_cbrs_composes_registered_mechanisms_and_band() -> None:
    v2 = load_profile("cbrs_winnforum")
    assert v2.metadata.id == "cbrs_winnforum"
    assert v2.metadata.version == "2.0.0"
    assert v2.metadata.status == "reference"
    rng = v2.spectrum.ranges[0]
    assert rng.low_hz == 3_550_000_000
    assert rng.high_hz == 3_700_000_000
    ch = v2.spectrum.channelization
    assert ch is not None
    assert ch.width_hz == 10_000_000
    assert ch.origin_hz == rng.low_hz
    assert v2.access is not None
    assert [c.id for c in v2.access.classes] == ["incumbent", "pal", "gaa"]
    assert v2.authorization is not None
    assert v2.authorization.duration_s == 900
    assert v2.temporal is not None
    assert v2.temporal.reevaluation is not None
    assert v2.temporal.reevaluation.interval_s == 60
    assert v2.coordination is not None
    assert v2.coordination.mechanism == "snapshot_evaluate_apply"
    assert v2.rf is not None
    assert v2.rf.required is True
    assert v2.rf.propagation_model == "path_loss"
    assert v2.protection is not None
    assert "iap" not in v2.protection.mechanisms
    assert "dpa" not in v2.protection.mechanisms
    ctx = profile_context_from_document(v2)
    assert ctx.profile_id == "cbrs_winnforum"
    assert ctx.profile_hash
    assert ctx.rf_provenance == "path_loss_plus_aggregate/path_loss"
