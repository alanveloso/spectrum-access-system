"""managed network / managed-consumer canonical representation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adapters.device import (
    AdapterKind,
    ConsumerAdapter,
    NETWORK_CAPABILITY_MANAGED_AREA,
    NETWORK_CAPABILITY_NETWORK_IDENTITY,
    consumer_meets_requirements,
)
from adapters.discovery import GROUP_NETWORK_ADAPTERS, AdapterDiscovery
from adapters.managed_consumer import ManagedNetworkAdapter, managed_network_adapter
from primitives.geography import LinearRing
from primitives.request import SpectrumRequest
from primitives.time import UtcInstant
from profiles.negotiate import (
    adapters_satisfying_network_capabilities,
    negotiate_profile_plugins,
)
from profiles.schema import ProfileDocument


def _ring() -> list[list[float]]:
    return [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]


def test_managed_network_adapter_produces_area_consumer_not_geolocation() -> None:
    adapter = managed_network_adapter()
    assert isinstance(adapter, ConsumerAdapter)
    assert adapter.kind is AdapterKind.NETWORK
    view = adapter.to_consumer(
        {
            "network_id": "mfcn-42",
            "vsp_id": "vsp-a",
            "ring": _ring(),
            "low_hz": 3_700_000_000,
            "high_hz": 3_710_000_000,
            "eirp_dbm": 30.0,
        }
    )
    assert view.holder_id == "vsp-a/mfcn-42"
    assert NETWORK_CAPABILITY_MANAGED_AREA in view.capabilities
    assert NETWORK_CAPABILITY_NETWORK_IDENTITY in view.capabilities
    assert "geolocation" not in view.capabilities
    assert len(view.footprints) == 1
    assert isinstance(view.footprints[0].location, LinearRing)
    consumer_meets_requirements(
        view,
        ("managed_area", "network_identity", "frequency_range", "max_eirp"),
    )
    request = SpectrumRequest(
        request_id="r-net",
        holder_id=view.holder_id,
        footprints=view.footprints,
        requested_at=UtcInstant(datetime(2026, 8, 20, tzinfo=timezone.utc)),
    )
    assert request.holder_id == "vsp-a/mfcn-42"


def test_managed_network_rejects_cbsd_shaped_payload() -> None:
    adapter = ManagedNetworkAdapter()
    with pytest.raises(ValueError, match="rejects CBSD"):
        adapter.to_consumer(
            {
                "network_id": "n1",
                "cbsdId": "cbsd-1",
                "ring": _ring(),
                "low_hz": 1,
                "high_hz": 2,
                "eirp_dbm": 1.0,
            }
        )
    with pytest.raises(ValueError, match="rejects CBSD"):
        adapter.to_consumer(
            {
                "network_id": "n1",
                "grantId": "g1",
                "ring": _ring(),
                "low_hz": 1,
                "high_hz": 2,
                "eirp_dbm": 1.0,
            }
        )


def test_managed_network_multi_footprint_and_fail_closed() -> None:
    adapter = ManagedNetworkAdapter()
    view = adapter.to_consumer(
        {
            "network_id": "net-1",
            "footprints": [
                {
                    "ring": _ring(),
                    "low_hz": 1000,
                    "high_hz": 2000,
                    "eirp_dbm": 20.0,
                },
                {
                    "ring": [[2, 2], [3, 2], [3, 3], [2, 2]],
                    "low_hz": 2000,
                    "high_hz": 3000,
                    "eirp_dbm": 18.0,
                },
            ],
        }
    )
    assert len(view.footprints) == 2
    with pytest.raises(ValueError):
        adapter.to_consumer({"network_id": "net-1"})
    with pytest.raises(ValueError):
        adapter.to_consumer(
            {
                "network_id": " ",
                "ring": _ring(),
                "low_hz": 1,
                "high_hz": 2,
                "eirp_dbm": 1,
            }
        )


def test_profile_network_capabilities_negotiate_and_discover() -> None:
    doc = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "eu_elsa_probe", "version": "0.0.1", "status": "custom"},
        "spectrum": {
            "ranges": [{"id": "main", "low_hz": 2300000000, "high_hz": 2400000000}]
        },
        "requirements": {
            "network_capabilities": [
                "managed_area",
                "network_identity",
                "frequency_range",
                "max_eirp",
            ]
        },
        "rf": {"required": False, "policy": "path_loss"},
    }
    profile = ProfileDocument.model_validate(doc)
    adapter = ManagedNetworkAdapter()
    view = adapter.to_consumer(
        {
            "network_id": "n1",
            "ring": _ring(),
            "low_hz": 2300000000,
            "high_hz": 2310000000,
            "eirp_dbm": 23.0,
        }
    )
    negotiate_profile_plugins(
        profile, consumer=view, consumer_adapter=adapter
    )
    discovery = AdapterDiscovery(
        overlays={GROUP_NETWORK_ADAPTERS: {"managed": ManagedNetworkAdapter}},
        list_entry_points=lambda _g: (),
    )
    hits = adapters_satisfying_network_capabilities(
        discovery,
        GROUP_NETWORK_ADAPTERS,
        profile.requirements.network_capabilities,
    )
    assert "managed" in hits


def test_device_adapter_does_not_satisfy_network_kind_requirement() -> None:
    from adapters.device import MappingDeviceAdapter

    doc = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "net_only", "version": "0.0.1", "status": "custom"},
        "spectrum": {
            "ranges": [{"id": "main", "low_hz": 1, "high_hz": 2}]
        },
        "requirements": {"network_capabilities": ["managed_area"]},
        "rf": {"required": False, "policy": "path_loss"},
    }
    profile = ProfileDocument.model_validate(doc)
    with pytest.raises(ValueError, match="kind is not network"):
        negotiate_profile_plugins(
            profile, consumer_adapter=MappingDeviceAdapter()
        )


def test_schema_rejects_geolocation_as_network_capability() -> None:
    doc = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "bad", "version": "0.0.1", "status": "custom"},
        "spectrum": {
            "ranges": [{"id": "main", "low_hz": 1, "high_hz": 2}]
        },
        "requirements": {"network_capabilities": ["geolocation"]},
        "rf": {"required": False, "policy": "path_loss"},
    }
    with pytest.raises(Exception):
        ProfileDocument.model_validate(doc)
