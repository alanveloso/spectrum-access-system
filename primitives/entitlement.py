"""Protection entitlement: a right to protected use in a frequency×geo scope (D12).

This is not an access class and not a constraint-source identity. Holder ids are
opaque. Geographic area here is the right's scope, not an exclusion mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass

from primitives.frequency import FrequencyRange
from primitives.geography import LinearRing, PointRadius, representative_point
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval, UtcInstant

EntitlementArea = PointRadius | LinearRing


@dataclass(frozen=True, slots=True)
class ProtectionEntitlement:
    entitlement_id: str
    holder_id: str
    frequency: FrequencyRange
    area: EntitlementArea | None = None
    validity: TimeInterval | None = None

    def __post_init__(self) -> None:
        if not self.entitlement_id.strip():
            raise ValueError("entitlement_id is required")
        if not self.holder_id.strip():
            raise ValueError("holder_id is required")

    def covers(
        self,
        *,
        holder_id: str,
        footprint: TransmissionFootprint,
        at: UtcInstant | None = None,
    ) -> bool:
        """True when the holder’s footprint is a subset of this right’s scope."""
        if holder_id != self.holder_id:
            return False
        if not self.frequency.contains(footprint.frequency):
            return False
        if self.validity is not None:
            if at is None or not self.validity.contains(at):
                return False
        if self.area is None:
            return True
        point = representative_point(footprint.location)
        if point is None:
            return False
        return self.area.contains(point)
