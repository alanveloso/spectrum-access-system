"""Repository-level composition: submodule source vs installed package."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

from runtime import (
    PluginRegistry,
    PluginSelection,
    UnknownPluginError,
    compose_runtime,
    load_deployment,
)
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH

REPO = Path(__file__).resolve().parents[2]
SUBMODULE = REPO / "plugins" / "spectrum-propagation"
GITMODULES = REPO / ".gitmodules"


def _production_py_files() -> list[Path]:
    roots = ("services", "routes", "rf", "runtime", "adapters", "providers", "main.py", "config.py")
    files: list[Path] = []
    for name in roots:
        path = REPO / name
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def test_gitmodules_declares_spectrum_propagation_submodule() -> None:
    assert GITMODULES.is_file()
    text = GITMODULES.read_text(encoding="utf-8")
    assert "plugins/spectrum-propagation" in text
    assert "spectrum-propagation" in text
    assert "https://github.com/alanveloso/spectrum-propagation.git" in text
    assert "git@github.com:" not in text


def test_submodule_gitlink_is_pinned_commit() -> None:
    out = subprocess.check_output(
        ["git", "submodule", "status", "plugins/spectrum-propagation"],
        cwd=REPO,
        text=True,
    ).strip()
    assert "plugins/spectrum-propagation" in out
    sha = out.split()[0].lstrip("-+U")
    assert len(sha) == 40


def test_submodule_source_tree_present_when_initialized() -> None:
    if not SUBMODULE.is_dir():
        pytest.skip("submodule not initialized in this checkout")
    assert (SUBMODULE / "spectrum_propagation" / "__init__.py").is_file()


def test_production_runtime_does_not_import_plugins_package() -> None:
    offenders: list[str] = []
    for path in _production_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "plugins" or alias.name.startswith("plugins."):
                        offenders.append(f"{path.relative_to(REPO)}:import:{alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "plugins" or node.module.startswith("plugins."):
                    offenders.append(f"{path.relative_to(REPO)}:from:{node.module}")
    assert offenders == []


def test_production_runtime_does_not_sys_path_submodule() -> None:
    offenders: list[str] = []
    needle = "plugins/spectrum-propagation"
    for path in _production_py_files():
        src = path.read_text(encoding="utf-8")
        if "sys.path" in src and needle in src:
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


def test_spectrum_propagation_importable_as_installed_package() -> None:
    spec = importlib.util.find_spec("spectrum_propagation")
    assert spec is not None and spec.origin
    import spectrum_propagation as sp

    assert sp.__version__


def test_rf_entry_points_discovered_without_submodule_path_hacks() -> None:
    reg = PluginRegistry.from_discovery()
    names = reg.rf_models.names()
    assert "free_space" in names
    assert "itm" in names
    assert reg.rf_models.load("free_space") is not None


def test_reference_deployment_composes_with_installed_propagation_package() -> None:
    from profiles import load_profile

    profile = load_profile("cbrs_winnforum")
    deployment = load_deployment(DEFAULT_DEPLOYMENT_PATH).model_copy(update={"providers": []})
    runtime = compose_runtime(
        profile,
        deployment,
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    assert runtime.rf is not None
    assert "spectrum-propagation" in runtime.rf.provenance


def test_unknown_rf_plugin_fails_closed() -> None:
    from profiles import load_profile

    profile = load_profile("cbrs_winnforum")
    base = load_deployment(DEFAULT_DEPLOYMENT_PATH)
    deployment = base.model_copy(
        update={
            "rf": PluginSelection(plugin="nonexistent_rf_backend"),
            "providers": [],
        }
    )
    with pytest.raises(UnknownPluginError):
        compose_runtime(
            profile,
            deployment,
            PluginRegistry.from_discovery(),
            require_data_plugins=False,
        )


def test_submodule_version_matches_installed_package() -> None:
    import spectrum_propagation as sp

    if not SUBMODULE.is_dir():
        pytest.skip("submodule not initialized")
    child_version = (SUBMODULE / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{sp.__version__}"' in child_version


def test_repository_lego_gitlink_only_changes_with_child_revision() -> None:
    """Documented Y SHA must match submodule gitlink (SAS core unchanged to switch)."""
    if not SUBMODULE.is_dir():
        pytest.skip("submodule not initialized")
    status = subprocess.check_output(
        ["git", "submodule", "status", "plugins/spectrum-propagation"],
        cwd=REPO,
        text=True,
    ).strip()
    gitlink_sha = status.split()[0].lstrip("-+U")
    child_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=SUBMODULE,
        text=True,
    ).strip()
    assert gitlink_sha == child_sha
