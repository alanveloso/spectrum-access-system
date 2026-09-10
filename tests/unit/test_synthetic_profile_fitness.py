"""Synthetic Profile Fitness Test.

Architecture question: can a Spectrum Profile with a capability combination that
no builtin profile expresses be loaded, validated, have its requirements derived
and be composed into a runtime — using only installed plugins and without any
production change made for that profile?

The synthetic profile is a test fixture (``tests/fixtures/profiles``). It is not
a regulatory regime and must never move into ``profiles/definitions``.
"""

from __future__ import annotations

import pytest

from profiles import load_profile
from profiles.schema import DataSection
from runtime import (
    DeploymentConfig,
    MissingCapabilityError,
    PluginRegistry,
    PluginSelection,
    compose_runtime,
    required_capabilities,
)
from tests.fixtures.synthetic_profile import (
    SYNTHETIC_PROFILE_ID,
    synthetic_deployment,
    synthetic_profile,
)

_BUILTIN_IDS = (
    "cbrs_winnforum",
    "br_anatel_slp_3700",
    "eu_elsa",
    "us_tvws_15_711",
)

# Plugin names that only exist to serve the CBRS/WInnForum reference regime.
_CBRS_ONLY_PLUGINS = frozenset(
    {"cbsd", "winnforum_rest", "free_space", "itm", "independent_fspl"}
)


def _registry() -> PluginRegistry:
    """Live discovery — installed distributions only, no test overlays."""
    return PluginRegistry.from_discovery()


def test_synthetic_profile_loads_and_validates():
    profile = synthetic_profile()
    assert profile.metadata.id == SYNTHETIC_PROFILE_ID
    assert profile.metadata.status == "custom"
    # Deliberately absent sections.
    assert profile.rf is None
    assert profile.protection is None
    assert profile.coordination is None
    assert profile.data is None
    assert profile.access is None


def test_synthetic_requirements_are_network_only():
    requirements = required_capabilities(synthetic_profile())
    assert requirements.network_capabilities == frozenset(
        {"network_identity", "frequency_range"}
    )
    assert requirements.device_capabilities == frozenset()
    assert requirements.data_capabilities == frozenset()
    assert requirements.rf_required is False
    assert requirements.as_sorted_tokens() == (
        "network.frequency_range",
        "network.network_identity",
    )


def test_synthetic_capability_combination_is_novel():
    """Guards against overfitting the generalization to the four builtin profiles."""
    synthetic = required_capabilities(synthetic_profile())
    for profile_id in _BUILTIN_IDS:
        builtin = required_capabilities(load_profile(profile_id))
        assert synthetic != builtin, f"synthetic profile mirrors {profile_id}"
        assert synthetic.as_sorted_tokens() != builtin.as_sorted_tokens()
    # No builtin omits the rf/protection/coordination sections entirely.
    for profile_id in _BUILTIN_IDS:
        document = load_profile(profile_id)
        assert document.rf is not None
        assert document.protection is not None
        assert document.coordination is not None


def test_synthetic_deployment_composes_with_installed_plugins():
    composition = compose_runtime(
        synthetic_profile(), synthetic_deployment(), _registry()
    )
    assert composition.network_adapter is not None
    assert composition.provenance.profile_id == SYNTHETIC_PROFILE_ID
    assert composition.provenance.selected_plugins == {"network": "managed"}
    assert composition.provenance.unsatisfied_data_capabilities == ()


def test_synthetic_composition_requires_no_cbrs_dependency():
    composition = compose_runtime(
        synthetic_profile(), synthetic_deployment(), _registry()
    )
    assert composition.device_adapter is None
    assert composition.protocol_adapter is None
    assert composition.rf is None
    assert composition.providers == ()
    selected = set(composition.provenance.selected_plugins.values())
    assert not selected & _CBRS_ONLY_PLUGINS


def test_synthetic_composition_still_fails_closed():
    """Optionality comes from the profile, not from relaxing the composer."""
    profile = synthetic_profile()
    with pytest.raises(MissingCapabilityError, match="network"):
        compose_runtime(
            profile, DeploymentConfig(id="empty_lab"), _registry()
        )


def test_synthetic_network_plugin_must_advertise_declared_capabilities():
    profile = synthetic_profile()
    deployment = synthetic_deployment().model_copy(
        update={"network": PluginSelection(plugin="mapping")}
    )
    # `mapping` advertises managed_area/frequency_range/max_eirp but not
    # network_identity, so composition must reject it.
    with pytest.raises(Exception) as excinfo:
        compose_runtime(profile, deployment, _registry())
    assert "network_identity" in str(excinfo.value)


def test_synthetic_profile_with_one_data_capability_resolves_generic_provider():
    """A data capability is satisfied by an installed provider, not by a bundle."""
    profile = synthetic_profile().model_copy(
        update={"data": DataSection(required_capabilities=("boundaries",))}
    )
    deployment = synthetic_deployment().model_copy(
        update={"providers": (PluginSelection(plugin="bundle_boundaries"),)}
    )
    composition = compose_runtime(
        profile, deployment, _registry(), require_data_plugins=True
    )
    assert composition.provenance.unsatisfied_data_capabilities == ()
    assert "boundaries" in composition.provider_for("boundaries").advertised_capabilities()
    assert composition.rf is None
    assert composition.device_adapter is None


def test_synthetic_profile_is_not_a_builtin_definition():
    """Adding the fixture must not have required touching the production tree."""
    from profiles.trust import builtin_profiles_dir

    ids = {path.stem for path in builtin_profiles_dir().glob("*.yaml")}
    assert SYNTHETIC_PROFILE_ID not in ids
    assert ids == set(_BUILTIN_IDS)
