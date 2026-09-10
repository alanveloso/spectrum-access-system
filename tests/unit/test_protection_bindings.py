"""Typed protection bindings on the canonical Profile document."""

from __future__ import annotations

import copy

import pytest

from profiles.errors import ProfileValidationError
from profiles.context import profile_hash
from profiles.parse import load_profile, parse_profile_document
from profiles.schema import DistanceExclusionBinding


def _cbrs() -> object:
    return load_profile("cbrs_winnforum")


def _binding_by_id(doc: object, binding_id: str) -> DistanceExclusionBinding:
    assert doc.protection is not None
    for item in doc.protection.bindings:
        if item.id == binding_id:
            return item
    raise AssertionError(f"missing binding {binding_id!r}")


def _minimal_with_spectrum(
    *,
    low_hz: int = 3_550_000_000,
    high_hz: int = 3_700_000_000,
) -> dict:
    return {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "example", "version": "1.0.0", "status": "custom"},
        "spectrum": {
            "ranges": [{"id": "main", "low_hz": low_hz, "high_hz": high_hz}]
        },
        "protection": {
            "mechanisms": ["distance_exclusion"],
            "bindings": [],
        },
    }


def test_canonical_cbrs_loads_with_peer_bindings() -> None:
    doc = _cbrs()
    assert doc.metadata.id == "cbrs_winnforum"
    assert doc.protection is not None
    assert len(doc.protection.bindings) == 2

    esc = _binding_by_id(doc, "peer_esc")
    assert esc.mechanism == "distance_exclusion"
    assert esc.distance_m == 40_000
    assert esc.frequency is not None
    assert esc.frequency.low_hz == 3_550_000_000
    assert esc.frequency.high_hz == 3_700_000_000

    ppa = _binding_by_id(doc, "peer_ppa")
    assert ppa.mechanism == "distance_exclusion"
    assert ppa.distance_m == 1000
    assert ppa.frequency is None


def test_invalid_distance_and_extra_field_fail_closed() -> None:
    for bad in (0, -1.0):
        doc = _minimal_with_spectrum()
        doc["protection"]["bindings"] = [
            {
                "id": "standoff",
                "mechanism": "distance_exclusion",
                "distance_m": bad,
            }
        ]
        with pytest.raises(ProfileValidationError):
            parse_profile_document(doc)

    doc = _minimal_with_spectrum()
    doc["protection"]["bindings"] = [
        {
            "id": "standoff",
            "mechanism": "distance_exclusion",
            "distance_m": 1000,
            "radius_m": 1000,
        }
    ]
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)


def test_duplicate_id_and_undeclared_mechanism_fail_closed() -> None:
    doc = _minimal_with_spectrum()
    doc["protection"]["bindings"] = [
        {"id": "dup", "mechanism": "distance_exclusion", "distance_m": 10},
        {"id": "dup", "mechanism": "distance_exclusion", "distance_m": 20},
    ]
    with pytest.raises(ProfileValidationError, match="unique"):
        parse_profile_document(doc)

    doc2 = _minimal_with_spectrum()
    doc2["protection"]["mechanisms"] = ["protection_entitlement"]
    doc2["protection"]["bindings"] = [
        {"id": "standoff", "mechanism": "distance_exclusion", "distance_m": 10}
    ]
    with pytest.raises(ProfileValidationError, match="not listed"):
        parse_profile_document(doc2)


def test_unknown_or_wrong_binding_mechanism_fail_closed() -> None:
    doc = _minimal_with_spectrum()
    doc["protection"]["mechanisms"] = ["distance_exclusion", "channel_exclusion"]
    doc["protection"]["bindings"] = [
        {"id": "bad", "mechanism": "channel_exclusion", "distance_m": 10}
    ]
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)


def test_invalid_and_out_of_spectrum_frequency_fail_closed() -> None:
    doc = _minimal_with_spectrum()
    doc["protection"]["bindings"] = [
        {
            "id": "standoff",
            "mechanism": "distance_exclusion",
            "distance_m": 10,
            "frequency": {"low_hz": 2000, "high_hz": 1000},
        }
    ]
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)

    outside = _minimal_with_spectrum(low_hz=3_550_000_000, high_hz=3_700_000_000)
    outside["protection"]["bindings"] = [
        {
            "id": "standoff",
            "mechanism": "distance_exclusion",
            "distance_m": 10,
            "frequency": {"low_hz": 3_400_000_000, "high_hz": 3_500_000_000},
        }
    ]
    with pytest.raises(ProfileValidationError, match="not fully contained"):
        parse_profile_document(outside)

    partial = _minimal_with_spectrum(low_hz=3_550_000_000, high_hz=3_700_000_000)
    partial["protection"]["bindings"] = [
        {
            "id": "standoff",
            "mechanism": "distance_exclusion",
            "distance_m": 10,
            "frequency": {"low_hz": 3_600_000_000, "high_hz": 3_800_000_000},
        }
    ]
    with pytest.raises(ProfileValidationError, match="not fully contained"):
        parse_profile_document(partial)


def test_contained_frequency_scope_passes() -> None:
    doc = _minimal_with_spectrum(low_hz=3_550_000_000, high_hz=3_700_000_000)
    doc["protection"]["bindings"] = [
        {
            "id": "standoff",
            "mechanism": "distance_exclusion",
            "distance_m": 10,
            "frequency": {"low_hz": 3_560_000_000, "high_hz": 3_690_000_000},
        }
    ]
    parsed = parse_profile_document(doc)
    assert parsed.protection is not None
    assert parsed.protection.bindings[0].frequency is not None
    assert parsed.protection.bindings[0].frequency.low_hz == 3_560_000_000


def test_other_reference_profiles_still_load() -> None:
    for profile_id in ("br_anatel_slp_3700", "eu_elsa", "us_tvws_15_711"):
        parsed = load_profile(profile_id)
        assert parsed.metadata.id == profile_id
        if parsed.protection is not None:
            assert parsed.protection.bindings == ()


def test_cbrs_hash_deterministic_across_repeated_loads() -> None:
    first = profile_hash(load_profile("cbrs_winnforum"))
    again = [profile_hash(load_profile("cbrs_winnforum")) for _ in range(5)]
    assert set(again) == {first}
    # Bindings are part of the hashed document payload.
    payload = load_profile("cbrs_winnforum").model_dump(mode="json", exclude_none=True)
    stripped = copy.deepcopy(payload)
    stripped["protection"]["bindings"] = []
    without_bindings = profile_hash(parse_profile_document(stripped))
    assert first != without_bindings
