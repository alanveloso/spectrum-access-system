"""Constraints on frequency subsets and power. Distinct from access classes (D11)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from primitives.frequency import FrequencyRange
from primitives.geography import LinearRing, PointRadius
from primitives.power import PowerDbm
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval


class ConstraintKind(StrEnum):
    FREQUENCY_ALLOW = "frequency_allow"
    FREQUENCY_DENY = "frequency_deny"
    MAX_POWER = "max_power"


ConstraintArea = PointRadius | LinearRing


@dataclass(frozen=True, slots=True)
class Constraint:
    """Declarative restriction. Construction is fail-closed; no expression language."""

    kind: ConstraintKind
    frequency: FrequencyRange | None = None
    max_power: PowerDbm | None = None
    area: ConstraintArea | None = None
    validity: TimeInterval | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind in (ConstraintKind.FREQUENCY_ALLOW, ConstraintKind.FREQUENCY_DENY):
            if self.frequency is None:
                raise ValueError(f"{self.kind} requires frequency")
        if self.kind == ConstraintKind.MAX_POWER and self.max_power is None:
            raise ValueError("max_power constraint requires max_power")

    def frequency_overlaps(self, footprint: TransmissionFootprint) -> bool:
        if self.frequency is None:
            return True
        return self.frequency.overlaps(footprint.frequency)
