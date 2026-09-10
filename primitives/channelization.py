"""Fixed-width assignment grid (D13). Not RF aggregation resolution."""

from __future__ import annotations

from primitives.frequency import FrequencyRange


def assignment_channels(
    low_hz: int,
    high_hz: int,
    *,
    width_hz: int,
    origin_hz: int,
    clip_low_hz: int,
    clip_high_hz: int,
) -> tuple[FrequencyRange, ...]:
    """Split ``[low_hz, high_hz)`` onto a fixed-width grid, clipped to an allowed band.

    Leftover head/tail shorter than ``width_hz`` are kept so edge contain-checks match.
    """
    if width_hz <= 0:
        raise ValueError("width_hz must be positive")
    start = max(low_hz, clip_low_hz)
    end = min(high_hz, clip_high_hz)
    if end <= start:
        return ()
    rel = start - origin_hz
    aligned = origin_hz + ((rel + width_hz - 1) // width_hz) * width_hz
    if (start - origin_hz) % width_hz == 0:
        aligned = start
    elif aligned - width_hz >= start and aligned - width_hz >= clip_low_hz:
        aligned = aligned - width_hz
    channels: list[FrequencyRange] = []
    if start < aligned:
        head_end = min(aligned, end)
        if head_end > start:
            channels.append(FrequencyRange(low_hz=start, high_hz=head_end))
    cur = aligned
    while cur + width_hz <= end:
        channels.append(FrequencyRange(low_hz=cur, high_hz=cur + width_hz))
        cur += width_hz
    if cur < end:
        channels.append(FrequencyRange(low_hz=cur, high_hz=end))
    return tuple(channels)
