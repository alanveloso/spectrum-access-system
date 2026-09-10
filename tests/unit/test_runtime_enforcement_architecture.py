"""Architecture guards for runtime composition enforcement."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Operational consumers must not rediscover plugins or construct protocol adapters.
_CONSUMER_ROOTS = (
    REPO / "routes",
    REPO / "services",
)

# Modules allowed to call compose_runtime / PluginRegistry.from_discovery.
_COMPOSITION_BOOTSTRAP_ALLOWLIST = frozenset(
    {
        "main.py",
        "celery_app.py",
        "runtime/bootstrap.py",
        "runtime/composition.py",
        "runtime/doctor.py",
        "runtime/registry.py",
        "tools/profile_doctor.py",
        "tools/doctor.py",
    }
)

_FORBIDDEN_CALLS = {
    "AdapterDiscovery": {"routes", "services"},
    "DataProviderDiscovery": {"routes", "services"},
    "RfModelDiscovery": {"routes", "services"},
    "WinnForumRestProtocolAdapter": {"routes", "services"},
}

# Allowed residual references (import for constants / type docs only).
_ALLOWLIST = {
    (REPO / "services" / "cbsd_version.py", "WinnForumRestProtocolAdapter"),  # removed
    (REPO / "services" / "iap" / "coupling.py", "_configured_iap_path_loss_model"),
    (REPO / "services" / "border_protection.py", "_configured_bpr_path_loss_model"),
}


def _iter_py(root: Path):
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py" and path.stat().st_size == 0:
            continue
        yield path


def _is_bootstrap_owner(path: Path) -> bool:
    rel = path.relative_to(REPO).as_posix()
    return rel in _COMPOSITION_BOOTSTRAP_ALLOWLIST


def test_routes_and_services_do_not_construct_winnforum_adapter():
    offenders: list[str] = []
    for root in _CONSUMER_ROOTS:
        for path in _iter_py(root):
            text = path.read_text(encoding="utf-8")
            if "WinnForumRestProtocolAdapter(" in text:
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], offenders


def test_routes_and_services_do_not_rediscover_plugins():
    offenders: list[str] = []
    needles = (
        "AdapterDiscovery(",
        "DataProviderDiscovery(",
        "RfModelDiscovery(",
        "entry_points(",
        "PluginRegistry.from_discovery(",
        "compose_runtime(",
    )
    for root in _CONSUMER_ROOTS:
        for path in _iter_py(root):
            if _is_bootstrap_owner(path):
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO)}:{needle}")
    assert offenders == [], offenders


def test_compose_runtime_only_in_bootstrap_modules():
    """Business/runtime consumers cannot construct RuntimeComposition."""
    offenders: list[str] = []
    scan_roots = (
        REPO / "routes",
        REPO / "services",
        REPO / "models",
        REPO / "adapters",
        REPO / "providers",
        REPO / "rf",
        REPO / "primitives",
    )
    for root in scan_roots:
        if not root.is_dir():
            continue
        for path in _iter_py(root):
            if _is_bootstrap_owner(path):
                continue
            text = path.read_text(encoding="utf-8")
            if "compose_runtime(" in text or "PluginRegistry.from_discovery(" in text:
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], offenders


def test_services_do_not_read_path_loss_settings_for_selection():
    """Operational selection must not call get_settings for path-loss model."""
    offenders: list[str] = []
    for path in _iter_py(REPO / "services"):
        if path.name in {"coupling.py", "border_protection.py"}:
            # Obsolete helpers may remain for migration diagnostics but must not
            # be invoked from production coupling/BPR paths (tested behaviorally).
            continue
        text = path.read_text(encoding="utf-8")
        if "sas_iap_path_loss_model" in text or "sas_bpr_path_loss_model" in text:
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], offenders


def test_profile_yaml_still_has_no_plugin_keys():
    forbidden = (
        "rf_plugin:",
        "terrain_provider:",
        "protocol_adapter:",
        "provider_class:",
        "python_module:",
        "entry_point:",
    )
    for path in (REPO / "profiles" / "definitions").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        for key in forbidden:
            assert key not in text, f"{path.name} contains {key}"
