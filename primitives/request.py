"""Spectrum request: authorization holder plus transmission footprint(s)."""

from __future__ import annotations

from dataclasses import dataclass

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius
from primitives.power import PowerDbm
from primitives.time import TimeInterval, UtcInstant

FootprintLocation = GeoPoint | PointRadius | LinearRing


@dataclass(frozen=True, slots=True)
class TransmissionFootprint:
    """One frequency × geography × power scope. Validity is optional (not a lease)."""

    frequency: FrequencyRange
    power: PowerDbm
    location: FootprintLocation
    validity: TimeInterval | None = None


@dataclass(frozen=True, slots=True)
class SpectrumRequest:
    """Generic admission/reevaluation request. Opaque ids; no protocol nouns."""

    request_id: str
    holder_id: str
    footprints: tuple[TransmissionFootprint, ...]
    requested_at: UtcInstant
    access_class_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id is required")
        if not self.holder_id.strip():
            raise ValueError("holder_id is required")
        if not self.footprints:
            raise ValueError("SpectrumRequest requires at least one footprint")
        if self.access_class_id is not None and not self.access_class_id.strip():
            raise ValueError("access_class_id must be a non-empty token when set")
