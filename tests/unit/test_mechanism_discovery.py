"""External mechanism discovery proofs (open catalog, not closed-world)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from primitives.discovery import GROUP_MECHANISMS, MechanismDiscovery
from primitives.registry import (
    MechanismAxis,
    MechanismContract,
    MechanismRegistry,
    builtin_mechanism_contracts,
    builtin_mechanism_registry,
    discovered_mechanism_registry,
)
from profiles.errors import ProfileValidationError
from profiles.parse import load_profile_document, parse_profile_document
from tests.fixtures.plugins.synthetic_mechanism import (
    SYNTHETIC_MECHANISM_ID,
    synthetic_mechanism_contract,
)

REPO = Path(__file__).resolve().parents[2]
SYNTHETIC_PROFILE = (
    REPO / "tests" / "fixtures" / "profiles" / "synthetic_mechanism_fitness.yaml"
)
_CORE_SCAN_ROOTS = (
    REPO / "primitives",
    REPO / "profiles",
    REPO / "runtime",
)


def test_builtin_seed_unchanged_and_open_compose():
    seed = builtin_mechanism_registry()
    assert SYNTHETIC_MECHANISM_ID not in seed.ids()
    assert "ordered_classes" in seed.ids()

    composed = discovered_mechanism_registry()
    assert SYNTHETIC_MECHANISM_ID in composed.ids()
    assert "ordered_classes" in composed.ids()
    assert composed.get(SYNTHETIC_MECHANISM_ID).axis is MechanismAxis.PROTECTION


def test_entry_point_discovery_loads_synthetic_without_manual_injection():
    discovery = MechanismDiscovery()
    assert "synthetic_mechanism" in discovery.names()
    contract = discovery.load("synthetic_mechanism")
    assert contract.mechanism_id == SYNTHETIC_MECHANISM_ID
    assert contract == synthetic_mechanism_contract()


def test_zero_core_change_sources_have_no_synthetic_mechanism_token():
    """Adding synthetic_mechanism must not require editing generic sources."""
    banned = SYNTHETIC_MECHANISM_ID
    for root in _CORE_SCAN_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert banned not in text, f"{path} must not reference {banned}"


def test_synthetic_profile_loads_with_discovered_registry():
    parsed = load_profile_document(SYNTHETIC_PROFILE)
    assert parsed.metadata.id == "synthetic_mechanism_fitness"
    assert SYNTHETIC_MECHANISM_ID in parsed.protection.mechanisms


def test_missing_required_mechanism_fails_closed():
    raw = yaml.safe_load(SYNTHETIC_PROFILE.read_text(encoding="utf-8"))
    empty = MechanismDiscovery(overlays={}, list_entry_points=lambda _g: ())
    builtins_only = MechanismRegistry(builtin_mechanism_contracts())
    with pytest.raises(ProfileValidationError, match="unknown mechanism"):
        parse_profile_document(raw, registry=builtins_only)
    # Explicit empty discovery also fails closed (no silent skip).
    with pytest.raises(ProfileValidationError, match="unknown mechanism"):
        parse_profile_document(
            raw, registry=discovered_mechanism_registry(discovery=empty)
        )


def test_duplicate_mechanism_identity_fails_deterministically():
    discovery = MechanismDiscovery(
        overlays={
            "synthetic_mechanism": synthetic_mechanism_contract,
            "synthetic_mechanism_alt": synthetic_mechanism_contract,
        },
        list_entry_points=lambda _g: (),
    )
    with pytest.raises(ValueError, match="duplicate mechanism"):
        discovered_mechanism_registry(discovery=discovery)

    with pytest.raises(ValueError, match="duplicate mechanism"):
        discovered_mechanism_registry(
            discovery=MechanismDiscovery(
                overlays={
                    "ordered_classes_ext": MechanismContract(
                        "ordered_classes", MechanismAxis.ACCESS, "9.9.9"
                    ),
                },
                list_entry_points=lambda _g: (),
            )
        )


def test_malformed_mechanism_plugin_fails_at_discovery():
    discovery = MechanismDiscovery(
        overlays={"bad_mech": lambda: "not-a-contract"},
        list_entry_points=lambda _g: (),
    )
    with pytest.raises(ValueError, match="MechanismContract"):
        discovery.load("bad_mech")

    discovery_invalid_id = MechanismDiscovery(
        overlays={
            "bad_id": lambda: MechanismContract(
                "Bad-ID", MechanismAxis.PROTECTION, "1.0.0"
            )
        },
        list_entry_points=lambda _g: (),
    )
    with pytest.raises(ValueError, match="invalid mechanism_id"):
        discovery_invalid_id.load("bad_id")


def test_adapter_discovery_does_not_load_mechanisms():
    from adapters.discovery import AdapterDiscovery

    discovery = AdapterDiscovery(list_entry_points=lambda _g: ())
    with pytest.raises(ValueError, match="MechanismDiscovery"):
        discovery.names(GROUP_MECHANISMS)


def test_registry_py_is_seed_not_closed_world_exhaustive_list():
    """AST guard: registry must expose discovery compose, not only a closed tuple."""
    source = (REPO / "primitives" / "registry.py").read_text(encoding="utf-8")
    assert "discovered_mechanism_registry" in source
    assert "builtin_mechanism_contracts" in source
    tree = ast.parse(source)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "discovered_mechanism_registry" in names
