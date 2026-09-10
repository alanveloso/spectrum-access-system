"""Availability constraint: first-class validity / event driver.

Time-bounded protection or allowance windows expire availability. That is
**not** class-versus-class preemption (see ``primitives.preemption``).

Keep-alive/heartbeat is out of scope. Protocol codecs belong in adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius, representative_point
from primitives.power import PowerDbm
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval, UtcInstant

AvailabilityArea = PointRadius | LinearRing


class AvailabilityMode(StrEnum):
    """Scheduled vs on-demand availability processing (closed set)."""

    SCHEDULED = "scheduled"
    ON_DEMAND = "on_demand"


class AvailabilityZoneKind(StrEnum):
    """Closed zone kinds for allowance, restriction, protection, and exclusion."""

    ALLOWANCE = "allowance"
    RESTRICTION = "restriction"
    PROTECTION = "protection"
    EXCLUSION = "exclusion"


class AvailabilityEventKind(StrEnum):
    UPDATED = "updated"
    EXPIRED = "expired"
    EVACUATION = "evacuation"


@dataclass(frozen=True, slots=True)
class AvailabilityScope:
    """Frequency × optional geography × optional power cap for one window."""

    frequency: FrequencyRange
    area: AvailabilityArea | None = None
    max_eirp: PowerDbm | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityConstraint:
    """Named availability window from a protection/availability source (not an access class)."""

    constraint_id: str
    mode: AvailabilityMode
    validity: TimeInterval
    scope: AvailabilityScope
    zone_kind: AvailabilityZoneKind = AvailabilityZoneKind.ALLOWANCE
    source_id: str | None = None

    def __post_init__(self) -> None:
        if not self.constraint_id.strip():
            raise ValueError("constraint_id is required")
        if self.source_id is not None and not self.source_id.strip():
            raise ValueError("source_id must be non-empty when set")

    def is_in_force(self, instant: UtcInstant) -> bool:
        return self.validity.contains(instant)

    def _area_contains_point(self, point: GeoPoint) -> bool:
        assert self.scope.area is not None
        if isinstance(self.scope.area, PointRadius):
            return self.scope.area.contains(point)
        return self.scope.area.contains(point)

    def _location_matches(self, footprint: TransmissionFootprint) -> bool:
        if self.scope.area is None:
            return True
        location = footprint.location
        if isinstance(location, LinearRing):
            # Network/area footprints: vertex coverage (rings have no single point).
            if (
                isinstance(self.scope.area, LinearRing)
                and location.coordinates == self.scope.area.coordinates
            ):
                return True
            verts = location.coordinates[:-1] or location.coordinates
            if not verts:
                return False
            inside_flags = [
                self._area_contains_point(
                    GeoPoint(latitude_deg=lat, longitude_deg=lon)
                )
                for lon, lat in verts
            ]
            if self.zone_kind in (
                AvailabilityZoneKind.EXCLUSION,
                AvailabilityZoneKind.PROTECTION,
            ):
                return any(inside_flags)
            return all(inside_flags)
        point = representative_point(location)
        if point is None:
            return self.zone_kind in {
                AvailabilityZoneKind.EXCLUSION,
                AvailabilityZoneKind.PROTECTION,
            }
        return self._area_contains_point(point)

    def _power_ok(self, footprint: TransmissionFootprint) -> bool:
        if self.scope.max_eirp is None:
            return True
        return float(footprint.power.dbm) <= float(self.scope.max_eirp.dbm)

    def allows_footprint(
        self, footprint: TransmissionFootprint, instant: UtcInstant
    ) -> bool:
        """True when the footprint may transmit under this constraint at ``instant``."""
        if not self.is_in_force(instant):
            return False
        overlaps = self.scope.frequency.overlaps(footprint.frequency)
        in_area = self._location_matches(footprint)
        if self.zone_kind in (
            AvailabilityZoneKind.EXCLUSION,
            AvailabilityZoneKind.PROTECTION,
        ):
            # In-force exclusion/protection that hits the footprint blocks use.
            if overlaps and in_area:
                return False
            return True
        # Allowance / restriction: must match scope and power.
        if not overlaps or not in_area:
            return False
        return self._power_ok(footprint)

    def expired_relative_to(
        self, previous_in_force: bool, instant: UtcInstant
    ) -> bool:
        """Availability expiry transition (window left force), not preemption."""
        return previous_in_force and not self.is_in_force(instant)


@dataclass(frozen=True, slots=True)
class AvailabilityChangeEvent:
    """First-class trigger for event-driven reevaluation (not a keep-alive)."""

    event_id: str
    constraint_id: str
    observed_at: UtcInstant
    kind: AvailabilityEventKind

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not self.constraint_id.strip():
            raise ValueError("constraint_id is required")


def any_constraint_allows(
    constraints: tuple[AvailabilityConstraint, ...],
    footprint: TransmissionFootprint,
    instant: UtcInstant,
) -> bool:
    """Fail closed when no constraints are provided."""
    if not constraints:
        return False
    return any(item.allows_footprint(footprint, instant) for item in constraints)
