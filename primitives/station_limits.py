"""Reusable station/spectrum admission limits.

Closed selectors only — no expression language. Not country-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DuplexMode(StrEnum):
    TDD = "tdd"
    FDD = "fdd"
    HALF_DUPLEX = "half_duplex"
    SIMPLEX = "simplex"


@dataclass(frozen=True, slots=True)
class DuplexModeRequirement:
    """Declared duplex mode for a band/regime (e.g. TDD-only local networks)."""

    mode: DuplexMode

    def allows(self, mode: DuplexMode | str) -> bool:
        if isinstance(mode, DuplexMode):
            return mode == self.mode
        try:
            return DuplexMode(str(mode)) == self.mode
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class MaxAssignmentBandwidth:
    """Cap on assigned contiguous bandwidth (Hz), optional indoor/outdoor selector."""

    max_bandwidth_hz: int
    indoor_outdoor: str | None = None

    def __post_init__(self) -> None:
        if self.max_bandwidth_hz <= 0:
            raise ValueError("max_bandwidth_hz must be positive")
        if self.indoor_outdoor is not None and self.indoor_outdoor not in {
            "indoor",
            "outdoor",
        }:
            raise ValueError("indoor_outdoor must be 'indoor' or 'outdoor'")

    def applies_to(self, indoor_outdoor: str | None) -> bool:
        if self.indoor_outdoor is None:
            return True
        return indoor_outdoor == self.indoor_outdoor

    def allows(
        self, low_hz: int, high_hz: int, *, indoor_outdoor: str | None = None
    ) -> bool:
        if not self.applies_to(indoor_outdoor):
            return True
        if high_hz <= low_hz:
            return False
        return (high_hz - low_hz) <= self.max_bandwidth_hz


@dataclass(frozen=True, slots=True)
class AntennaHeightLimit:
    """Maximum antenna height AGL (m), optional indoor/outdoor and device_class selectors."""

    max_height_m: float
    indoor_outdoor: str | None = None
    device_class: str | None = None

    def __post_init__(self) -> None:
        if self.max_height_m <= 0:
            raise ValueError("max_height_m must be positive")
        if self.indoor_outdoor is not None and self.indoor_outdoor not in {
            "indoor",
            "outdoor",
        }:
            raise ValueError("indoor_outdoor must be 'indoor' or 'outdoor'")
        if self.device_class is not None and not self.device_class.strip():
            raise ValueError("device_class must be non-empty when set")

    def applies_to(
        self, *, indoor_outdoor: str | None = None, device_class: str | None = None
    ) -> bool:
        if self.indoor_outdoor is not None and indoor_outdoor != self.indoor_outdoor:
            return False
        if self.device_class is not None and device_class != self.device_class:
            return False
        return True

    def allows(
        self,
        height_m: float,
        *,
        indoor_outdoor: str | None = None,
        device_class: str | None = None,
    ) -> bool:
        if height_m < 0:
            return False
        if not self.applies_to(
            indoor_outdoor=indoor_outdoor, device_class=device_class
        ):
            return True
        return height_m <= self.max_height_m


@dataclass(frozen=True, slots=True)
class ForbiddenDeviceRoles:
    """Opaque device-role denylist (e.g. repeater, booster). Not vendor names."""

    roles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError("roles must be non-empty")
        if any(not role.strip() for role in self.roles):
            raise ValueError("roles must be non-empty tokens")

    def allows(self, role: str) -> bool:
        return role not in self.roles
