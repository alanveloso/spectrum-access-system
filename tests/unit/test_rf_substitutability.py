"""Tests proving RF backend substitutability via DeploymentConfig only."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from adapters.discovery import GROUP_DEVICE_ADAPTERS, GROUP_PROTOCOL_ADAPTERS, AdapterDiscovery
from adapters.device import MappingDeviceAdapter
from adapters.protocol import GenericJsonProtocolAdapter
from adapters.winnforum_rest import winnforum_rest_protocol_adapter
from primitives.geography import GeoPoint
from profiles import load_profile
from providers.discovery import DataProviderDiscovery
from rf.cbrs_winnforum import free_space_rf_adapter
from rf.discovery import RfModelDiscovery
from rf.port import PathLossRequest
from runtime import (
    DeploymentConfig,
    PluginRegistry,
    UnknownPluginError,
    compose_runtime,
    load_deployment,
)
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH
from services.iap.rf_adapter import path_loss_db_fn_from_rf_port
from services.iap.coupling import make_production_iap_coupling
from services.iap.models import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
)

REPO = Path(__file__).resolve().parents[2]
ALT_DEPLOYMENT = (
    REPO / "tests" / "fixtures" / "deployments" / "alternate_independent_fspl.yaml"
)
PLUGIN_ROOT = REPO / "tests" / "plugin_packages" / "independent_fspl"

# Plugin is a separate installable package; for unit tests load from source tree.
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _grant_point() -> tuple[GrantRfInfo, ProtectionPoint, FrequencyChannel]:
    grant = GrantRfInfo(
        grant_id="g",
        cbsd_id="c",
        latitude=39.0,
        longitude=-77.0,
        height_m=200.0,  # tall TX accentuates slant vs 2D FSPL difference
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    point = ProtectionPoint(
        point_id="p",
        latitude=39.001,
        longitude=-77.001,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-80.0,
        entity_kind=ProtectedEntityKind.GENERIC,
    )
    channel = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_560_000_000)
    return grant, point, channel


def _registry_with_both_rf() -> PluginRegistry:
    from sas_rf_independent_fspl.adapter import independent_fspl_rf_adapter

    return PluginRegistry.from_discovery(
        adapters=AdapterDiscovery(
            overlays={
                GROUP_DEVICE_ADAPTERS: {"cbsd": MappingDeviceAdapter},
                GROUP_PROTOCOL_ADAPTERS: {
                    "winnforum_rest": winnforum_rest_protocol_adapter,
                    "generic_json": GenericJsonProtocolAdapter,
                },
            },
            list_entry_points=lambda _g: (),
        ),
        providers=DataProviderDiscovery(overlays={}, list_entry_points=lambda _g: ()),
        rf_models=RfModelDiscovery(
            overlays={
                "free_space": free_space_rf_adapter,
                "independent_fspl": independent_fspl_rf_adapter,
            },
            list_entry_points=lambda _g: (),
        ),
    )


def _registry_reference_only() -> PluginRegistry:
    return PluginRegistry.from_discovery(
        adapters=AdapterDiscovery(
            overlays={
                GROUP_DEVICE_ADAPTERS: {"cbsd": MappingDeviceAdapter},
                GROUP_PROTOCOL_ADAPTERS: {
                    "winnforum_rest": winnforum_rest_protocol_adapter,
                },
            },
            list_entry_points=lambda _g: (),
        ),
        providers=DataProviderDiscovery(overlays={}, list_entry_points=lambda _g: ()),
        rf_models=RfModelDiscovery(
            overlays={"free_space": free_space_rf_adapter},
            list_entry_points=lambda _g: (),
        ),
    )


def _deployment_without_providers(path: Path) -> DeploymentConfig:
    """RF-focused composition: drop data providers (not under test here)."""
    return load_deployment(path).model_copy(update={"providers": ()})


def test_reference_and_alternate_deployments_select_different_rf():
    profile = load_profile("cbrs_winnforum")
    registry = _registry_with_both_rf()
    dep_a = _deployment_without_providers(DEFAULT_DEPLOYMENT_PATH)
    dep_b = _deployment_without_providers(ALT_DEPLOYMENT)
    assert dep_a.rf is not None and dep_b.rf is not None
    assert dep_a.rf.plugin != dep_b.rf.plugin
    assert dep_a.protocol == dep_b.protocol
    assert dep_a.device == dep_b.device

    runtime_a = compose_runtime(
        profile, dep_a, registry, require_data_plugins=False
    )
    runtime_b = compose_runtime(
        profile, dep_b, registry, require_data_plugins=False
    )
    assert runtime_a.rf is not None and runtime_b.rf is not None
    assert runtime_a.rf is not runtime_b.rf
    assert getattr(runtime_a.rf, "provenance", "") != getattr(
        runtime_b.rf, "provenance", ""
    )
    assert runtime_a.profile.metadata.id == runtime_b.profile.metadata.id


def test_alternate_plugin_absent_fails_closed():
    profile = load_profile("cbrs_winnforum")
    dep_b = _deployment_without_providers(ALT_DEPLOYMENT)
    with pytest.raises(UnknownPluginError):
        compose_runtime(
            profile, dep_b, _registry_reference_only(), require_data_plugins=False
        )


def test_lego_iap_invokes_selected_rf_backend():
    """Same IAP call path; only composed RfPort differs."""
    profile = load_profile("cbrs_winnforum")
    registry = _registry_with_both_rf()
    runtime_a = compose_runtime(
        profile,
        _deployment_without_providers(DEFAULT_DEPLOYMENT_PATH),
        registry,
        require_data_plugins=False,
    )
    runtime_b = compose_runtime(
        profile,
        _deployment_without_providers(ALT_DEPLOYMENT),
        registry,
        require_data_plugins=False,
    )
    grant, point, channel = _grant_point()
    coupling_a = make_production_iap_coupling(rf_port=runtime_a.rf)
    coupling_b = make_production_iap_coupling(rf_port=runtime_b.rf)
    mw_a = coupling_a(grant, point, channel, 20.0)
    mw_b = coupling_b(grant, point, channel, 20.0)
    # Different FSPL identities → different interference power.
    assert mw_a != mw_b


def test_path_loss_values_differ_between_backends():
    registry = _registry_with_both_rf()
    ref = registry.load_rf("free_space")
    alt = registry.load_rf("independent_fspl")
    # Short range + tall TX → slant-range vs 2D divergence is material.
    req = PathLossRequest(
        tx=GeoPoint(latitude_deg=39.0, longitude_deg=-77.0),
        rx=GeoPoint(latitude_deg=39.001, longitude_deg=-77.001),
        tx_height_m=200.0,
        rx_height_m=1.5,
        frequency_hz=3_625_000_000,
    )
    loss_a = ref.path_loss(req).loss_db
    loss_b = alt.path_loss(req).loss_db
    assert loss_a != loss_b
    assert abs(loss_a - loss_b) > 0.5


def test_bpr_bridge_uses_composed_rf():
    registry = _registry_with_both_rf()
    alt = registry.load_rf("independent_fspl")
    from services.iap.rf_adapter import path_loss_db_between_points

    loss = path_loss_db_between_points(
        alt,
        tx_lat=39.0,
        tx_lon=-77.0,
        tx_height_m=10.0,
        rx_lat=39.01,
        rx_lon=-77.01,
        rx_height_m=1.5,
        frequency_hz=3_625_000_000,
    )
    assert math.isfinite(loss)


def test_no_environment_override_after_composition(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_IAP_PATH_LOSS_MODEL", "itm")
    from config import clear_settings_cache

    clear_settings_cache()
    profile = load_profile("cbrs_winnforum")
    runtime = compose_runtime(
        profile,
        _deployment_without_providers(ALT_DEPLOYMENT),
        _registry_with_both_rf(),
        require_data_plugins=False,
    )
    assert runtime.rf is not None
    assert runtime.rf.provenance == "independent-fspl:v1"
    grant, point, channel = _grant_point()
    # Bound to alternate RF explicitly — Settings ITM must not win.
    make_production_iap_coupling(rf_port=runtime.rf)
    fn = path_loss_db_fn_from_rf_port(runtime.rf)
    assert fn(grant, point, channel) == runtime.rf.path_loss(
        PathLossRequest(
            tx=GeoPoint(latitude_deg=grant.latitude, longitude_deg=grant.longitude),
            rx=GeoPoint(latitude_deg=point.latitude, longitude_deg=point.longitude),
            tx_height_m=grant.height_m,
            rx_height_m=1.5,
            frequency_hz=max((grant.low_hz + grant.high_hz) // 2, 1),
            indoor=grant.indoor,
            tx_height_is_agl=grant.height_is_agl,
        )
    ).loss_db
    clear_settings_cache()


def test_alternate_plugin_package_exists_outside_sas_package_tree():
    assert PLUGIN_ROOT.is_dir()
    assert (PLUGIN_ROOT / "pyproject.toml").is_file()
    # Must not be imported as part of the main `rf` package.
    import rf

    assert not hasattr(rf, "independent_fspl")


def test_core_switch_a_to_b_is_deployment_only():
    """Documented invariant: switching RF is deployment YAML + plugin install."""
    a = DEFAULT_DEPLOYMENT_PATH.read_text(encoding="utf-8")
    b = ALT_DEPLOYMENT.read_text(encoding="utf-8")
    assert "plugin: free_space" in a
    assert "plugin: independent_fspl" in b
    # Same non-RF selections.
    assert "plugin: winnforum_rest" in a and "plugin: winnforum_rest" in b
    assert "plugin: cbsd" in a and "plugin: cbsd" in b
