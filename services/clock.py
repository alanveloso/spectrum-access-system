"""Injectable UTC wall-clock for deterministic scheduling tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

ClockProvider = Callable[[], datetime]

_provider: ClockProvider | None = None


def ensure_utc(value: datetime) -> datetime:
    """Normalize to timezone-aware UTC (naive inputs are treated as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    """Return timezone-aware UTC now (overridable via :func:`set_clock_provider`)."""
    if _provider is not None:
        return ensure_utc(_provider())
    return datetime.now(timezone.utc)


def set_clock_provider(provider: ClockProvider | None) -> None:
    """Install or clear a process-wide clock provider (tests only)."""
    global _provider
    _provider = provider


def reset_clock_provider() -> None:
    set_clock_provider(None)
