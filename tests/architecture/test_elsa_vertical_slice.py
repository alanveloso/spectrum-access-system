"""eLSA second-regime vertical slice through generic composition.

Proves one existing eLSA1 procedure executes via Profile + DeploymentConfig +
RuntimeComposition + PluginRegistry + NetworkAdapter + ProtocolAdapter without
profile-id dispatch in the generic core.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.device import AdapterKind, ConsumerAdapter
from adapters.protocol import DomainOperation, ProtocolAdapter, ProtocolInbound
from primitives.availability import AvailabilityConstraint, AvailabilityZoneKind
from primitives.geography import LinearRing
from primitives.request import SpectrumRequest
from profiles import load_profile
from profiles.trust import builtin_profiles_dir
from runtime import PluginRegistry, compose_runtime
from runtime.capabilities import required_capabilities
from runtime.deployment import DeploymentConfig, PluginSelection

_RING = [
    [2.3000, 48.8500],
    [2.3100, 48.8500],
    [2.3100, 48.8600],
    [2.3000, 48.8600],
    [2.3000, 48.8500],
]

_PLUGIN_IMPLEMENTATION_TOKENS = (
    "elsa1",
    "ManagedNetworkAdapter",
    "Elsa1ProtocolAdapter",
    "adapters.elsa1",
    "adapters.managed_consumer",
    "plugin:",
    "entry_point",
    "protocol_adapter",
)


def _elsa_deployment() -> DeploymentConfig:
    return DeploymentConfig(
        id="elsa_vertical_slice",
        version="1.0.0",
        protocol=PluginSelection(plugin="elsa1"),
        network=PluginSelection(plugin="managed"),
        providers=(
            PluginSelection(plugin="bundle_protected_entities"),
            PluginSelection(plugin="bundle_boundaries"),
        ),
    )


def _consumer_payload() -> dict[str, object]:
    return {
        "network_id": "mfcn-1",
        "vsp_id": "vsp-a",
        "ring": _RING,
        "low_hz": 2_300_000_000,
        "high_hz": 2_310_000_000,
        "eirp_dbm": 30.0,
    }


def _elsrai_notification_envelope() -> dict[str, object]:
    return {
        "procedure": "elsraiNotification",
        "transaction_id": "tx-elsa-slice",
        "requested_at": "2026-08-20T12:00:00+00:00",
        "consumer": _consumer_payload(),
        "elsrai": {
            "zones": [
                {
                    "id": "zone-allow-1",
                    "kind": "allowance",
                    "low_hz": 2_300_000_000,
                    "high_hz": 2_320_000_000,
                    "ring": _RING,
                    "max_eirp_dbm": 30.0,
                    "validity_start": "2026-08-20T12:00:00+00:00",
                    "validity_end": "2026-08-20T18:00:00+00:00",
                    "mode": "scheduled",
                    "source_id": "incumbent-x",
                }
            ],
            "event_kind": "updated",
            "event_id": "ev-1",
            "observed_at": "2026-08-20T12:00:01+00:00",
        },
    }


def test_eu_elsa_vertical_slice_through_generic_composition() -> None:
    profile_path = Path(builtin_profiles_dir()) / "eu_elsa.yaml"
    profile_yaml = profile_path.read_text(encoding="utf-8")
    for token in _PLUGIN_IMPLEMENTATION_TOKENS:
        assert token not in profile_yaml

    profile = load_profile("eu_elsa")
    assert profile.metadata.id == "eu_elsa"

    req = required_capabilities(profile)
    assert req.network_capabilities == frozenset(
        {"managed_area", "network_identity", "frequency_range", "max_eirp"}
    )
    assert not req.device_capabilities
    assert req.data_capabilities == frozenset({"protected_entities", "boundaries"})
    assert not req.rf_required

    composition = compose_runtime(
        profile, _elsa_deployment(), PluginRegistry.from_discovery()
    )
    protocol = composition.protocol_adapter
    network = composition.network_adapter
    assert isinstance(protocol, ProtocolAdapter)
    assert isinstance(network, ConsumerAdapter)
    assert network.kind is AdapterKind.NETWORK
    assert composition.device_adapter is None
    assert composition.rf is None
    assert composition.provenance.selected_plugins["protocol"] == "elsa1"
    assert composition.provenance.selected_plugins["network"] == "managed"
    assert composition.provenance.unsatisfied_data_capabilities == ()

    inbound = protocol.decode(_elsrai_notification_envelope(), network)
    assert type(inbound) is ProtocolInbound
    assert inbound.operation is DomainOperation.APPLY_AVAILABILITY
    assert type(inbound.request) is SpectrumRequest
    assert inbound.request.holder_id == "vsp-a/mfcn-1"
    assert inbound.request.footprints
    assert isinstance(inbound.request.footprints[0].location, LinearRing)
    assert len(inbound.availability_constraints) == 1
    constraint = inbound.availability_constraints[0]
    assert type(constraint) is AvailabilityConstraint
    assert constraint.zone_kind is AvailabilityZoneKind.ALLOWANCE
    assert constraint.source_id == "incumbent-x"

    with pytest.raises(ValueError, match="rejects CBSD"):
        network.to_consumer({**_consumer_payload(), "cbsdId": "cbsd-1"})
    with pytest.raises(ValueError, match="rejects CBSD"):
        protocol.decode(
            {**_elsrai_notification_envelope(), "cbsdId": "cbsd-1"},
            network,
        )
