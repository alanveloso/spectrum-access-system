"""Managed network / managed-consumer adapter.

Maps a network authorization holder (e.g. eLSA VSP/MFCN identity) onto
ConsumerView with area footprints. Explicitly rejects CBSD/Grant payload shapes
so networks are never modeled as fake CBSDs.

Protocol eLSA1 codecs: ``adapters.elsa1`` ( vertical slice).
"""

from __future__ import annotations

from typing import Mapping, Sequence

from adapters.device import (
    ADAPTER_API_VERSION,
    AdapterKind,
    ConsumerView,
    DEVICE_CAPABILITY_FREQUENCY_RANGE,
    DEVICE_CAPABILITY_MAX_EIRP,
    NETWORK_CAPABILITY_MANAGED_AREA,
    NETWORK_CAPABILITY_NETWORK_IDENTITY,
)
from primitives.frequency import FrequencyRange
from primitives.geography import LinearRing
from primitives.power import PowerDbm
from primitives.request import TransmissionFootprint

# WInnForum / CBSD vocabulary — refuse rather than coerce into a network consumer.
_REJECTED_CBSD_KEYS = frozenset(
    {
        "cbsdId",
        "fccId",
        "cbsdSerialNumber",
        "grantId",
        "installationParam",
        "cbsdInfo",
    }
)


def _reject_cbsd_shaped_payload(payload: Mapping[str, object]) -> None:
    present = sorted(key for key in _REJECTED_CBSD_KEYS if key in payload)
    if present:
        raise ValueError(
            "managed network adapter rejects CBSD/Grant payload keys: "
            + ", ".join(present)
        )


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _holder_id(payload: Mapping[str, object]) -> str:
    network_id = _require_str(payload, "network_id")
    vsp = payload.get("vsp_id")
    if isinstance(vsp, str) and vsp.strip():
        return f"{vsp.strip()}/{network_id}"
    return network_id


def _parse_footprint(raw: Mapping[str, object]) -> TransmissionFootprint:
    ring = raw.get("ring")
    low = raw.get("low_hz")
    high = raw.get("high_hz")
    eirp = raw.get("eirp_dbm")
    if not isinstance(ring, (list, tuple)):
        raise ValueError("footprint.ring is required")
    if not isinstance(low, int) or not isinstance(high, int):
        raise ValueError("footprint low_hz and high_hz are required integers")
    if not isinstance(eirp, (int, float)):
        raise ValueError("footprint eirp_dbm is required")
    return TransmissionFootprint(
        frequency=FrequencyRange(low_hz=low, high_hz=high),
        power=PowerDbm(float(eirp)),
        location=LinearRing.from_lon_lat(ring),
    )


def _footprints(payload: Mapping[str, object]) -> tuple[TransmissionFootprint, ...]:
    raw_list = payload.get("footprints")
    if isinstance(raw_list, Sequence) and not isinstance(raw_list, (str, bytes)):
        if not raw_list:
            raise ValueError("footprints must be a non-empty list")
        out: list[TransmissionFootprint] = []
        for index, item in enumerate(raw_list):
            if not isinstance(item, Mapping):
                raise ValueError(f"footprints[{index}] must be a mapping")
            out.append(_parse_footprint(item))
        return tuple(out)
 # Single-area shorthand (same fields as MappingNetworkAdapter ring form).
    if "ring" in payload:
        return (_parse_footprint(payload),)
    raise ValueError("footprints list or ring shorthand is required")


class ManagedNetworkAdapter:
    """Canonical managed-consumer → ConsumerView. kind=network, area footprints."""

    api_version = ADAPTER_API_VERSION
    kind = AdapterKind.NETWORK

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                NETWORK_CAPABILITY_MANAGED_AREA,
                NETWORK_CAPABILITY_NETWORK_IDENTITY,
                DEVICE_CAPABILITY_FREQUENCY_RANGE,
                DEVICE_CAPABILITY_MAX_EIRP,
            }
        )

    def to_consumer(self, payload: Mapping[str, object]) -> ConsumerView:
        _reject_cbsd_shaped_payload(payload)
        return ConsumerView(
            holder_id=_holder_id(payload),
            capabilities=self.advertised_capabilities(),
            footprints=_footprints(payload),
        )


def managed_network_adapter() -> ManagedNetworkAdapter:
    """Entry-point factory for spectrum_access.network_adapters."""
    return ManagedNetworkAdapter()
