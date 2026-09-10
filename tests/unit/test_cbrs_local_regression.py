"""Local behavioral comparison after CBRS recomposition.

Not an official WInnForum harness campaign.
"""

from __future__ import annotations

from services.grant_service import DEFAULT_GRANT_DURATION_SEC, HEARTBEAT_INTERVAL_SEC
from services.spectrum_inquiry_service import CBRS_HIGH_HZ, CBRS_LOW_HZ, CHANNEL_HZ
from profiles.parse import load_profile


def test_runtime_cbrs_constants_match_canonical_composition() -> None:
    v2 = load_profile("cbrs_winnforum")
    rng = v2.spectrum.ranges[0]
    ch = v2.spectrum.channelization
    assert ch is not None
    assert v2.authorization is not None
    assert v2.temporal is not None
    assert v2.temporal.reevaluation is not None

    assert rng.low_hz == CBRS_LOW_HZ == 3_550_000_000
    assert rng.high_hz == CBRS_HIGH_HZ == 3_700_000_000
    assert ch.width_hz == CHANNEL_HZ == 10_000_000
    assert ch.origin_hz == CBRS_LOW_HZ
    assert v2.authorization.duration_s == DEFAULT_GRANT_DURATION_SEC == 900
    assert v2.temporal.reevaluation.interval_s == HEARTBEAT_INTERVAL_SEC == 60
    assert v2.metadata.version == "2.0.0"
