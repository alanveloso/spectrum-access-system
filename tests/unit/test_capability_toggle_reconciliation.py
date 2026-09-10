"""Capability / toggle reconciliation: Profile requirements vs operator disables."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config import clear_settings_cache, get_settings
from profiles import load_profile
from runtime.toggle_reconciliation import (
    IAP_PROTECTION_MECHANISM,
    RequiredCapabilityDisabledError,
    SAS_IAP_ENABLED_SETTING,
    profile_requires_mechanism,
    reconcile_operational_toggles,
)
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile


REPO = Path(__file__).resolve().parents[2]


def test_cbrs_requires_aggregate_linear_power():
    profile = load_profile("cbrs_winnforum")
    assert profile_requires_mechanism(profile, IAP_PROTECTION_MECHANISM)


def test_synthetic_does_not_require_aggregate_linear_power():
    profile = synthetic_profile()
    assert not profile_requires_mechanism(profile, IAP_PROTECTION_MECHANISM)


def test_cbrs_iap_enabled_passes():
    profile = load_profile("cbrs_winnforum")
    reconcile_operational_toggles(profile, SimpleNamespace(sas_iap_enabled=True))


def test_cbrs_iap_disabled_fails_closed():
    profile = load_profile("cbrs_winnforum")
    with pytest.raises(RequiredCapabilityDisabledError) as excinfo:
        reconcile_operational_toggles(
            profile, SimpleNamespace(sas_iap_enabled=False)
        )
    message = str(excinfo.value)
    assert IAP_PROTECTION_MECHANISM in message
    assert SAS_IAP_ENABLED_SETTING in message
    assert excinfo.value.mechanism_id == IAP_PROTECTION_MECHANISM
    assert excinfo.value.setting == SAS_IAP_ENABLED_SETTING


def test_synthetic_iap_disabled_passes():
    profile = synthetic_profile()
    reconcile_operational_toggles(profile, SimpleNamespace(sas_iap_enabled=False))


def test_unset_default_iap_enabled_is_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SAS_IAP_ENABLED", raising=False)
    clear_settings_cache()
    assert get_settings().sas_iap_enabled is True
    reconcile_operational_toggles(load_profile("cbrs_winnforum"), get_settings())


def test_request_path_iap_enabled_raises_under_cbrs_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """Bound CBRS composition + disabled toggle must not silently skip IAP."""
    from runtime.context import runtime_composition_scope
    from runtime import PluginRegistry, compose_runtime, load_deployment
    from runtime.deployment import DEFAULT_DEPLOYMENT_PATH
    from services.iap.coupling import iap_enabled

    monkeypatch.setenv("SAS_IAP_ENABLED", "false")
    clear_settings_cache()
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    with runtime_composition_scope(composition):
        with pytest.raises(RequiredCapabilityDisabledError):
            iap_enabled()
    monkeypatch.delenv("SAS_IAP_ENABLED", raising=False)
    clear_settings_cache()


def test_request_path_iap_disabled_allowed_for_synthetic(
    monkeypatch: pytest.MonkeyPatch,
):
    from runtime.context import runtime_composition_scope
    from runtime import PluginRegistry, compose_runtime
    from services.iap.coupling import iap_enabled

    monkeypatch.setenv("SAS_IAP_ENABLED", "false")
    clear_settings_cache()
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    with runtime_composition_scope(composition):
        assert iap_enabled() is False
    monkeypatch.delenv("SAS_IAP_ENABLED", raising=False)
    clear_settings_cache()


def test_startup_reconciles_before_composition(monkeypatch: pytest.MonkeyPatch):
    """Smoke: on_startup path calls reconcile (CBRS + disabled → raises)."""
    from config import clear_settings_cache
    import main as main_mod

    monkeypatch.setenv("SAS_IAP_ENABLED", "false")
    clear_settings_cache()
    # Active profile in tests is cbrs via selection default / fixtures.
    from profiles.selection import set_profile_override

    set_profile_override("cbrs_winnforum")
    with pytest.raises(RequiredCapabilityDisabledError):
        main_mod.on_startup()
    monkeypatch.delenv("SAS_IAP_ENABLED", raising=False)
    clear_settings_cache()
