"""CBSD device adapter.

Maps WInnForum radio fields onto ConsumerView. Generic primitives never see
CBSD nouns; admission/SIQ policy stays in services until.
"""

from __future__ import annotations

from typing import Mapping

from adapters.device import (
    ADAPTER_API_VERSION,
    AdapterKind,
    ConsumerView,
    DEVICE_CAPABILITY_FREQUENCY_RANGE,
    DEVICE_CAPABILITY_GEOLOCATION,
    DEVICE_CAPABILITY_MAX_EIRP,
)
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint
from primitives.power import PowerDbm
from primitives.request import TransmissionFootprint

_CBRS_DEVICE_BAND = FrequencyRange(low_hz=3_550_000_000, high_hz=3_700_000_000)


def _holder_id(payload: Mapping[str, object]) -> str:
    cbsd_id = payload.get("cbsdId")
    if isinstance(cbsd_id, str) and cbsd_id.strip():
        return cbsd_id
    fcc = payload.get("fccId")
    serial = payload.get("cbsdSerialNumber")
    if isinstance(fcc, str) and fcc.strip() and isinstance(serial, str) and serial.strip():
        return f"{fcc}/{serial}"
    raise ValueError("cbsdId or fccId+cbsdSerialNumber is required")


def _installation(payload: Mapping[str, object]) -> Mapping[str, object]:
    raw = payload.get("installationParam")
    if not isinstance(raw, Mapping):
        raise ValueError("installationParam is required")
    return raw


def _frequency(payload: Mapping[str, object]) -> FrequencyRange:
    op = payload.get("operationParam")
    if isinstance(op, Mapping):
        rng = op.get("operationFrequencyRange")
        if isinstance(rng, Mapping):
            low = rng.get("lowFrequency")
            high = rng.get("highFrequency")
            if isinstance(low, int) and isinstance(high, int):
                return FrequencyRange(low_hz=low, high_hz=high)
    inquired = payload.get("inquiredSpectrum")
    if isinstance(inquired, list) and inquired and isinstance(inquired[0], Mapping):
        low = inquired[0].get("lowFrequency")
        high = inquired[0].get("highFrequency")
        if isinstance(low, int) and isinstance(high, int):
            return FrequencyRange(low_hz=low, high_hz=high)
    # Registration / stored snapshot without grant operationParam.
    return _CBRS_DEVICE_BAND


def _eirp_dbm(payload: Mapping[str, object]) -> float:
    op = payload.get("operationParam")
    if isinstance(op, Mapping):
        eirp = op.get("maxEirp")
        if isinstance(eirp, (int, float)):
            return float(eirp)
    install = payload.get("installationParam")
    if isinstance(install, Mapping):
        cap = install.get("eirpCapability")
        if isinstance(cap, (int, float)):
            return float(cap)
    # Registration snapshots may omit declared capability; use category ceiling.
    cat = str(payload.get("cbsdCategory") or "A").upper()
    return 47.0 if cat == "B" else 30.0


class CbsdDeviceAdapter:
    """Device adapter for a CBSD radio snapshot. Not a protocol codec."""

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
        install = _installation(payload)
        lat = install.get("latitude")
        lon = install.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ValueError("installationParam.latitude and longitude are required")
        footprint = TransmissionFootprint(
            frequency=_frequency(payload),
            power=PowerDbm(_eirp_dbm(payload)),
            location=GeoPoint(latitude_deg=float(lat), longitude_deg=float(lon)),
        )
        return ConsumerView(
            holder_id=_holder_id(payload),
            capabilities=self.advertised_capabilities(),
            footprints=(footprint,),
        )


def cbsd_device_adapter() -> CbsdDeviceAdapter:
    return CbsdDeviceAdapter()
