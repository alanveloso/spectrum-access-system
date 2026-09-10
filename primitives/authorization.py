"""Lease, fixed window, authorized area, and exclusion zone (D8/D9/D16).

Keep-alive/refresh is not part of the lease. Availability windows/events are
first-class in ``primitives.availability``. This is not a protocol
authorization object.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius, representative_point
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval, UtcInstant

GeoArea = PointRadius | LinearRing


class LeaseState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    EXPIRED = "expired"


_TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.ACTIVE: frozenset(
        {LeaseState.SUSPENDED, LeaseState.TERMINATED, LeaseState.EXPIRED}
    ),
    LeaseState.SUSPENDED: frozenset(
        {LeaseState.ACTIVE, LeaseState.TERMINATED, LeaseState.EXPIRED}
    ),
    LeaseState.TERMINATED: frozenset(),
    LeaseState.EXPIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Lease:
    """Holder + frequency×geography×power scope + validity + state machine."""

    lease_id: str
    holder_id: str
    footprints: tuple[TransmissionFootprint, ...]
    validity: TimeInterval
    state: LeaseState = LeaseState.ACTIVE

    def __post_init__(self) -> None:
        if not self.lease_id.strip():
            raise ValueError("lease_id is required")
        if not self.holder_id.strip():
            raise ValueError("holder_id is required")
        if not self.footprints:
            raise ValueError("Lease requires at least one footprint")

    def is_usable_at(self, instant: UtcInstant) -> bool:
        return self.state == LeaseState.ACTIVE and self.validity.contains(instant)

    def transition(self, new_state: LeaseState) -> Lease:
        if new_state == self.state:
            return self
        allowed = _TRANSITIONS[self.state]
        if new_state not in allowed:
            raise ValueError(f"illegal lease transition {self.state} -> {new_state}")
        return replace(self, state=new_state)

    def expire_if_due(self, instant: UtcInstant) -> Lease:
        if self.state in (LeaseState.TERMINATED, LeaseState.EXPIRED):
            return self
        if self.validity.contains(instant):
            return self
        return self.transition(LeaseState.EXPIRED)


@dataclass(frozen=True, slots=True)
class FixedWindow:
    """Outer validity cap. A lease interval must be contained in this window."""

    window: TimeInterval

    def allows_validity(self, validity: TimeInterval) -> bool:
        return self.window.contains_interval(validity)


@dataclass(frozen=True, slots=True)
class AuthorizedArea:
    area_id: str
    ring: LinearRing

    def __post_init__(self) -> None:
        if not self.area_id.strip():
            raise ValueError("area_id is required")

    def allows(self, location: GeoPoint | PointRadius | LinearRing) -> bool:
        point = representative_point(location)
        if point is None:
            return False
        return self.ring.contains(point)


@dataclass(frozen=True, slots=True)
class ExclusionZone:
    """Frequency×geography exclusion. Untestable location is treated as excluded."""

    zone_id: str
    frequency: FrequencyRange
    area: GeoArea

    def __post_init__(self) -> None:
        if not self.zone_id.strip():
            raise ValueError("zone_id is required")

    def excludes(self, footprint: TransmissionFootprint) -> bool:
        if not self.frequency.overlaps(footprint.frequency):
            return False
        point = representative_point(footprint.location)
        if point is None:
            return True
        if isinstance(self.area, PointRadius):
            return self.area.contains(point)
        return self.area.contains(point)
