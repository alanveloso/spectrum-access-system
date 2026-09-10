"""Persistence attribution and synthetic isolation architecture proofs."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from models.persistence import (
    CBRS_WINNFORUM_ONLY_TABLES,
    GENERIC_TABLES,
    resolve_persistence_plan,
)
from profiles import load_profile
from profiles.context import profile_hash, selected_mechanism_ids
from profiles.parse import load_profile_document
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.capabilities import required_capabilities
from runtime.deployment import (
    DEFAULT_DEPLOYMENT_PATH,
    DeploymentConfig,
    PluginSelection,
)
from runtime.errors import MissingCapabilityError
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile

REPO = Path(__file__).resolve().parents[2]

# Freeze baseline profile hashes (must remain identical across freeze docs).
FREEZE_PROFILE_HASHES = {
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


def _registry() -> PluginRegistry:
    return PluginRegistry.from_discovery()


def _network_only_deployment() -> DeploymentConfig:
    return DeploymentConfig(
        id="freeze_network_only",
        version="1.0.0",
        description="Freeze audit: network adapter only",
        network=PluginSelection(plugin="managed"),
    )


def _mapping_device_deployment() -> DeploymentConfig:
    return DeploymentConfig(
        id="freeze_mapping_device",
        version="1.0.0",
        description="Freeze audit: generic mapping device + free_space RF",
        device=PluginSelection(plugin="mapping"),
        rf=PluginSelection(plugin="free_space"),
    )


# --- INV-11 / synthetic isolation (must remain PASS) -----------------------------


def test_inv11_synthetic_persistence_has_no_cbrs_tables():
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)
    assert plan.table_names == GENERIC_TABLES
    assert not (plan.table_names & CBRS_WINNFORUM_ONLY_TABLES)


def test_inv11_synthetic_startup_requirements():
    profile = synthetic_profile()
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


# --- Profile hash freeze ---------------------------------------------------------


def test_baseline_profile_hashes_identical():
    for profile_id, expected in FREEZE_PROFILE_HASHES.items():
        assert profile_hash(load_profile(profile_id)) == expected


# --- Profile-ID guard (INV-01) ---------------------------------------------------


def test_inv01_generic_packages_have_no_profile_id_dispatch():
    import ast

    banned_ids = set(FREEZE_PROFILE_HASHES) | {"synthetic_minimal"}
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
        source = path.read_text(encoding="utf-8")
        # Allow comments/docs mentioning profile names; forbid string constants used
        # as dispatch identities in comparisons is covered by AST Constant scan in
        # database.py tests; here forbid `profile.metadata.id == "…"`.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if (
                    isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                    and comparator.value in banned_ids
                ):
                    pytest.fail(
                        f"{path} compares against profile id {comparator.value!r}"
                    )


# --- Persistence attribution and regime isolation ---------------------------------


def test_elsa_network_only_does_not_activate_cbrs_reference_persistence():
    """eLSA network-only must not pull full CBRS reference schema."""
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.device_adapter is None
    assert composition.network_adapter is not None
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert plan.table_names & CBRS_WINNFORUM_ONLY_TABLES == set()


def test_anatel_generic_mapping_device_does_not_activate_cbrs_persistence():
    """ANATEL + MappingDeviceAdapter must not activate Cbsd/Grant/PAL/…"""
    composition = compose_runtime(
        load_profile("br_anatel_slp_3700"),
        _mapping_device_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    assert composition.device_adapter is not None
    assert type(composition.device_adapter).__name__ == "MappingDeviceAdapter"
    assert composition.protocol_adapter is None
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert "cbsds" not in plan.table_names


def test_tvws_generic_mapping_device_does_not_activate_cbrs_persistence():
    composition = compose_runtime(
        load_profile("us_tvws_15_711"),
        _mapping_device_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    assert type(composition.device_adapter).__name__ == "MappingDeviceAdapter"
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert plan.table_names & CBRS_WINNFORUM_ONLY_TABLES == set()


def test_desired_invariant_elsa_must_not_require_cbrs_persistence():
    composition = compose_runtime(
        load_profile("eu_elsa"),
        _network_only_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" not in plan.contribution_ids
    assert plan.table_names & CBRS_WINNFORUM_ONLY_TABLES == set()


def test_desired_invariant_anatel_mapping_must_not_require_cbrs_orm():
    composition = compose_runtime(
        load_profile("br_anatel_slp_3700"),
        _mapping_device_deployment(),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbsds" not in plan.table_names
    assert "cbrs_winnforum_reference" not in plan.contribution_ids


# --- Novel profile zero-core (existing semantics only) ---------------------------


def test_novel_profile_zero_core_change(tmp_path: Path):
    novel = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {
            "id": "freeze_novel_minimal",
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
    path = tmp_path / "freeze_novel_minimal.yaml"
    path.write_text(yaml.safe_dump(novel), encoding="utf-8")
    parsed = load_profile_document(path)
    composition = compose_runtime(
        parsed, _network_only_deployment(), _registry(), require_data_plugins=False
    )
    assert composition.network_adapter is not None
    plan = resolve_persistence_plan(composition)
    # Novel profile without CBRS-trigger mechanisms stays generic-only.
    assert plan.contribution_ids == ("generic",)
    # Production sources must not mention this novel id.
    for rel in ("runtime", "database.py", "models/persistence.py", "primitives"):
        root = REPO / rel
        paths = [root] if root.is_file() else list(root.rglob("*.py"))
        for p in paths:
            assert "freeze_novel_minimal" not in p.read_text(encoding="utf-8")


# --- CBRS reference still activates when appropriate -----------------------------


def test_cbrs_reference_deployment_still_selects_reference_persistence():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        _registry(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "cbrs_winnforum_reference" in plan.contribution_ids
    assert "aggregate_linear_power" in selected_mechanism_ids(composition.profile)


# --- Toggle fail-closed (INV-09) -------------------------------------------------


def test_inv09_cbrs_iap_disabled_fails_closed():
    from types import SimpleNamespace

    from runtime.toggle_reconciliation import (
        RequiredCapabilityDisabledError,
        reconcile_operational_toggles,
    )

    with pytest.raises(RequiredCapabilityDisabledError):
        reconcile_operational_toggles(
            load_profile("cbrs_winnforum"),
            SimpleNamespace(sas_iap_enabled=False),
        )


# --- Required network missing fails closed ---------------------------------------


def test_required_network_missing_fails_closed():
    with pytest.raises(MissingCapabilityError):
        compose_runtime(
            load_profile("eu_elsa"),
            load_deployment(DEFAULT_DEPLOYMENT_PATH),  # no network selection
            _registry(),
            require_data_plugins=False,
        )
