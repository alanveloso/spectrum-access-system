"""Device/Network adapter contract.

External radios or networks become an authorization holder plus footprints.
Protocol translation is out of scope. Discovery is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing
from primitives.power import PowerDbm
from primitives.request import TransmissionFootprint

ADAPTER_API_VERSION = "1.0.0"

DEVICE_CAPABILITY_GEOLOCATION = "geolocation"
DEVICE_CAPABILITY_FREQUENCY_RANGE = "frequency_range"
DEVICE_CAPABILITY_MAX_EIRP = "max_eirp"

# Network/managed-consumer capabilities. Shared tokens with device where
# frequency/power apply; never imply per-radio geolocation.
NETWORK_CAPABILITY_MANAGED_AREA = "managed_area"
NETWORK_CAPABILITY_NETWORK_IDENTITY = "network_identity"


class AdapterKind(StrEnum):
    DEVICE = "device"
    NETWORK = "network"


@dataclass(frozen=True, slots=True)
class ConsumerView:
    """Canonical holder: opaque id, advertised capabilities, transmission footprints."""

    holder_id: str
    capabilities: frozenset[str]
    footprints: tuple[TransmissionFootprint, ...]

    def __post_init__(self) -> None:
        if not self.holder_id.strip():
            raise ValueError("holder_id is required")
        if any(not cap.strip() for cap in self.capabilities):
            raise ValueError("capabilities must be non-empty tokens")
        if DEVICE_CAPABILITY_GEOLOCATION in self.capabilities and not self.footprints:
            raise ValueError("geolocation capability requires at least one footprint")
        if NETWORK_CAPABILITY_MANAGED_AREA in self.capabilities:
            if not self.footprints:
                raise ValueError("managed_area capability requires at least one footprint")
            for footprint in self.footprints:
                if not isinstance(footprint.location, LinearRing):
                    raise ValueError(
                        "managed_area capability requires LinearRing footprints"
                    )


def consumer_meets_requirements(
    view: ConsumerView, required: tuple[str, ...]
) -> None:
    missing = [cap for cap in required if cap not in view.capabilities]
    if missing:
        raise ValueError(f"consumer missing required capabilities: {missing}")


@runtime_checkable
class ConsumerAdapter(Protocol):
    """Maps an external payload to ConsumerView. Device and network share this shape."""

    @property
    def api_version(self) -> str: ...

    @property
    def kind(self) -> AdapterKind: ...

    def advertised_capabilities(self) -> frozenset[str]: ...

    def to_consumer(self, payload: Mapping[str, object]) -> ConsumerView: ...


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


class MappingDeviceAdapter:
    """Test/reference device adapter: dict → ConsumerView. Not a protocol codec."""

    api_version = ADAPTER_API_VERSION
    kind = AdapterKind.DEVICE

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                DEVICE_CAPABILITY_GEOLOCATION,
                DEVICE_CAPABILITY_FREQUENCY_RANGE,
                DEVICE_CAPABILITY_MAX_EIRP,
            }
        )

    def to_consumer(self, payload: Mapping[str, object]) -> ConsumerView:
        holder_id = _require_str(payload, "holder_id")
        lat = payload.get("latitude_deg")
        lon = payload.get("longitude_deg")
        low = payload.get("low_hz")
        high = payload.get("high_hz")
        eirp = payload.get("eirp_dbm")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ValueError("latitude_deg and longitude_deg are required")
        if not isinstance(low, int) or not isinstance(high, int):
            raise ValueError("low_hz and high_hz are required")
        if not isinstance(eirp, (int, float)):
            raise ValueError("eirp_dbm is required")
        footprint = TransmissionFootprint(
            frequency=FrequencyRange(low_hz=low, high_hz=high),
            power=PowerDbm(float(eirp)),
            location=GeoPoint(latitude_deg=float(lat), longitude_deg=float(lon)),
        )
        return ConsumerView(
            holder_id=holder_id,
            capabilities=self.advertised_capabilities(),
            footprints=(footprint,),
        )


class MappingNetworkAdapter:
    """Test/reference network adapter: holder + area ring, no per-radio point."""

    api_version = ADAPTER_API_VERSION
    kind = AdapterKind.NETWORK

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                NETWORK_CAPABILITY_MANAGED_AREA,
                DEVICE_CAPABILITY_FREQUENCY_RANGE,
                DEVICE_CAPABILITY_MAX_EIRP,
            }
        )

    def to_consumer(self, payload: Mapping[str, object]) -> ConsumerView:
        holder_id = _require_str(payload, "holder_id")
        ring = payload.get("ring")
        low = payload.get("low_hz")
        high = payload.get("high_hz")
        eirp = payload.get("eirp_dbm")
        if not isinstance(ring, (list, tuple)):
            raise ValueError("ring is required")
        if not isinstance(low, int) or not isinstance(high, int):
            raise ValueError("low_hz and high_hz are required")
        if not isinstance(eirp, (int, float)):
            raise ValueError("eirp_dbm is required")
        footprint = TransmissionFootprint(
            frequency=FrequencyRange(low_hz=low, high_hz=high),
            power=PowerDbm(float(eirp)),
            location=LinearRing.from_lon_lat(ring),
        )
        return ConsumerView(
            holder_id=holder_id,
            capabilities=self.advertised_capabilities(),
            footprints=(footprint,),
        )
