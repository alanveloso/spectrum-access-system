"""assignment_channels matches SIQ 10 MHz split identity."""

from __future__ import annotations

from primitives.channelization import assignment_channels
from services.spectrum_inquiry_service import (
    CBRS_HIGH_HZ,
    CBRS_LOW_HZ,
    CHANNEL_HZ,
    _split_10mhz,
)


def _channels(low: int, high: int):
    return assignment_channels(
        low,
        high,
        width_hz=CHANNEL_HZ,
        origin_hz=CBRS_LOW_HZ,
        clip_low_hz=CBRS_LOW_HZ,
        clip_high_hz=CBRS_HIGH_HZ,
    )


def test_split_identity_full_band_and_edges():
    cases = (
        (CBRS_LOW_HZ, CBRS_HIGH_HZ),
        (CBRS_LOW_HZ, CBRS_LOW_HZ),
        (CBRS_HIGH_HZ, CBRS_HIGH_HZ),
        (3_000_000_000, 3_010_000_000),
        (3_555_000_000, 3_575_000_000),
        (3_548_000_000, 3_562_000_000),
        (3_690_000_000, 3_705_000_000),
        (3_560_000_000, 3_570_000_000),
    )
    for low, high in cases:
        split = _split_10mhz(low, high)
        primitive = [(c.low_hz, c.high_hz) for c in _channels(low, high)]
        assert primitive == split


def test_origin_relative_alignment_not_absolute_hz_modulo():
    """A non-zero origin must not use absolute Hz % width (CBRS grid is 3550-based)."""
    width = 10_000_000
    origin = 3_550_000_000
    channels = assignment_channels(
        3_555_000_000,
        3_575_000_000,
        width_hz=width,
        origin_hz=origin,
        clip_low_hz=origin,
        clip_high_hz=origin + 150_000_000,
    )
    assert [(c.low_hz, c.high_hz) for c in channels] == [
        (3_555_000_000, 3_560_000_000),
        (3_560_000_000, 3_570_000_000),
        (3_570_000_000, 3_575_000_000),
    ]
