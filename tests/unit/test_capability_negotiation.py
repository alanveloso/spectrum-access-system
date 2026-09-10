"""profile negotiates capabilities, not plugin names."""

from __future__ import annotations

import pytest

from adapters.device import MappingDeviceAdapter, MappingNetworkAdapter
from adapters.discovery import GROUP_DEVICE_ADAPTERS, GROUP_NETWORK_ADAPTERS, AdapterDiscovery
from primitives.geography import LinearRing
from providers.contract import (
    DataKind,
    DatasetProvenance,
    MappingFeatureProvider,
    MappingTerrainProvider,
)
from rf.cbrs_winnforum import CbrsWinnForumRfAdapter
from profiles.negotiate import (
    adapters_satisfying_device_capabilities,
    negotiate_profile_plugins,
)
from profiles.parse import parse_profile_document

_PROV = DatasetProvenance(dataset_id="mem", dataset_version="1", provider_id="map")


def _rf_profile() -> dict:
    return {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "example", "version": "1.0.0", "status": "custom"},
        "spectrum": {"ranges": [{"id": "main", "low_hz": 1000, "high_hz": 2000}]},
        "rf": {
            "required": True,
            "policy": "path_loss_plus_aggregate",
            "propagation_model": "path_loss",
        },
        "data": {"required_capabilities": ["terrain", "protected_entities"]},
        "requirements": {
            "device_capabilities": ["geolocation", "frequency_range", "max_eirp"]
        },
    }


def test_device_adapter_and_providers_and_rf_port_satisfy_profile():
    profile = parse_profile_document(_rf_profile())
    device = MappingDeviceAdapter()
    view = device.to_consumer(
        {
            "holder_id": "h1",
            "latitude_deg": 39.0,
            "longitude_deg": -77.0,
            "low_hz": 1000,
            "high_hz": 2000,
            "eirp_dbm": 23.0,
        }
    )
    ring = LinearRing.from_lon_lat([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])
    providers = (
        MappingTerrainProvider({(0.0, 0.0): 1.0}, _PROV),
        MappingFeatureProvider(DataKind.PROTECTED_ENTITIES, (("e1", ring),), _PROV),
    )
    negotiate_profile_plugins(
        profile,
        consumer=view,
        consumer_adapter=device,
        providers=providers,
        rf_port=CbrsWinnForumRfAdapter(backend="free_space"),
    )


def test_network_adapter_fails_geolocation_requirement_same_profile():
    profile = parse_profile_document(_rf_profile())
    with pytest.raises(ValueError, match="missing required capabilities"):
        negotiate_profile_plugins(
            profile,
            consumer_adapter=MappingNetworkAdapter(),
            providers=(
                MappingTerrainProvider({(0.0, 0.0): 1.0}, _PROV),
                MappingFeatureProvider(
                    DataKind.PROTECTED_ENTITIES,
                    (("e1", LinearRing.from_lon_lat([[0, 0], [1, 0], [1, 1], [0, 0]])),),
                    _PROV,
                ),
            ),
            rf_port=CbrsWinnForumRfAdapter(backend="free_space"),
        )


def test_missing_rf_port_or_terrain_provider_fail_closed():
    profile = parse_profile_document(_rf_profile())
    device = MappingDeviceAdapter()
    ring = LinearRing.from_lon_lat([[0, 0], [1, 0], [1, 1], [0, 0]])
    providers = (
        MappingTerrainProvider({(0.0, 0.0): 1.0}, _PROV),
        MappingFeatureProvider(DataKind.PROTECTED_ENTITIES, (("e1", ring),), _PROV),
    )
    with pytest.raises(ValueError, match="no RF port"):
        negotiate_profile_plugins(profile, consumer_adapter=device, providers=providers)
    with pytest.raises(ValueError, match="missing required"):
        negotiate_profile_plugins(
            profile,
            consumer_adapter=device,
            providers=(),
            rf_port=CbrsWinnForumRfAdapter(backend="free_space"),
        )


def test_discovery_filters_by_capability_not_profile_plugin_names():
    profile = parse_profile_document(_rf_profile())
    required = profile.requirements.device_capabilities
    discovery = AdapterDiscovery(
        overlays={
            GROUP_DEVICE_ADAPTERS: {"alpha": MappingDeviceAdapter},
            GROUP_NETWORK_ADAPTERS: {"beta": MappingNetworkAdapter},
        },
        list_entry_points=lambda _g: (),
    )
    device_hits = adapters_satisfying_device_capabilities(
        discovery, GROUP_DEVICE_ADAPTERS, required
    )
    assert device_hits == ("alpha",)
    with pytest.raises(ValueError, match="no installed adapter"):
        adapters_satisfying_device_capabilities(
            discovery, GROUP_NETWORK_ADAPTERS, required
        )
