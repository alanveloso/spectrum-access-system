"""Persistence attribution: reference contribution only from reference semantics."""

from __future__ import annotations

from profiles import load_profile
from profiles.context import selected_mechanism_ids
from models.persistence import (
    CBRS_WINNFORUM_ONLY_TABLES,
    GENERIC_TABLES,
    resolve_persistence_plan,
)
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.deployment import (
    DEFAULT_DEPLOYMENT_PATH,
    DeploymentConfig,
    PluginSelection,
)
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile


def _registry() -> PluginRegistry:
    return PluginRegistry.from_discovery()


def _network_only() -> DeploymentConfig:
    return DeploymentConfig(
        id="attr_network_only",
        version="1.0.0",
        network=PluginSelection(plugin="managed"),
    )


def _mapping_device() -> DeploymentConfig:
    return DeploymentConfig(
        id="attr_mapping_device",
        version="1.0.0",
        device=PluginSelection(plugin="mapping"),
        rf=PluginSelection(plugin="free_space"),
    )


def test_case_a_cbrs_reference_activates_reference_persistence():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.protocol_adapter is not None
    assert composition.protocol_adapter.protocol_id == "winnforum-rest"
    assert "aggregate_linear_power" in selected_mechanism_ids(composition.profile)
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" in plan.contribution_ids
    assert "cbsds" in plan.table_names
    assert "grants" in plan.table_names


def test_case_b_anatel_mapping_device_stays_generic():
    composition = compose_runtime(
        load_profile("br_anatel_slp_3700"),
        _mapping_device(),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.device_adapter is not None
    assert "geolocation" in composition.requirements.device_capabilities
    assert "protection_entitlement" in selected_mechanism_ids(composition.profile)
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)
    assert "cbsds" not in plan.table_names


def test_case_c_tvws_mapping_device_stays_generic():
    composition = compose_runtime(
        load_profile("us_tvws_15_711"),
        _mapping_device(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert plan.table_names & CBRS_WINNFORUM_ONLY_TABLES == set()


def test_case_d_elsa_network_stays_generic():
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only(),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.network_adapter is not None
    assert composition.device_adapter is None
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)


def test_case_e_synthetic_minimal_generic_only():
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)
    assert plan.table_names == GENERIC_TABLES


def test_generic_device_semantics_do_not_activate_cbrs_reference_persistence():
    composition = compose_runtime(
        load_profile("br_anatel_slp_3700"),
        _mapping_device(),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.device_adapter is not None
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


def test_generic_network_semantics_do_not_activate_cbrs_reference_persistence():
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only(),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.network_adapter is not None
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


def test_generic_protection_semantics_do_not_activate_cbrs_reference_persistence():
    """protection_entitlement / channel_exclusion alone are not CBRS ownership."""
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only(),
        _registry(),
        require_data_plugins=False,
    )
    mechs = set(selected_mechanism_ids(composition.profile))
    assert "protection_entitlement" in mechs
    assert "channel_exclusion" in mechs
    assert "aggregate_linear_power" not in mechs
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


def test_geolocation_requirement_does_not_activate_cbrs_reference_persistence():
    composition = compose_runtime(
        load_profile("us_tvws_15_711"),
        _mapping_device(),
        _registry(),
        require_data_plugins=False,
    )
    assert "geolocation" in composition.requirements.device_capabilities
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


def test_winnforum_protocol_alone_activates_reference_persistence():
    """Protocol semantic id winnforum-rest is sufficient positive evidence."""
    # ANATEL profile + WInnForum protocol (adversarial: shared protection mechs
    # present, but activation must come from protocol ownership).
    deployment = DeploymentConfig(
        id="attr_anatel_winnforum_protocol",
        version="1.0.0",
        protocol=PluginSelection(plugin="winnforum_rest"),
        device=PluginSelection(plugin="mapping"),
    )
    composition = compose_runtime(
        load_profile("br_anatel_slp_3700"),
        deployment,
        _registry(),
        require_data_plugins=False,
    )
    assert composition.protocol_adapter.protocol_id == "winnforum-rest"
    assert "aggregate_linear_power" not in selected_mechanism_ids(composition.profile)
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" in plan.contribution_ids
