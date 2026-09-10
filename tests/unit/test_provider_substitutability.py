"""Provider Lego proof: boundaries capability substitutable via DeploymentConfig only."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest

from profiles import load_profile
from providers.contract import TOKEN_US_CANADA_BORDER
from providers.discovery import DataProviderDiscovery
from runtime import PluginRegistry, UnknownPluginError, compose_runtime, load_deployment
from runtime.context import runtime_composition_scope
from runtime.deployment import DeploymentConfig, PluginSelection
from runtime.errors import MissingCapabilityError
from services import border_geometry
from tests.fixtures.plugins.alternate_boundaries import (
    LEGO_BOUNDARY_VERTICES,
    MappingBoundariesProvider,
)

REPO = Path(__file__).resolve().parents[2]
DEPLOYMENT_A = REPO / "tests" / "fixtures" / "deployments" / "boundaries_provider_a.yaml"
DEPLOYMENT_B = REPO / "tests" / "fixtures" / "deployments" / "boundaries_provider_b.yaml"
PROFILE = load_profile("cbrs_winnforum")


def _registry_from_entry_points() -> PluginRegistry:
    return PluginRegistry.from_discovery()


def _compose(deployment_path: Path):
    return compose_runtime(
        PROFILE,
        load_deployment(deployment_path),
        _registry_from_entry_points(),
        require_data_plugins=False,
    )


def test_boundaries_deployments_select_different_plugins():
    dep_a = load_deployment(DEPLOYMENT_A)
    dep_b = load_deployment(DEPLOYMENT_B)
    assert dep_a.providers[0].plugin == "protection_boundaries"
    assert dep_b.providers[0].plugin == "mapping_boundaries"
    assert dep_a.protocol == dep_b.protocol
    assert dep_a.device == dep_b.device
    assert dep_a.rf == dep_b.rf


def test_boundaries_provider_for_resolves_via_discovery():
    runtime_a = _compose(DEPLOYMENT_A)
    runtime_b = _compose(DEPLOYMENT_B)
    provider_a = runtime_a.provider_for("boundaries")
    provider_b = runtime_b.provider_for("boundaries")
    assert provider_a is not provider_b
    assert runtime_a.provenance.resolved["data.boundaries"] == "protection_boundaries"
    assert runtime_b.provenance.resolved["data.boundaries"] == "mapping_boundaries"
    assert runtime_a.profile.metadata.id == runtime_b.profile.metadata.id


def test_boundaries_lego_consumer_follows_composed_provider():
    runtime_a = _compose(DEPLOYMENT_A)
    runtime_b = _compose(DEPLOYMENT_B)

    border_geometry.reset_border_geometry_cache()
    with runtime_composition_scope(runtime_a):
        key_a, vertices_a = border_geometry._vertices_from_composed_provider()  # noqa: SLF001
    border_geometry.reset_border_geometry_cache()
    with runtime_composition_scope(runtime_b):
        key_b, vertices_b = border_geometry._vertices_from_composed_provider()  # noqa: SLF001
    border_geometry.reset_border_geometry_cache()

    assert key_a != key_b
    assert vertices_a != vertices_b
    assert list(LEGO_BOUNDARY_VERTICES) == vertices_b
    assert "mapping_boundaries" in key_b


def test_boundaries_lego_legacy_filesystem_not_third_authority(tmp_path: Path):
    corrupt = tmp_path / "uscabdry_sampled.kmz"
    corrupt.write_bytes(b"not-a-valid-kmz")
    runtime_b = _compose(DEPLOYMENT_B)
    border_geometry.reset_border_geometry_cache()
    with runtime_composition_scope(runtime_b):
        _key, vertices = border_geometry._vertices_from_composed_provider()  # noqa: SLF001
        assert vertices == list(LEGO_BOUNDARY_VERTICES)
        with pytest.raises(border_geometry.BorderGeometryUnavailable):
            border_geometry.border_vertices(kmz_path=corrupt)
    border_geometry.reset_border_geometry_cache()


def test_mapping_boundaries_plugin_absent_fails_closed():
    limited = PluginRegistry.from_discovery(
        providers=DataProviderDiscovery(
            overlays={},
            list_entry_points=lambda group: [
                ep
                for ep in entry_points(group=group)
                if ep.name != "mapping_boundaries"
            ],
        ),
    )
    with pytest.raises(UnknownPluginError):
        compose_runtime(
            PROFILE,
            load_deployment(DEPLOYMENT_B),
            limited,
            require_data_plugins=False,
        )


def test_boundaries_capability_missing_after_compose_fails_closed():
    empty = DeploymentConfig(
        id="no_boundaries",
        version="1.0.0",
        description="boundaries capability intentionally absent",
        device=PluginSelection(plugin="cbsd"),
        rf=PluginSelection(plugin="free_space"),
        protocol=PluginSelection(plugin="winnforum_rest"),
        providers=(),
    )
    runtime = compose_runtime(
        PROFILE, empty, _registry_from_entry_points(), require_data_plugins=False
    )
    with pytest.raises(MissingCapabilityError):
        runtime.provider_for("boundaries")


def test_mapping_boundaries_entry_point_loads_contract():
    provider = DataProviderDiscovery().load("mapping_boundaries")
    assert isinstance(provider, MappingBoundariesProvider)
    record = provider.fetch(token=TOKEN_US_CANADA_BORDER)
    assert tuple(record.vertices) == LEGO_BOUNDARY_VERTICES
