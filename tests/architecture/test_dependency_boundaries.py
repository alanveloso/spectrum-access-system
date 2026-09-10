"""Phase E: generic packages must not depend on business services.

Pre-change proof at b1912be: ``runtime/rf_bridge.py`` imports
``services.iap.models`` and ``services.propagation.errors`` — a reversed edge
(generic runtime → specialized services). After remediation that edge is gone.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Generic / lower-level packages whose production modules must not import services.
_GENERIC_PACKAGE_ROOTS = (
    "runtime",
    "primitives",
    "profiles",
)

# RF *contracts* and generic backends — not concrete CBRS adapters.
_GENERIC_RF_MODULES = (
    "rf/port.py",
    "rf/discovery.py",
    "rf/boundary.py",
    "rf/external_propagation.py",
    "rf/__init__.py",
)

# Provider *contracts* / discovery — not concrete bundle implementations.
_GENERIC_PROVIDER_MODULES = (
    "providers/contract.py",
    "providers/discovery.py",
    "providers/__init__.py",
)

# Adapter *interfaces* / discovery — not concrete CBRS/eLSA adapters.
_GENERIC_ADAPTER_MODULES = (
    "adapters/device.py",
    "adapters/protocol.py",
    "adapters/discovery.py",
    "adapters/plugin_names.py",
    "adapters/__init__.py",
)

# Bootstrap / composition-root modules may wire across layers.
_BOOTSTRAP_ALLOWLIST = frozenset(
    {
        # none under runtime/ for services — bootstrap is main/celery_app
    }
)

# Tooling inside generic packages that may inspect wider surfaces.
_TOOLING_ALLOWLIST = frozenset(
    {
        "profiles/doctor.py",
        "runtime/doctor.py",
    }
)

# Concrete reference modules that live under rf/ or providers/ but are not
# generic contracts (explicitly out of the generic→services ban).
_CONCRETE_REFERENCE_MODULES = frozenset(
    {
        "rf/cbrs_winnforum.py",
        "providers/protection_bundle.py",
        "providers/operator_feature_bundle.py",
    }
)


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _imports_services(tree: ast.AST) -> list[str]:
    return sorted(
        mod
        for mod in _imported_modules(tree)
        if mod == "services" or mod.startswith("services.")
    )


def _iter_package_py_files(package: str) -> list[Path]:
    root = REPO / package
    assert root.is_dir(), package
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_historical_runtime_rf_bridge_removed():
    """Phase E remediation: IAP→RfPort adapter must not live under runtime."""
    assert not (REPO / "runtime" / "rf_bridge.py").is_file()
    assert (REPO / "services" / "iap" / "rf_adapter.py").is_file()


def test_runtime_package_does_not_import_services():
    offenders: list[str] = []
    for path in _iter_package_py_files("runtime"):
        rel = path.relative_to(REPO).as_posix()
        if rel in _TOOLING_ALLOWLIST or rel in _BOOTSTRAP_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for mod in _imports_services(tree):
            offenders.append(f"{rel} -> {mod}")
    assert offenders == [], (
        "generic runtime must not import business services:\n"
        + "\n".join(offenders)
    )


def test_primitives_and_profiles_do_not_import_services():
    offenders: list[str] = []
    for package in ("primitives", "profiles"):
        for path in _iter_package_py_files(package):
            rel = path.relative_to(REPO).as_posix()
            if rel in _TOOLING_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for mod in _imports_services(tree):
                offenders.append(f"{rel} -> {mod}")
    assert offenders == [], (
        "primitives/profiles must not import business services:\n"
        + "\n".join(offenders)
    )


def test_generic_rf_contracts_do_not_import_services():
    offenders: list[str] = []
    for relative in _GENERIC_RF_MODULES:
        path = REPO / relative
        assert path.is_file(), relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for mod in _imports_services(tree):
            offenders.append(f"{relative} -> {mod}")
    assert offenders == [], (
        "generic RF contracts must not import business services:\n"
        + "\n".join(offenders)
    )


def test_generic_provider_and_adapter_contracts_do_not_import_services():
    offenders: list[str] = []
    for relative in (*_GENERIC_PROVIDER_MODULES, *_GENERIC_ADAPTER_MODULES):
        path = REPO / relative
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for mod in _imports_services(tree):
            offenders.append(f"{relative} -> {mod}")
    assert offenders == [], (
        "generic provider/adapter contracts must not import business services:\n"
        + "\n".join(offenders)
    )


def test_iap_rf_adapter_owns_iap_to_rf_translation():
    """Consumer-side ownership: IAP adapts its models to RfPort."""
    path = REPO / "services" / "iap" / "rf_adapter.py"
    assert path.is_file(), "expected services/iap/rf_adapter.py after Phase E move"
    text = path.read_text(encoding="utf-8")
    assert "GrantRfInfo" in text
    assert "path_loss_db_fn_from_rf_port" in text
    assert "from rf.port import" in text or "from rf.port import" in text.replace(
        " ", " "
    )
    # Must not live under runtime anymore.
    assert not (REPO / "runtime" / "rf_bridge.py").is_file()


def test_no_runtime_reexport_hiding_services_dependency():
    """Neutral aliases under runtime must not re-export services types."""
    for path in _iter_package_py_files("runtime"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _imports_services(tree) == [] or path.relative_to(REPO).as_posix() in (
            _TOOLING_ALLOWLIST | _BOOTSTRAP_ALLOWLIST
        )
