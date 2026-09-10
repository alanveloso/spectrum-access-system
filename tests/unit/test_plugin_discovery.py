"""discover device/network/protocol adapters without editing core."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from adapters.device import AdapterKind, MappingDeviceAdapter, MappingNetworkAdapter
from adapters.discovery import (
    GROUP_DATA_PROVIDERS,
    GROUP_DEVICE_ADAPTERS,
    GROUP_NETWORK_ADAPTERS,
    GROUP_PROTOCOL_ADAPTERS,
    AdapterDiscovery,
)
from adapters.protocol import GenericJsonProtocolAdapter

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


class _Ep:
    def __init__(self, name: str, factory: type) -> None:
        self.name = name
        self._factory = factory

    def load(self) -> type:
        return self._factory


def test_overlay_registers_plugin_without_changing_discovery_module():
    discovery = AdapterDiscovery(
        overlays={
            GROUP_DEVICE_ADAPTERS: {"mapping": MappingDeviceAdapter},
            GROUP_NETWORK_ADAPTERS: {"mapping": MappingNetworkAdapter},
            GROUP_PROTOCOL_ADAPTERS: {"generic_json": GenericJsonProtocolAdapter},
        },
        list_entry_points=lambda _group: (),
    )
    device = discovery.load(GROUP_DEVICE_ADAPTERS, "mapping")
    network = discovery.load(GROUP_NETWORK_ADAPTERS, "mapping")
    protocol = discovery.load(GROUP_PROTOCOL_ADAPTERS, "generic_json")
    assert device.kind is AdapterKind.DEVICE
    assert network.kind is AdapterKind.NETWORK
    assert protocol.protocol_id == "generic-json"
    assert discovery.names(GROUP_DEVICE_ADAPTERS) == frozenset({"mapping"})


def test_entry_point_loads_third_party_class():
    discovery = AdapterDiscovery(
        list_entry_points=lambda group: (
            (_Ep("ext", MappingDeviceAdapter),) if group == GROUP_DEVICE_ADAPTERS else ()
        )
    )
    loaded = discovery.load(GROUP_DEVICE_ADAPTERS, "ext")
    assert loaded.kind is AdapterKind.DEVICE


def test_duplicate_name_wrong_kind_and_unknown_group_fail_closed():
    discovery = AdapterDiscovery(
        overlays={GROUP_DEVICE_ADAPTERS: {"mapping": MappingDeviceAdapter}},
        list_entry_points=lambda group: (
            (_Ep("mapping", MappingDeviceAdapter),) if group == GROUP_DEVICE_ADAPTERS else ()
        ),
    )
    with pytest.raises(ValueError, match="duplicate"):
        discovery.names(GROUP_DEVICE_ADAPTERS)

    kind_mismatch = AdapterDiscovery(
        overlays={GROUP_DEVICE_ADAPTERS: {"net": MappingNetworkAdapter}},
        list_entry_points=lambda _group: (),
    )
    with pytest.raises(ValueError, match="kind"):
        kind_mismatch.load(GROUP_DEVICE_ADAPTERS, "net")

    empty = AdapterDiscovery(list_entry_points=lambda _group: ())
    with pytest.raises(ValueError, match="unsupported adapter group"):
        empty.names(GROUP_DATA_PROVIDERS)
    with pytest.raises(ValueError, match="unknown plugin"):
        empty.load(GROUP_PROTOCOL_ADAPTERS, "missing")


def test_incompatible_api_version_fail_closed():
    class OldDevice(MappingDeviceAdapter):
        api_version = "0.0.1"

    discovery = AdapterDiscovery(
        overlays={GROUP_DEVICE_ADAPTERS: {"old": OldDevice}},
        list_entry_points=lambda _group: (),
    )
    with pytest.raises(ValueError, match="api_version"):
        discovery.load(GROUP_DEVICE_ADAPTERS, "old")


def test_discovery_module_has_no_regime_nouns_or_service_imports():
    path = Path(__file__).resolve().parents[2] / "adapters" / "discovery.py"
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()
    for token in _BANNED:
        assert token not in lowered, f"discovery.py contains banned token {token!r}"
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
