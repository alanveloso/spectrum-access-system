"""device/network adapter → canonical ConsumerView + capabilities."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.device import (
    ADAPTER_API_VERSION,
    AdapterKind,
    ConsumerAdapter,
    MappingDeviceAdapter,
    MappingNetworkAdapter,
    consumer_meets_requirements,
)
from primitives.request import SpectrumRequest
from primitives.time import UtcInstant

_BANNED = (
    "cbsd",
    "cbrs",
    "pal",
    "gaa",
    "grant",
    "heartbeat",
    "winnforum",
    "fcc",
)


def test_device_adapter_produces_holder_footprint_and_capabilities():
    adapter = MappingDeviceAdapter()
    assert adapter.kind is AdapterKind.DEVICE
    assert adapter.api_version == ADAPTER_API_VERSION
    view = adapter.to_consumer(
        {
            "holder_id": "h1",
            "latitude_deg": 39.0,
            "longitude_deg": -77.0,
            "low_hz": 1000,
            "high_hz": 2000,
            "eirp_dbm": 23.0,
        }
    )
    assert isinstance(adapter, ConsumerAdapter)
    consumer_meets_requirements(
        view, ("geolocation", "frequency_range", "max_eirp")
    )
    request = SpectrumRequest(
        request_id="r1",
        holder_id=view.holder_id,
        footprints=view.footprints,
        requested_at=UtcInstant(datetime(2026, 8, 16, tzinfo=timezone.utc)),
    )
    assert request.holder_id == "h1"
    assert len(request.footprints) == 1


def test_network_adapter_has_no_geolocation_capability():
    adapter = MappingNetworkAdapter()
    assert adapter.kind is AdapterKind.NETWORK
    view = adapter.to_consumer(
        {
            "holder_id": "net-1",
            "ring": [[0, 0], [1, 0], [1, 1], [0, 0]],
            "low_hz": 1000,
            "high_hz": 2000,
            "eirp_dbm": 30.0,
        }
    )
    assert "geolocation" not in view.capabilities
    with pytest.raises(ValueError):
        consumer_meets_requirements(view, ("geolocation",))
    consumer_meets_requirements(view, ("frequency_range", "max_eirp"))


def test_device_adapter_fail_closed_on_incomplete_payload():
    adapter = MappingDeviceAdapter()
    with pytest.raises(ValueError):
        adapter.to_consumer({"holder_id": "h1"})
    with pytest.raises(ValueError):
        adapter.to_consumer(
            {
                "holder_id": " ",
                "latitude_deg": 0,
                "longitude_deg": 0,
                "low_hz": 1,
                "high_hz": 2,
                "eirp_dbm": 1,
            }
        )


def test_adapters_package_has_no_regime_nouns_or_service_imports():
    root = Path(__file__).resolve().parents[2] / "adapters"
    generic = {"device.py", "protocol.py", "discovery.py", "__init__.py"}
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        if path.name in generic:
            for token in _BANNED:
                assert token not in lowered, f"{path.name} contains banned token {token!r}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("services")
                    assert not alias.name.startswith("models")
                    assert not alias.name.startswith("routes")
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("services")
                assert not node.module.startswith("models")
                assert not node.module.startswith("routes")
