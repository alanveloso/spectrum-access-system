"""Architecture fitness guards for the profile-driven claim.

Invariant under test: adding a new Spectrum Profile may require new plugins,
providers, protocols or regulatory mechanisms, but it must not require editing
the generic platform merely to recognize or select that regime.

These guards protect the generic layers only. Regulatory modules, reference
deployments, tooling and tests may legitimately name concrete regimes.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_BUILTIN_PROFILE_IDS = frozenset(
    {
        "cbrs_winnforum",
        "br_anatel_slp_3700",
        "eu_elsa",
        "us_tvws_15_711",
    }
)

_JURISDICTION_TOKENS = frozenset(
    {
        "US",
        "USA",
        "BR",
        "EU",
        "United States",
        "Brazil",
        "FCC",
        "ANATEL",
        "WInnForum",
        "CBRS",
        "TVWS",
    }
)

# Generic platform surface: contracts, profile infrastructure, composition
# machinery and plugin discovery. Everything here must be regime-agnostic.
_GENERIC_CORE = (
    "runtime/capabilities.py",
    "runtime/composition.py",
    "runtime/context.py",
    "runtime/deployment.py",
    "runtime/doctor.py",
    "runtime/errors.py",
    "runtime/registry.py",
    "runtime/__init__.py",
    "profiles/schema.py",
    "profiles/parse.py",
    "profiles/semantics.py",
    "profiles/context.py",
    "profiles/negotiate.py",
    "profiles/trust.py",
    "profiles/errors.py",
    "profiles/cost.py",
    "profiles/__init__.py",
    "adapters/device.py",
    "adapters/protocol.py",
    "adapters/discovery.py",
    "adapters/plugin_names.py",
    "providers/contract.py",
    "providers/discovery.py",
    "rf/port.py",
    "rf/discovery.py",
    "rf/boundary.py",
    "rf/__init__.py",
)

# Concrete implementations that generic core must never import directly.
_CONCRETE_MODULES = (
    "adapters.cbsd",
    "adapters.winnforum_rest",
    "adapters.elsa1",
    "adapters.managed_consumer",
    "rf.cbrs_winnforum",
    "rf.external_propagation",
    "services",
    "models",
    "routes",
    "schemas",
    "protection_data",
)

# Known, classified regime coupling inside otherwise generic packages. Listed so
# the debt cannot grow silently; see docs/architecture/extension_boundaries.md.
_ALLOWED_REGIME_COUPLING = frozenset(
    {
        # WInnForum-specific route accessor living in the generic runtime package.
        "runtime/access.py",
        # Source-tree bootstrap when entry points are unavailable.
        "profiles/doctor.py",
    }
)


def _generic_core_trees() -> list[tuple[str, ast.Module]]:
    trees: list[tuple[str, ast.Module]] = []
    for relative in _GENERIC_CORE:
        path = REPO / relative
        assert path.is_file(), f"generic core module missing: {relative}"
        trees.append(
            (relative, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        )
    return trees


def _string_constants(node: ast.AST) -> list[str]:
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _selector_literals(tree: ast.Module) -> list[tuple[int, str]]:
    """String literals used to select behaviour: comparisons, dict keys, matches."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for value in _string_constants(node):
                found.append((node.lineno, value))
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.append((node.lineno, key.value))
        elif isinstance(node, ast.MatchValue):
            for value in _string_constants(node):
                found.append((node.lineno, value))
        elif isinstance(node, ast.Subscript):
            for value in _string_constants(node.slice):
                found.append((node.lineno, value))
    return found


def test_generic_core_does_not_branch_on_builtin_profile_id():
    offenders: list[str] = []
    for relative, tree in _generic_core_trees():
        for lineno, value in _selector_literals(tree):
            if value in _BUILTIN_PROFILE_IDS:
                offenders.append(f"{relative}:{lineno}: {value!r}")
    assert offenders == [], (
        "generic platform code selects behaviour by builtin profile id:\n"
        + "\n".join(offenders)
    )


def test_generic_core_does_not_branch_on_jurisdiction():
    offenders: list[str] = []
    for relative, tree in _generic_core_trees():
        for lineno, value in _selector_literals(tree):
            if value in _JURISDICTION_TOKENS:
                offenders.append(f"{relative}:{lineno}: {value!r}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "country",
                "jurisdiction",
            }:
                offenders.append(f"{relative}:{node.lineno}: .{node.attr}")
    assert offenders == [], (
        "generic platform code selects behaviour by jurisdiction:\n"
        + "\n".join(offenders)
    )


def test_no_profile_or_jurisdiction_dispatch_tables_in_production():
    prefixes = ("PROFILE_TO_", "JURISDICTION_TO_", "COUNTRY_TO_", "REGIME_TO_")
    roots = (
        "runtime",
        "profiles",
        "primitives",
        "adapters",
        "providers",
        "rf",
        "services",
        "routes",
        "models",
        "schemas",
    )
    offenders: list[str] = []
    for root in roots:
        for path in sorted((REPO / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names = [node.target.id]
                for name in names:
                    if name.startswith(prefixes):
                        rel = path.relative_to(REPO)
                        offenders.append(f"{rel}:{node.lineno}: {name}")
    assert offenders == [], "profile/jurisdiction dispatch table found:\n" + "\n".join(
        offenders
    )


def test_generic_core_does_not_import_concrete_implementations():
    offenders: list[str] = []
    for relative, tree in _generic_core_trees():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".")[0]
                if module in _CONCRETE_MODULES or root in _CONCRETE_MODULES:
                    offenders.append(f"{relative}:{node.lineno}: {module}")
    assert offenders == [], (
        "generic platform code imports a concrete implementation:\n"
        + "\n".join(offenders)
    )


def test_regime_coupling_inside_generic_packages_stays_contained():
    """Regime-specific imports in runtime/ and profiles/ must not spread."""
    coupled: set[str] = set()
    for root in ("runtime", "profiles"):
        for path in sorted((REPO / root).rglob("*.py")):
            relative = str(path.relative_to(REPO))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    root_module = module.split(".")[0]
                    if module in _CONCRETE_MODULES or root_module in _CONCRETE_MODULES:
                        coupled.add(relative)
    assert coupled == _ALLOWED_REGIME_COUPLING, (
        "regime coupling in generic packages changed; update the audit document "
        f"before changing this guard: {sorted(coupled)}"
    )


def test_composition_resolves_plugins_only_through_the_registry():
    path = REPO / "runtime" / "composition.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    loaders = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("load_")
    }
    assert loaders == {
        "load_protocol",
        "load_device",
        "load_network",
        "load_rf",
        "load_provider",
    }


def test_startup_dataset_readiness_is_capability_driven():
    """Protection-data readiness must follow profile requirements, not be constant."""
    source = (REPO / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="main.py")
    readiness_loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "provider"
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "getattr"
            for inner in ast.walk(node)
        )
    ]
    assert readiness_loops, "startup must validate composed providers when data is required"
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "data_capabilities"
            for inner in ast.walk(node.test)
        )
    ]
    assert guards, "startup provider readiness must be gated on profile data capabilities"
    assert "assert_protection_data_ready" not in source
