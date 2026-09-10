"""Spectrum Profile spectrum section plus access/power/time/geography."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Annotated, Literal, Union

from primitives.access import AccessClass, OrderedAccess
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius
from primitives.station_limits import (
    AntennaHeightLimit,
    DuplexMode,
    DuplexModeRequirement,
    ForbiddenDeviceRoles,
    MaxAssignmentBandwidth,
)


class SpectrumSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    low_hz: int
    high_hz: int

    @model_validator(mode="after")
    def _interval(self) -> SpectrumSegment:
        FrequencyRange(low_hz=self.low_hz, high_hz=self.high_hz)
        return self


class FixedWidthChannelization(BaseModel):
    """Assignment grid only (D13). RF aggregation resolution is a separate parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    width_hz: int = Field(..., gt=0)
    origin_hz: int = Field(..., ge=0)
    role: str = Field(default="assignment")

    @model_validator(mode="after")
    def _role_and_mechanism(self) -> FixedWidthChannelization:
        if self.mechanism != "fixed_width_channelization":
            raise ValueError("unsupported channelization mechanism")
        if self.role != "assignment":
            raise ValueError("channelization.role must be 'assignment'")
        return self


class SpectrumRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    low_hz: int
    high_hz: int
    segments: tuple[SpectrumSegment, ...] = ()

    @model_validator(mode="after")
    def _range_and_segments(self) -> SpectrumRange:
        parent = FrequencyRange(low_hz=self.low_hz, high_hz=self.high_hz)
        ids = [seg.id for seg in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError(f"segment ids must be unique within range {self.id!r}")
        for seg in self.segments:
            child = FrequencyRange(low_hz=seg.low_hz, high_hz=seg.high_hz)
            if not parent.contains(child):
                raise ValueError(
                    f"segment {seg.id!r} is not contained in range {self.id!r}"
                )
        return self


class SpectrumSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ranges: tuple[SpectrumRange, ...]
    channelization: FixedWidthChannelization | None = None

    @model_validator(mode="after")
    def _ranges(self) -> SpectrumSection:
        if not self.ranges:
            raise ValueError("spectrum.ranges must contain at least one range")
        ids = [item.id for item in self.ranges]
        if len(ids) != len(set(ids)):
            raise ValueError("spectrum range ids must be unique")
        ordered = sorted(self.ranges, key=lambda item: item.low_hz)
        for left, right in zip(ordered, ordered[1:], strict=False):
            a = FrequencyRange(low_hz=left.low_hz, high_hz=left.high_hz)
            b = FrequencyRange(low_hz=right.low_hz, high_hz=right.high_hz)
            if a.overlaps(b):
                raise ValueError("spectrum ranges must not overlap")
        return self


class ProfileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    status: str = Field(default="custom")
    references: tuple[str, ...] = ()
    based_on: str | None = None


class AccessClassConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    priority: int
    preemptible: bool


class AccessSection(BaseModel):
    """Omitted on the parent document when the regime has no classes (D10)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    classes: tuple[AccessClassConfig, ...]

    @model_validator(mode="after")
    def _ordered_classes(self) -> AccessSection:
        if self.mechanism != "ordered_classes":
            raise ValueError("access.mechanism must be ordered_classes")
        OrderedAccess(
            classes=tuple(
                AccessClass(
                    class_id=item.id,
                    priority=item.priority,
                    preemptible=item.preemptible,
                )
                for item in self.classes
            )
        )
        return self


class AuthorizationSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    duration_s: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _duration(self) -> AuthorizationSection:
        if self.mechanism == "dynamic_lease" and self.duration_s is None:
            raise ValueError("dynamic_lease requires duration_s")
        return self


class PowerRule(BaseModel):
    """Closed selector set (D15). No expressions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_eirp_dbm: float
    max_psd_dbm_mhz: float | None = None
    indoor_outdoor: str | None = None
    height_m_low: float | None = None
    height_m_high: float | None = None
    area_id: str | None = None
    device_class: str | None = None

    @model_validator(mode="after")
    def _selectors(self) -> PowerRule:
        if self.indoor_outdoor is not None and self.indoor_outdoor not in {
            "indoor",
            "outdoor",
        }:
            raise ValueError("indoor_outdoor must be 'indoor' or 'outdoor'")
        if (self.height_m_low is None) != (self.height_m_high is None):
            raise ValueError("height_m_low and height_m_high must be set together")
        if self.height_m_low is not None and self.height_m_high is not None:
            if self.height_m_low < 0 or self.height_m_high < 0:
                raise ValueError("height_m bounds must be non-negative")
            if self.height_m_high <= self.height_m_low:
                raise ValueError("height_m_high must be greater than height_m_low")
        return self


class PowerSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    rules: tuple[PowerRule, ...] = ()

    @model_validator(mode="after")
    def _mechanism(self) -> PowerSection:
        if self.mechanism != "rule_table":
            raise ValueError("power.mechanism must be rule_table")
        return self


class GeoPointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude_deg: float
    longitude_deg: float

    @model_validator(mode="after")
    def _point(self) -> GeoPointConfig:
        GeoPoint(latitude_deg=self.latitude_deg, longitude_deg=self.longitude_deg)
        return self


class AuthorizedAreaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    ring: tuple[tuple[float, float], ...]

    @model_validator(mode="after")
    def _ring(self) -> AuthorizedAreaConfig:
        LinearRing.from_lon_lat(self.ring)
        return self


class ExclusionZoneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    low_hz: int
    high_hz: int
    ring: tuple[tuple[float, float], ...] | None = None
    center: GeoPointConfig | None = None
    radius_m: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _area(self) -> ExclusionZoneConfig:
        FrequencyRange(low_hz=self.low_hz, high_hz=self.high_hz)
        ring_set = self.ring is not None
        ball_set = self.center is not None and self.radius_m is not None
        if ring_set == ball_set:
            raise ValueError("exclusion zone requires exactly one of ring or center+radius_m")
        if ring_set:
            LinearRing.from_lon_lat(self.ring or ())
            return self
        if self.center is None or self.radius_m is None:
            raise ValueError("exclusion zone requires exactly one of ring or center+radius_m")
        PointRadius(
            center=GeoPoint(
                latitude_deg=self.center.latitude_deg,
                longitude_deg=self.center.longitude_deg,
            ),
            radius_m=self.radius_m,
        )
        return self


class GeographySection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    authorized_areas: tuple[AuthorizedAreaConfig, ...] = ()
    exclusion_zones: tuple[ExclusionZoneConfig, ...] = ()
    center: GeoPointConfig | None = None
    radius_m: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _payload(self) -> GeographySection:
        if self.mechanism == "authorized_area" and not self.authorized_areas:
            raise ValueError("authorized_area requires authorized_areas")
        if self.mechanism == "exclusion_zone" and not self.exclusion_zones:
            raise ValueError("exclusion_zone requires exclusion_zones")
        if self.mechanism == "point_radius":
            if self.center is None or self.radius_m is None:
                raise ValueError("point_radius requires center and radius_m")
            PointRadius(
                center=GeoPoint(
                    latitude_deg=self.center.latitude_deg,
                    longitude_deg=self.center.longitude_deg,
                ),
                radius_m=self.radius_m,
            )
        return self


class PeriodicReevaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)
    interval_s: int = Field(..., gt=0)


class AvailabilityConstraintConfig(BaseModel):
    """: declare availability as a first-class temporal/authorization mechanism."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: Literal["availability_constraint"] = "availability_constraint"
    mode: Literal["scheduled", "on_demand"]


class TemporalSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reevaluation: PeriodicReevaluation | None = None
    availability: AvailabilityConstraintConfig | None = None


_DATA_CAPABILITIES = frozenset(
    {
        "terrain",
        "land_cover",
        "protected_entities",
        "rights",
        "boundaries",
        "reference_data",
    }
)
_DEVICE_CAPABILITIES = frozenset({"geolocation", "frequency_range", "max_eirp"})
# Network/managed-consumer requirements (no per-radio geolocation token).
_NETWORK_CAPABILITIES = frozenset(
    {"managed_area", "network_identity", "frequency_range", "max_eirp"}
)


class FrequencyScopeConfig(BaseModel):
    """Closed frequency window for a protection binding (Hz)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low_hz: int
    high_hz: int

    @model_validator(mode="after")
    def _interval(self) -> FrequencyScopeConfig:
        FrequencyRange(low_hz=self.low_hz, high_hz=self.high_hz)
        return self


class DistanceExclusionBinding(BaseModel):
    """Parameterized ``distance_exclusion`` instance; ``id`` is profile-local opaque."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    mechanism: Literal["distance_exclusion"] = "distance_exclusion"
    distance_m: float = Field(..., gt=0)
    frequency: FrequencyScopeConfig | None = None

    @model_validator(mode="after")
    def _id_token(self) -> DistanceExclusionBinding:
        if not self.id.strip() or self.id != self.id.strip():
            raise ValueError("binding id must be a non-blank token")
        return self


class ProtectionSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanisms: tuple[str, ...] = ()
    # Single typed binding kind; promote to a mechanism-discriminated
    # union when a second real parameterized protection binding is needed.
    bindings: tuple[DistanceExclusionBinding, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> ProtectionSection:
        if len(self.mechanisms) != len(set(self.mechanisms)):
            raise ValueError("protection.mechanisms must be unique")
        for item in self.mechanisms:
            if not item.strip():
                raise ValueError("protection.mechanisms entries must be non-empty")
        binding_ids = [item.id for item in self.bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("protection.bindings ids must be unique")
        declared = set(self.mechanisms)
        for binding in self.bindings:
            if binding.mechanism not in declared:
                raise ValueError(
                    f"binding {binding.id!r} mechanism {binding.mechanism!r} "
                    "is not listed in protection.mechanisms"
                )
        return self


class CoordinationSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str = Field(..., min_length=1)


class RfSection(BaseModel):
    """Selects registered RF policy/model. No vendor adapter names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required: bool
    policy: str = Field(..., min_length=1)
    propagation_model: str | None = None

    @model_validator(mode="after")
    def _required_model(self) -> RfSection:
        if self.required and not (self.propagation_model and self.propagation_model.strip()):
            raise ValueError("rf.required requires propagation_model")
        return self


class DataSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_capabilities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _caps(self) -> DataSection:
        unknown = [item for item in self.required_capabilities if item not in _DATA_CAPABILITIES]
        if unknown:
            raise ValueError(f"unsupported data capabilities: {unknown}")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("data.required_capabilities must be unique")
        return self


class RequirementsSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device_capabilities: tuple[str, ...] = ()
    network_capabilities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _caps(self) -> RequirementsSection:
        unknown = [item for item in self.device_capabilities if item not in _DEVICE_CAPABILITIES]
        if unknown:
            raise ValueError(f"unsupported device capabilities: {unknown}")
        if len(self.device_capabilities) != len(set(self.device_capabilities)):
            raise ValueError("requirements.device_capabilities must be unique")
        unknown_net = [
            item for item in self.network_capabilities if item not in _NETWORK_CAPABILITIES
        ]
        if unknown_net:
            raise ValueError(f"unsupported network capabilities: {unknown_net}")
        if len(self.network_capabilities) != len(set(self.network_capabilities)):
            raise ValueError("requirements.network_capabilities must be unique")
        return self


class DuplexModeConstraint(BaseModel):
    """: declared duplex mode (closed enum)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: Literal["duplex_mode"] = "duplex_mode"
    mode: Literal["tdd", "fdd", "half_duplex", "simplex"]

    def to_primitive(self) -> DuplexModeRequirement:
        return DuplexModeRequirement(mode=DuplexMode(self.mode))


class MaxAssignmentBandwidthConstraint(BaseModel):
    """: max contiguous assignment bandwidth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: Literal["max_assignment_bandwidth"] = "max_assignment_bandwidth"
    max_bandwidth_hz: int = Field(..., gt=0)
    indoor_outdoor: Literal["indoor", "outdoor"] | None = None

    def to_primitive(self) -> MaxAssignmentBandwidth:
        return MaxAssignmentBandwidth(
            max_bandwidth_hz=self.max_bandwidth_hz,
            indoor_outdoor=self.indoor_outdoor,
        )


class AntennaHeightLimitConstraint(BaseModel):
    """: max antenna height AGL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: Literal["antenna_height_limit"] = "antenna_height_limit"
    max_height_m: float = Field(..., gt=0)
    indoor_outdoor: Literal["indoor", "outdoor"] | None = None
    device_class: str | None = None

    @model_validator(mode="after")
    def _device_class(self) -> AntennaHeightLimitConstraint:
        if self.device_class is not None and not self.device_class.strip():
            raise ValueError("device_class must be non-empty when set")
        return self

    def to_primitive(self) -> AntennaHeightLimit:
        return AntennaHeightLimit(
            max_height_m=self.max_height_m,
            indoor_outdoor=self.indoor_outdoor,
            device_class=self.device_class,
        )


class ForbiddenDeviceRolesConstraint(BaseModel):
    """: opaque device-role denylist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: Literal["forbidden_device_roles"] = "forbidden_device_roles"
    roles: tuple[str, ...]

    @model_validator(mode="after")
    def _roles(self) -> ForbiddenDeviceRolesConstraint:
        if not self.roles:
            raise ValueError("roles must be non-empty")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("roles must be unique")
        if any(not role.strip() for role in self.roles):
            raise ValueError("roles must be non-empty tokens")
        return self

    def to_primitive(self) -> ForbiddenDeviceRoles:
        return ForbiddenDeviceRoles(roles=frozenset(self.roles))


ProfileConstraint = Annotated[
    Union[
        DuplexModeConstraint,
        MaxAssignmentBandwidthConstraint,
        AntennaHeightLimitConstraint,
        ForbiddenDeviceRolesConstraint,
    ],
    Field(discriminator="mechanism"),
]


class ProfileDocument(BaseModel):
    """v2 envelope including protection/coordination/rf/data/requirements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: str
    kind: str
    metadata: ProfileMetadata
    spectrum: SpectrumSection
    access: AccessSection | None = None
    authorization: AuthorizationSection | None = None
    power: PowerSection | None = None
    geography: GeographySection | None = None
    temporal: TemporalSection | None = None
    protection: ProtectionSection | None = None
    coordination: CoordinationSection | None = None
    rf: RfSection | None = None
    data: DataSection | None = None
    requirements: RequirementsSection | None = None
    constraints: tuple[ProfileConstraint, ...] = ()

    @model_validator(mode="after")
    def _envelope(self) -> ProfileDocument:
        if self.api_version != "spectrum-access/v2":
            raise ValueError("api_version must be 'spectrum-access/v2'")
        if self.kind != "SpectrumProfile":
            raise ValueError("kind must be 'SpectrumProfile'")
        if self.metadata.status not in {"reference", "custom"}:
            raise ValueError("metadata.status must be 'reference' or 'custom'")
        return self
