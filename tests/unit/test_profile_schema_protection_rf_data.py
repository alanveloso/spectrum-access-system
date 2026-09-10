"""protection, coordination, RF, data, and capability requirements."""

from __future__ import annotations

import pytest

from primitives.registry import MechanismAxis, MechanismContract, builtin_mechanism_registry
from profiles.errors import ProfileValidationError
from profiles.parse import parse_profile_document


def _base() -> dict:
    return {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "example", "version": "1.0.0", "status": "custom"},
        "spectrum": {"ranges": [{"id": "main", "low_hz": 1000, "high_hz": 2000}]},
    }


def test_composes_registered_protection_coordination_rf_and_data():
    doc = _base()
    doc["protection"] = {
        "mechanisms": [
            "protection_entitlement",
            "exclusion_zone",
            "aggregate_linear_power",
        ]
    }
    doc["coordination"] = {"mechanism": "snapshot_evaluate_apply"}
    doc["rf"] = {
        "required": True,
        "policy": "path_loss_plus_aggregate",
        "propagation_model": "path_loss",
    }
    doc["data"] = {"required_capabilities": ["terrain", "protected_entities"]}
    doc["requirements"] = {
        "device_capabilities": ["geolocation", "frequency_range", "max_eirp"]
    }
    parsed = parse_profile_document(doc)
    assert parsed.protection is not None
    assert parsed.coordination is not None
    assert parsed.rf is not None
    assert parsed.rf.required is True
    assert parsed.data is not None
    assert parsed.requirements is not None


def test_rf_required_without_model_and_unknown_capabilities_fail_closed():
    doc = _base()
    doc["rf"] = {"required": True, "policy": "path_loss_plus_aggregate"}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
    doc2 = _base()
    doc2["data"] = {"required_capabilities": ["ned"]}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc2)
    doc3 = _base()
    doc3["rf"] = {
        "required": True,
        "policy": "path_loss_plus_aggregate",
        "propagation_model": "itm",
    }
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc3)
    extra = builtin_mechanism_registry()
    extra.register(
        MechanismContract("itm", MechanismAxis.RF, "1.0.0", slot="rf_model")
    )
    doc3["data"] = {"required_capabilities": ["terrain"]}
    doc3["requirements"] = {"device_capabilities": ["geolocation"]}
    parsed = parse_profile_document(doc3, registry=extra)
    assert parsed.rf is not None
    assert parsed.rf.propagation_model == "itm"


def test_rejects_unregistered_protection_and_keeps_v1_loader():
    doc = _base()
    doc["protection"] = {"mechanisms": ["iap_fairshare"]}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
    doc2 = _base()
    doc2["coordination"] = {"mechanism": "ordered_classes"}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc2)
    doc3 = _base()
    doc3["rf"] = {
        "required": False,
        "policy": "path_loss_plus_aggregate",
    }
    parsed = parse_profile_document(doc3)
    assert parsed.rf is not None
    assert parsed.rf.propagation_model is None
