"""Profile-Driven Architecture v1 permanent invariant fitness suite."""
from __future__ import annotations

import ast
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

from adapters.winnforum_rest import WINNFORUM_REST_PROTOCOL_ID
from models.persistence import (
    CBRS_WINNFORUM_ONLY_TABLES,
    GENERIC_TABLES,
    resolve_persistence_plan,
)
from profiles import load_profile
from profiles.context import profile_hash, selected_mechanism_ids
from profiles.parse import load_profile_document
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.deployment import (
    DEFAULT_DEPLOYMENT_PATH,
    DeploymentConfig,
    PluginSelection,
)
from runtime.errors import MissingCapabilityError
from runtime.toggle_reconciliation import (
    RequiredCapabilityDisabledError,
    reconcile_operational_toggles,
)
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]

BASELINE_PROFILE_HASHES = {
    "cbrs_winnforum": (
        "27e83545a62be543e23691a52fc1de5570877c9305f628f956bd2fbaf209158b"
    ),
    "br_anatel_slp_3700": (
        "cbdb21ef3178a32ec14e7aba7f57d3b8159c15a420204ee2b495f157db39bb90"
    ),
    "eu_elsa": (
        "0ca44d8afe71948e15b743a5a11d3adc220e638297e62fde7cf0921d3e27a044"
    ),
    "us_tvws_15_711": (
        "2eafa5766c07ecf0d893c56f08a21c66d0be93ea5d37ef749420e72bc7277474"
    ),
}

# Plugin entry-point name (deployment) vs semantic protocol_id (adapter contract).
WINNFORUM_PLUGIN_NAME = "winnforum_rest"


def _registry() -> PluginRegistry:
    return PluginRegistry.from_discovery()


def _network_only() -> DeploymentConfig:
    return DeploymentConfig(
        id="v1_network_only",
        version="1.0.0",
        network=PluginSelection(plugin="managed"),
    )


def _mapping_device() -> DeploymentConfig:
    return DeploymentConfig(
        id="v1_mapping_device",
        version="1.0.0",
        device=PluginSelection(plugin="mapping"),
        rf=PluginSelection(plugin="free_space"),
    )


# --- Persistence attribution invariants (INV-12) -----------------------------------


def test_v1_anatel_mapping_has_no_cbrs_reference_persistence():
    composition = compose_runtime(
        load_profile("br_anatel_slp_3700"),
        _mapping_device(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert "cbsds" not in plan.table_names


def test_v1_elsa_network_has_no_cbrs_reference_persistence():
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert plan.table_names & CBRS_WINNFORUM_ONLY_TABLES == set()


def test_v1_tvws_mapping_has_no_cbrs_reference_persistence():
    composition = compose_runtime(
        load_profile("us_tvws_15_711"),
        _mapping_device(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


def test_v1_synthetic_generic_only_persistence():
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)
    assert plan.table_names == GENERIC_TABLES


def test_v1_cbrs_reference_activates_reference_persistence():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.protocol_adapter is not None
    assert composition.protocol_adapter.protocol_id == WINNFORUM_REST_PROTOCOL_ID
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" in plan.contribution_ids


def test_v1_protocol_id_is_semantic_not_plugin_package_name():
    """winnforum-rest (protocol_id) ≠ winnforum_rest (entry-point / plugin name)."""
    assert WINNFORUM_REST_PROTOCOL_ID == "winnforum-rest"
    assert WINNFORUM_PLUGIN_NAME == "winnforum_rest"
    assert WINNFORUM_REST_PROTOCOL_ID != WINNFORUM_PLUGIN_NAME
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.provenance.selected_plugins.get("protocol") == WINNFORUM_PLUGIN_NAME
    assert composition.protocol_adapter.protocol_id == WINNFORUM_REST_PROTOCOL_ID


def test_v1_generic_protection_mechanisms_rejected_as_cbrs_triggers():
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only(),
        _registry(),
        require_data_plugins=False,
    )
    mechs = set(selected_mechanism_ids(composition.profile))
    assert {"protection_entitlement", "channel_exclusion", "snapshot_evaluate_apply"} <= mechs
    assert "aggregate_linear_power" not in mechs
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


# --- INV-11 / INV-13 synthetic --------------------------------------------------


def test_v1_synthetic_requirements_and_composition():
    profile = synthetic_profile()
    from runtime.capabilities import required_capabilities

    req = required_capabilities(profile)
    assert "geolocation" not in req.device_capabilities
    assert "network_identity" in req.network_capabilities
    composition = compose_runtime(
        profile, synthetic_deployment(), _registry(), require_data_plugins=False
    )
    assert composition.device_adapter is None
    assert composition.network_adapter is not None
    assert composition.protocol_adapter is None
    assert composition.rf is None


# --- INV-01 / hashes / novel profile --------------------------------------------


def test_baseline_profile_hashes_identical():
    for profile_id, expected in BASELINE_PROFILE_HASHES.items():
        assert profile_hash(load_profile(profile_id)) == expected


def test_v1_generic_packages_have_no_profile_id_dispatch():
    banned = set(BASELINE_PROFILE_HASHES) | {"synthetic_minimal"}
    roots = (
        REPO / "runtime",
        REPO / "database.py",
        REPO / "models" / "persistence.py",
        REPO / "primitives",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if (
                    isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                    and comparator.value in banned
                ):
                    pytest.fail(
                        f"{path} compares against profile id {comparator.value!r}"
                    )


def test_v1_novel_profile_zero_core_change(tmp_path: Path):
    novel = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {
            "id": "v1_novel_minimal",
            "version": "0.1.0",
            "status": "custom",
        },
        "spectrum": {
            "ranges": [{"id": "primary", "low_hz": 1000000000, "high_hz": 1100000000}]
        },
        "power": {"mechanism": "rule_table", "rules": [{"max_eirp_dbm": 10.0}]},
        "requirements": {
            "network_capabilities": ["network_identity", "frequency_range"]
        },
    }
    path = tmp_path / "v1_novel_minimal.yaml"
    path.write_text(yaml.safe_dump(novel), encoding="utf-8")
    parsed = load_profile_document(path)
    composition = compose_runtime(
        parsed, _network_only(), _registry(), require_data_plugins=False
    )
    assert composition.network_adapter is not None
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)
    for rel in ("runtime", "database.py", "models/persistence.py", "primitives"):
        root = REPO / rel
        paths = [root] if root.is_file() else list(root.rglob("*.py"))
        for p in paths:
            assert "v1_novel_minimal" not in p.read_text(encoding="utf-8")


# --- INV-10 toggle + missing capability -----------------------------------------


def test_v1_cbrs_iap_disabled_fails_closed():
    with pytest.raises(RequiredCapabilityDisabledError):
        reconcile_operational_toggles(
            load_profile("cbrs_winnforum"),
            SimpleNamespace(sas_iap_enabled=False),
        )


def test_v1_elsa_missing_network_fails_closed():
    with pytest.raises(MissingCapabilityError):
        compose_runtime(
            load_profile("eu_elsa"),
            load_deployment(DEFAULT_DEPLOYMENT_PATH),
            _registry(),
            require_data_plugins=False,
        )


# --- INV-14 discovery smoke (installed entry points) ----------------------------


def test_v1_installed_entry_point_groups_present():
    expected = {
        "spectrum_access.device_adapters": {"cbsd", "mapping"},
        "spectrum_access.network_adapters": {"managed", "mapping"},
        "spectrum_access.protocol_adapters": {
            "elsa1",
            "generic_json",
            "winnforum_rest",
        },
        "spectrum_access.rf_models": {"free_space", "itm"},
        "spectrum_access.data_providers": {
            "bundle_boundaries",
            "bundle_protected_entities",
            "mapping_boundaries",
            "protection_boundaries",
            "protection_land_cover",
            "protection_reference",
            "protection_rights",
            "protection_terrain",
        },
    }
    for group, names in expected.items():
        found = {ep.name for ep in entry_points().select(group=group)}
        assert names <= found, f"{group}: missing {names - found}"
    # Mechanisms group is open; synthetic fitness plugin is installed in this repo.
    mech = {ep.name for ep in entry_points().select(group="spectrum_access.mechanisms")}
    assert "synthetic_mechanism" in mech
