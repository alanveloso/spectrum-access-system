"""Access composed device/network adapters without rediscovery."""

from __future__ import annotations

from typing import Mapping

from adapters.device import ConsumerAdapter, ConsumerView
from runtime.composition import RuntimeComposition
from runtime.context import lookup_runtime_composition
from runtime.errors import RuntimeCompositionError


class DeviceAdapterNotBoundError(RuntimeCompositionError):
    """No device adapter is bound for the current execution scope."""


def require_composed_device_adapter(
    composition: RuntimeComposition | None = None,
) -> ConsumerAdapter:
    active = lookup_runtime_composition(composition)
    if active is None or active.device_adapter is None:
        raise DeviceAdapterNotBoundError(
            "RuntimeComposition has no device adapter; profile/deployment must select one"
        )
    return active.device_adapter


def adapt_device_payload(
    payload: Mapping[str, object],
    *,
    composition: RuntimeComposition | None = None,
) -> ConsumerView:
    """Translate an external device snapshot through the composed DeviceAdapter."""
    return require_composed_device_adapter(composition).to_consumer(payload)
