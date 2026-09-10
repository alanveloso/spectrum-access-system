"""Spectrum Profile access, power, temporal, geography — no YAML DSL."""

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


def test_optional_access_and_ordered_classes():
    omitted = parse_profile_document(_base())
    assert omitted.access is None
    doc = _base()
    doc["access"] = {
        "mechanism": "ordered_classes",
        "classes": [
            {"id": "critical", "priority": 300, "preemptible": False},
            {"id": "local", "priority": 200, "preemptible": True},
        ],
    }
    parsed = parse_profile_document(doc)
    assert parsed.access is not None
    assert len(parsed.access.classes) == 2
    empty = _base()
    empty["access"] = {"mechanism": "ordered_classes", "classes": []}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(empty)


def test_power_rule_table_closed_selectors_and_time_geo():
    doc = _base()
    doc["authorization"] = {"mechanism": "dynamic_lease", "duration_s": 300}
    doc["power"] = {
        "mechanism": "rule_table",
        "rules": [
            {
                "max_eirp_dbm": 30.0,
                "indoor_outdoor": "outdoor",
                "height_m_low": 0,
                "height_m_high": 6,
                "device_class": "handheld",
            }
        ],
    }
    doc["geography"] = {
        "mechanism": "authorized_area",
        "authorized_areas": [
            {"id": "site", "ring": [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]},
        ],
    }
    doc["temporal"] = {"reevaluation": {"mechanism": "periodic", "interval_s": 60}}
    parsed = parse_profile_document(doc)
    assert parsed.authorization is not None
    assert parsed.power is not None
    assert parsed.geography is not None
    assert parsed.temporal is not None


def test_rejects_dsl_wrong_axis_and_keeps_v1_loader():
    doc = _base()
    doc["power"] = {
        "mechanism": "rule_table",
        "rules": [{"max_eirp_dbm": 10, "expr": "eirp < 10"}],
    }
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
    doc2 = _base()
    doc2["access"] = {
        "mechanism": "dynamic_lease",
        "classes": [{"id": "a", "priority": 1, "preemptible": True}],
    }
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc2)
    doc3 = _base()
    doc3["not_a_section"] = {}
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc3)
