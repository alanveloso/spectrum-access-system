"""Architecture guard: production SAS must not execute WInnForum ITM directly."""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

# Production packages that must not import concrete WInnForum ITM.
_PRODUCTION_ROOTS = (
    "services",
    "routes",
    "rf",
    "runtime",
    "adapters",
    "providers",
    "profiles",
    "primitives",
    "models",
    "schemas",
    "protection_data",
    "compliance",
    "config.py",
    "main.py",
    "database.py",
    "tasks.py",
    "celery_app.py",
)

_FORBIDDEN_MODULES = frozenset(
    {
        "reference_models.propagation.wf_itm",
        "wf_itm",
    }
)
_FORBIDDEN_NAMES = frozenset({"CalcItmPropagationLoss"})


def _iter_production_py() -> list[Path]:
    files: list[Path] = []
    for name in _PRODUCTION_ROOTS:
        path = REPO / name
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def _module_imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                found.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
                found.add(alias.name)
    return found


def _attr_calls(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            names.add(node.attr)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
    return names


# Architecture guard: do not treat docstring mentions of forbidden APIs as
# Name/Attr bindings. Only Import / ImportFrom / Attribute loads count.
def test_production_modules_do_not_import_wf_itm() -> None:
    offenders: list[str] = []
    for path in _iter_production_py():
        # Skip pure documentation modules if any; all .py under production roots.
        if path.name.endswith(".md"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _module_imports(tree)
        for forbidden in _FORBIDDEN_MODULES:
            if forbidden in imports:
                offenders.append(f"{path.relative_to(REPO)}:import:{forbidden}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
                offenders.append(f"{path.relative_to(REPO)}:attr:{node.attr}")
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
                offenders.append(f"{path.relative_to(REPO)}:name:{node.id}")
    # Docstring/comment text is invisible to AST Name nodes; filter known
    # documentation-only string mentions is unnecessary.
    assert offenders == [], "direct WInnForum ITM coupling:\n" + "\n".join(offenders)


def test_esc_intentionally_bypasses_generic_rf_port() -> None:
    """ESC needs incidence + antenna patterns beyond RfPort.path_loss (Outcome D)."""
    from services.iap import coupling

    src = Path(coupling.__file__).read_text(encoding="utf-8")
    assert "make_esc_iap_coupling" in src
    assert "incidence" in src.lower() or "hor_cbsd" in src or "hor_rx" in src
    # Documented boundary: ESC does not consume composed RfPort for path loss.
    assert "Specialized path" in src or "beyond ``RfPort.path_loss``" in src


def test_rel1ext_composition_stays_in_sas() -> None:
    from services.propagation import rel1ext_dpa

    assert hasattr(rel1ext_dpa, "compose_dpa_pathloss_db")
    assert hasattr(rel1ext_dpa, "calc_p2108_clutter_db")
