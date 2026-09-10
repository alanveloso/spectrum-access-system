"""UTC time instants and half-open validity windows in seconds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class UtcInstant:
    value: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_aware_utc(self.value))


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """Half-open ``[start, end)`` validity window. Duration is seconds (D22 units)."""

    start: UtcInstant
    end: UtcInstant

    def __post_init__(self) -> None:
        if self.end.value <= self.start.value:
            raise ValueError("TimeInterval requires end > start")

    @classmethod
    def from_datetimes(cls, start: datetime, end: datetime) -> TimeInterval:
        return cls(start=UtcInstant(start), end=UtcInstant(end))

    @property
    def duration_seconds(self) -> float:
        return (self.end.value - self.start.value).total_seconds()

    def contains(self, instant: UtcInstant) -> bool:
        return self.start.value <= instant.value < self.end.value

    def overlaps(self, other: TimeInterval) -> bool:
        return self.start.value < other.end.value and self.end.value > other.start.value

    def contains_interval(self, other: TimeInterval) -> bool:
        """True when ``other`` is fully inside this half-open window."""
        return self.start.value <= other.start.value and other.end.value <= self.end.value
