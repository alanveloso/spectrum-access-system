"""semantic combination and capability checks before runtime."""

from __future__ import annotations

import pytest

from profiles.errors import ProfileValidationError
from profiles.parse import parse_profile_document


def _base() -> dict:
    return {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "example", "version": "1.0.0", "status": "custom"},
        "spectrum": {"ranges": [{"id": "main", "low_hz": 1000, "high_hz": 2000}]},
    }


def test_no_rf_profile_is_valid_without_threshold_protection():
    doc = _base()
    doc["protection"] = {"mechanisms": ["channel_exclusion", "exclusion_zone"]}
    parsed = parse_profile_document(doc)
    assert parsed.rf is None


def test_rf_required_needs_terrain_and_geolocation():
    doc = _base()
    doc["rf"] = {
        "required": True,
        "policy": "path_loss_plus_aggregate",
        "propagation_model": "path_loss",
    }
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
    doc["data"] = {"required_capabilities": ["terrain"]}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
    doc["requirements"] = {"device_capabilities": ["geolocation"]}
    parsed = parse_profile_document(doc)
    assert parsed.rf is not None


def test_aggregate_protection_requires_rf_and_policy_slot():
    doc = _base()
    doc["protection"] = {"mechanisms": ["aggregate_linear_power"]}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
    swapped = _base()
    swapped["rf"] = {
        "required": True,
        "policy": "path_loss",
        "propagation_model": "path_loss_plus_aggregate",
    }
    swapped["data"] = {"required_capabilities": ["terrain"]}
    swapped["requirements"] = {"device_capabilities": ["geolocation"]}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(swapped)
