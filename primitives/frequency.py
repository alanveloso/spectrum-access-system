"""Contiguous frequency interval in Hz (half-open [low_hz, high_hz))."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrequencyRange:
    """Regime-agnostic contiguous Hz interval.

    Convention matches existing interval overlap in this codebase:
    ``[low_hz, high_hz)`` — adjacent ranges that only share an edge do not overlap.
    """

    low_hz: int
    high_hz: int

    def __post_init__(self) -> None:
        if self.low_hz < 0 or self.high_hz < 0:
            raise ValueError("frequency bounds must be non-negative Hz")
        if self.high_hz <= self.low_hz:
            raise ValueError("FrequencyRange requires high_hz > low_hz")

    @property
    def width_hz(self) -> int:
        return self.high_hz - self.low_hz

    def contains_hz(self, hz: int) -> bool:
        return self.low_hz <= hz < self.high_hz

    def contains(self, other: FrequencyRange) -> bool:
        return self.low_hz <= other.low_hz and other.high_hz <= self.high_hz

    def overlaps(self, other: FrequencyRange) -> bool:
        return self.low_hz < other.high_hz and self.high_hz > other.low_hz

    def intersection(self, other: FrequencyRange) -> FrequencyRange | None:
        low = max(self.low_hz, other.low_hz)
        high = min(self.high_hz, other.high_hz)
        if high <= low:
            return None
        return FrequencyRange(low_hz=low, high_hz=high)
