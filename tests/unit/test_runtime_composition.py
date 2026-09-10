"""Capability derivation and runtime composition tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.device import (
    ADAPTER_API_VERSION,
    AdapterKind,
    MappingDeviceAdapter,
    MappingNetworkAdapter,
)
from adapters.discovery import (
    GROUP_DEVICE_ADAPTERS,
    GROUP_NETWORK_ADAPTERS,
    GROUP_PROTOCOL_ADAPTERS,
    AdapterDiscovery,
)
from adapters.protocol import GenericJsonProtocolAdapter
from primitives.geography import LinearRing
from providers.contract import (
    CAPABILITY_LAND_COVER,
    CAPABILITY_REFERENCE_DATA,
    CAPABILITY_RIGHTS,
    CAPABILITY_TERRAIN,
    DataKind,
    DatasetProvenance,
    MappingFeatureProvider,
    PROVIDER_API_VERSION,
)
from providers.discovery import DataProviderDiscovery
from rf.cbrs_winnforum import free_space_rf_adapter
from rf.discovery import RfModelDiscovery
from profiles import load_profile
from runtime import (
    DeploymentConfig,
    IncompatiblePluginError,
    MissingCapabilityError,
    PluginRegistry,
    PluginSelection,
    UnknownPluginError,
    compose_runtime,
    load_deployment,
    required_capabilities,
)
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH

_PROV = DatasetProvenance(dataset_id="test", dataset_version="1", provider_id="map")
_BUILTIN_IDS = (
    "cbrs_winnforum",
    "br_anatel_slp_3700",
    "eu_elsa",
    "us_tvws_15_711",
)


class _StubTerrain:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.TERRAIN

    def advertised_capabilities(self):
        return frozenset({CAPABILITY_TERRAIN})

    def provenance(self):
        return _PROV

    def fetch(self, *, point=None, token=None):
        raise NotImplementedError


class _StubLandCover:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.LAND_COVER

    def advertised_capabilities(self):
        return frozenset({CAPABILITY_LAND_COVER})

    def provenance(self):
        return _PROV

    def fetch(self, *, point=None, token=None):
        raise NotImplementedError


class _StubRights:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.RIGHTS

    def advertised_capabilities(self):
        return frozenset({CAPABILITY_RIGHTS})

    def provenance(self):
        return _PROV

    def fetch(self, *, point=None, token=None):
        raise NotImplementedError


class _StubReference:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.REFERENCE_DATA

    def advertised_capabilities(self):
        return frozenset({CAPABILITY_REFERENCE_DATA})

    def provenance(self):
        return _PROV

    def fetch(self, *, point=None, token=None):
        raise NotImplementedError


class _AltTerrain:
    """Alternate terrain implementation for Case B."""

    api_version = PROVIDER_API_VERSION
    kind = DataKind.TERRAIN

    def advertised_capabilities(self):
        return frozenset({CAPABILITY_TERRAIN})

    def provenance(self):
        return DatasetProvenance(
            dataset_id="alt", dataset_version="9", provider_id="alt_terrain"
        )

    def fetch(self, *, point=None, token=None):
        raise NotImplementedError


def _ring() -> LinearRing:
    return LinearRing.from_lon_lat([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]])


def _full_data_overlays() -> dict[str, object]:
    ring = _ring()
    return {
        "stub_terrain": _StubTerrain,
        "stub_land_cover": _StubLandCover,
        "stub_protected": lambda: MappingFeatureProvider(
            DataKind.PROTECTED_ENTITIES, (("e1", ring),), _PROV
        ),
        "stub_rights": _StubRights,
        "stub_boundaries": lambda: MappingFeatureProvider(
            DataKind.BOUNDARIES, (("b1", ring),), _PROV
        ),
        "stub_reference": _StubReference,
        "alt_terrain": _AltTerrain,
    }


def _test_registry(*, extra_providers: dict[str, object] | None = None) -> PluginRegistry:
    providers = dict(_full_data_overlays())
    if extra_providers:
        providers.update(extra_providers)
    return PluginRegistry.from_discovery(
        adapters=AdapterDiscovery(
            overlays={
                GROUP_DEVICE_ADAPTERS: {
                    "mapping": MappingDeviceAdapter,
                    "cbsd": MappingDeviceAdapter,
                },
                GROUP_NETWORK_ADAPTERS: {
                    "mapping": MappingNetworkAdapter,
                    "managed": MappingNetworkAdapter,
                },
                GROUP_PROTOCOL_ADAPTERS: {
                    "generic_json": GenericJsonProtocolAdapter,
                    "winnforum_rest": GenericJsonProtocolAdapter,
                },
            },
            list_entry_points=lambda _g: (),
        ),
        providers=DataProviderDiscovery(
            overlays=providers,
            list_entry_points=lambda _g: (),
        ),
        rf_models=RfModelDiscovery(
            overlays={"free_space": free_space_rf_adapter},
            list_entry_points=lambda _g: (),
        ),
    )


def _cbrs_full_deployment(*, terrain_plugin: str = "stub_terrain") -> DeploymentConfig:
    return DeploymentConfig(
        id="cbrs_lab",
        version="1.0.0",
        protocol=PluginSelection(plugin="winnforum_rest"),
        device=PluginSelection(plugin="mapping"),
        rf=PluginSelection(plugin="free_space"),
        providers=(
            PluginSelection(plugin=terrain_plugin),
            PluginSelection(plugin="stub_land_cover"),
            PluginSelection(plugin="stub_protected"),
            PluginSelection(plugin="stub_rights"),
            PluginSelection(plugin="stub_boundaries"),
            PluginSelection(plugin="stub_reference"),
        ),
    )


@pytest.mark.parametrize("profile_id", _BUILTIN_IDS)
def test_required_capabilities_deterministic(profile_id: str):
    profile = load_profile(profile_id)
    a = required_capabilities(profile)
    b = required_capabilities(profile)
    assert a == b
    assert a.as_sorted_tokens() == b.as_sorted_tokens()


def test_cbrs_requirements_include_rf_and_data():
    req = required_capabilities(load_profile("cbrs_winnforum"))
    assert req.rf_required
    assert req.rf_propagation_model == "path_loss"
    assert "terrain" in req.data_capabilities
    assert "geolocation" in req.device_capabilities
    assert not req.network_capabilities


def test_elsa_requirements_are_network_oriented():
    req = required_capabilities(load_profile("eu_elsa"))
    assert not req.rf_required
    assert "managed_area" in req.network_capabilities
    assert not req.device_capabilities


def test_case_a_reference_runtime_with_full_providers():
    profile = load_profile("cbrs_winnforum")
    registry = _test_registry()
    composition = compose_runtime(profile, _cbrs_full_deployment(), registry)
    assert composition.rf is not None
    assert composition.device_adapter is not None
    assert composition.protocol_adapter is not None
    assert composition.provenance.profile_id == "cbrs_winnforum"
    assert composition.provenance.unsatisfied_data_capabilities == ()


def test_case_b_same_profile_alternate_terrain_plugin():
    profile = load_profile("cbrs_winnforum")
    registry = _test_registry()
    a = compose_runtime(profile, _cbrs_full_deployment(terrain_plugin="stub_terrain"), registry)
    b = compose_runtime(profile, _cbrs_full_deployment(terrain_plugin="alt_terrain"), registry)
    assert a.provenance.selected_plugins != b.provenance.selected_plugins
    assert a.rf is not b.rf or a.providers[0] is not b.providers[0]
    assert a.providers[0].provenance().provider_id != b.providers[0].provenance().provider_id


def test_fail_closed_missing_required_plugin():
    profile = load_profile("cbrs_winnforum")
    dep = DeploymentConfig(id="bad", device=PluginSelection(plugin="mapping"))
    with pytest.raises(MissingCapabilityError, match="RF"):
        compose_runtime(profile, dep, _test_registry())


def test_fail_closed_unknown_plugin():
    profile = load_profile("cbrs_winnforum")
    dep = _cbrs_full_deployment()
    dep = dep.model_copy(update={"rf": PluginSelection(plugin="nonexistent_rf")})
    with pytest.raises(UnknownPluginError):
        compose_runtime(profile, dep, _test_registry())


def test_fail_closed_wrong_plugin_category():
    profile = load_profile("cbrs_winnforum")
    # network plugin name is not in the device group
    dep = _cbrs_full_deployment().model_copy(
        update={"device": PluginSelection(plugin="managed")}
    )
    with pytest.raises(UnknownPluginError):
        compose_runtime(profile, dep, _test_registry())


def test_fail_closed_plugin_lacking_capability():
    profile = load_profile("cbrs_winnforum")

    class LeanDevice:
        api_version = ADAPTER_API_VERSION
        kind = AdapterKind.DEVICE

        def advertised_capabilities(self):
            return frozenset({"geolocation", "frequency_range"})

        def to_consumer(self, payload):
            raise NotImplementedError

    registry = PluginRegistry.from_discovery(
        adapters=AdapterDiscovery(
            overlays={
                GROUP_DEVICE_ADAPTERS: {"lean": LeanDevice},
                GROUP_NETWORK_ADAPTERS: {},
                GROUP_PROTOCOL_ADAPTERS: {"generic_json": GenericJsonProtocolAdapter},
            },
            list_entry_points=lambda _g: (),
        ),
        providers=DataProviderDiscovery(
            overlays=_full_data_overlays(),
            list_entry_points=lambda _g: (),
        ),
        rf_models=RfModelDiscovery(
            overlays={"free_space": free_space_rf_adapter},
            list_entry_points=lambda _g: (),
        ),
    )
    dep = _cbrs_full_deployment().model_copy(
        update={
            "device": PluginSelection(plugin="lean"),
            "protocol": PluginSelection(plugin="generic_json"),
        }
    )
    with pytest.raises(IncompatiblePluginError, match="max_eirp"):
        compose_runtime(profile, dep, registry)


def test_fail_closed_missing_mandatory_provider_capability():
    profile = load_profile("cbrs_winnforum")
    dep = DeploymentConfig(
        id="partial",
        device=PluginSelection(plugin="mapping"),
        rf=PluginSelection(plugin="free_space"),
        providers=(PluginSelection(plugin="stub_terrain"),),
    )
    with pytest.raises(MissingCapabilityError, match="data providers"):
        compose_runtime(profile, dep, _test_registry(), require_data_plugins=True)


def test_fail_closed_missing_rf_backend():
    profile = load_profile("cbrs_winnforum")
    dep = DeploymentConfig(
        id="no_rf",
        device=PluginSelection(plugin="mapping"),
        providers=tuple(PluginSelection(plugin=n) for n in (
            "stub_terrain",
            "stub_land_cover",
            "stub_protected",
            "stub_rights",
            "stub_boundaries",
            "stub_reference",
        )),
    )
    with pytest.raises(MissingCapabilityError, match="RF"):
        compose_runtime(profile, dep, _test_registry())


def test_invalid_deployment_rejected_before_compose(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: BAD\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_deployment(bad)


def test_multi_profile_composition_isolation():
    registry = _test_registry()
    cbrs = compose_runtime(
        load_profile("cbrs_winnforum"), _cbrs_full_deployment(), registry
    )
    anatel_dep = DeploymentConfig(
        id="anatel_lab",
        device=PluginSelection(plugin="mapping"),
        providers=(
            PluginSelection(plugin="stub_protected"),
            PluginSelection(plugin="stub_boundaries"),
        ),
    )
    anatel = compose_runtime(
        load_profile("br_anatel_slp_3700"), anatel_dep, registry
    )
    assert cbrs.profile.metadata.id != anatel.profile.metadata.id
    assert cbrs.rf is not None
    assert anatel.rf is None
    assert cbrs.device_adapter is not anatel.device_adapter
    assert cbrs.providers is not anatel.providers
    # Mutating one provenance mapping must not affect the other.
    cbrs_plugins = dict(cbrs.provenance.selected_plugins)
    anatel_plugins = dict(anatel.provenance.selected_plugins)
    cbrs_plugins["device"] = "mutated"
    assert anatel.provenance.selected_plugins["device"] == anatel_plugins["device"]


def test_reference_deployment_composes_cbrs_with_all_data_capabilities():
    """Shipped reference deployment satisfies every required CBRS data capability."""
    profile = load_profile("cbrs_winnforum")
    deployment = load_deployment(DEFAULT_DEPLOYMENT_PATH)
    composition = compose_runtime(
        profile,
        deployment,
        PluginRegistry.from_discovery(),
    )
    assert composition.protocol_adapter is not None
    assert composition.device_adapter is not None
    assert composition.rf is not None
    assert composition.provenance.unsatisfied_data_capabilities == ()
    for cap in (
        "terrain",
        "land_cover",
        "protected_entities",
        "rights",
        "boundaries",
        "reference_data",
    ):
        assert composition.provider_for(cap) is not None


def test_reference_deployment_fails_closed_when_data_provider_removed():
    profile = load_profile("cbrs_winnforum")
    deployment = load_deployment(DEFAULT_DEPLOYMENT_PATH)
    trimmed = deployment.model_copy(
        update={
            "providers": tuple(
                p for p in deployment.providers if p.plugin != "protection_terrain"
            )
        }
    )
    with pytest.raises(MissingCapabilityError):
        compose_runtime(
            profile,
            trimmed,
            PluginRegistry.from_discovery(),
        )
