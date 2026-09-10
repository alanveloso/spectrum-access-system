"""Phase F: required data capabilities fail closed at composition time."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from primitives.geography import GeoPoint
from profiles import load_profile
from profiles.schema import DataSection
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH, DeploymentConfig, PluginSelection
from runtime.errors import MissingCapabilityError, IncompatiblePluginError
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile

REPO = Path(__file__).resolve().parents[2]

_PRODUCTION_BOOTSTRAP = (
    "main.py",
    "celery_app.py",
)


def test_production_bootstrap_does_not_pass_require_data_plugins_false():
    for rel in _PRODUCTION_BOOTSTRAP:
        source = (REPO / rel).read_text(encoding="utf-8")
        assert "require_data_plugins=False" not in source, rel
    bootstrap = ast.parse((REPO / "runtime" / "bootstrap.py").read_text(encoding="utf-8"))
    for node in ast.walk(bootstrap):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "initialize_process_runtime_composition":
            continue
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if arg.arg != "require_data_plugins":
                continue
            assert not (
                isinstance(default, ast.Constant) and default.value is False
            ), "bootstrap default require_data_plugins must be True"


def test_cbrs_reference_satisfies_every_required_data_capability():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
    )
    assert composition.provenance.unsatisfied_data_capabilities == ()
    for cap in sorted(composition.requirements.data_capabilities):
        assert composition.provider_for(cap) is not None


def test_synthetic_profile_without_data_requirements_composes():
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        PluginRegistry.from_discovery(),
    )
    assert composition.requirements.data_capabilities == frozenset()
    assert composition.provenance.unsatisfied_data_capabilities == ()


def test_required_generic_capability_without_satisfier_fails_closed():
    profile = synthetic_profile().model_copy(
        update={"data": DataSection(required_capabilities=("terrain",))}
    )
    with pytest.raises(MissingCapabilityError, match="terrain"):
        compose_runtime(
            profile, synthetic_deployment(), PluginRegistry.from_discovery()
        )


def test_unselected_installed_provider_does_not_satisfy():
    """protection_terrain is installed, but synthetic deployment selects none."""
    profile = synthetic_profile().model_copy(
        update={"data": DataSection(required_capabilities=("terrain",))}
    )
    with pytest.raises(MissingCapabilityError, match="terrain"):
        compose_runtime(
            profile, synthetic_deployment(), PluginRegistry.from_discovery()
        )


def test_selected_unavailable_provider_fails_closed():
    profile = synthetic_profile().model_copy(
        update={"data": DataSection(required_capabilities=("terrain",))}
    )
    deployment = DeploymentConfig(
        id="phase_f_missing_plugin",
        providers=(PluginSelection(plugin="nonexistent_terrain_provider_xyz"),),
    )
    with pytest.raises((MissingCapabilityError, IncompatiblePluginError, ValueError)):
        compose_runtime(profile, deployment, PluginRegistry.from_discovery())


def test_land_cover_provider_present_missing_tile_is_fetch_error_not_startup():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
    )
    cover = composition.provider_for("land_cover")
    with pytest.raises(ValueError, match="land_cover coverage missing"):
        cover.fetch(point=GeoPoint(latitude_deg=0.0, longitude_deg=0.0))


def test_compose_runtime_documents_test_only_data_escape():
    source = (REPO / "runtime" / "composition.py").read_text(encoding="utf-8")
    assert "require_data_plugins" in source
    assert "test" in source.lower()
