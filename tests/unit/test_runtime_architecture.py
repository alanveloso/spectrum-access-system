"""Architecture guards for the runtime composition package."""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "runtime"
PROFILES = REPO / "profiles"
PRIMITIVES = REPO / "primitives"

_BUILTIN_PROFILE_IDS = (
    "cbrs_winnforum",
    "br_anatel_slp_3700",
    "eu_elsa",
    "us_tvws_15_711",
)

# Generic composition core must not hard-code builtin profile ids / countries.
_GENERIC_RUNTIME_MODULES = (
    "capabilities.py",
    "composition.py",
    "deployment.py",
    "doctor.py",
    "errors.py",
    "registry.py",
    "__init__.py",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_runtime_core_has_no_builtin_profile_id_branches():
    offenders: list[str] = []
    for name in _GENERIC_RUNTIME_MODULES:
        path = RUNTIME / name
        text = path.read_text(encoding="utf-8")
        for profile_id in _BUILTIN_PROFILE_IDS:
            if profile_id in text:
                offenders.append(f"{path.name}:{profile_id}")
        for token in ("brazil", "united states", 'country ==', "if country"):
            if token in text.lower():
                offenders.append(f"{path.name}:{token}")
    # reference YAML may mention profile_hint; exclude deployments/
    assert offenders == [], offenders


def test_profiles_must_not_import_runtime():
    for path in _python_files(PROFILES):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("runtime"), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("runtime"), path


def test_primitives_must_not_import_runtime():
    for path in _python_files(PRIMITIVES):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("runtime"), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("runtime"), path


def test_profile_yaml_has_no_plugin_implementation_keys():
    forbidden = (
        "rf_plugin",
        "terrain_provider",
        "protocol_adapter",
        "provider_class",
        "python_module",
        "entry_point",
    )
    for path in (PROFILES / "definitions").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        for key in forbidden:
            assert f"{key}:" not in text, f"{path.name} contains {key}"
