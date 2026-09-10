"""Periodic keep-alive / refresh, separate from lease validity (catalog periodic_refresh).

Does not authorize spectrum. Does not extend a lease by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from primitives.time import TimeInterval, UtcInstant

# Synthetic origin so exclusive-end validity can be a TimeInterval without a stored start.
_OPEN_ORIGIN = datetime(1970, 1, 1, tzinfo=timezone.utc)


def open_until(end: UtcInstant, instant: UtcInstant) -> bool:
    """True when ``instant`` is in ``[origin, end)``. Equal to ``end`` is closed."""
    if end.value <= _OPEN_ORIGIN:
        return False
    return TimeInterval(start=UtcInstant(_OPEN_ORIGIN), end=end).contains(instant)


@dataclass(frozen=True, slots=True)
class PeriodicRefresh:
    """Advertised keep-alive period in seconds. Not outer lease validity."""

    interval_seconds: int

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")

    def next_after(self, instant: UtcInstant) -> UtcInstant:
        return UtcInstant(instant.value + timedelta(seconds=self.interval_seconds))

    def advertised(self, stored_seconds: int | None) -> int:
        """Prefer a positive stored interval; otherwise this default."""
        if stored_seconds is None or stored_seconds <= 0:
            return self.interval_seconds
        return int(stored_seconds)
