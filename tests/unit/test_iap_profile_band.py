"""IAP operating band from the canonical Profile."""

from __future__ import annotations

import pytest

from services.iap.aggregate import resolve_iap_band_origin_hz
from services.iap.protection_points import (
    ProtectionEntityError,
    cbrs_band_hz,
    clip_frequency_to_cbrs,
)
from profiles.errors import ProfileNotFoundError
from profiles.selection import clear_profile_override
from profiles import (
    load_profile,
    parse_profile_document,
    primary_spectrum_range,
    set_active_profile_document,
)


@pytest.fixture(autouse=True)
def _reset_profile():
    clear_profile_override()
    yield
    clear_profile_override()


def _cbrs_payload() -> dict:
    return load_profile("cbrs_winnforum").model_dump(mode="json", exclude_none=True)


def test_cbrs_band_and_origin_from_canonical_primary() -> None:
    assert cbrs_band_hz() == (3_550_000_000, 3_700_000_000)
    assert resolve_iap_band_origin_hz() == 3_550_000_000


def test_origin_follows_primary_not_assignment_channelization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _cbrs_payload()
    for item in payload["spectrum"]["ranges"]:
        if item["id"] == "primary":
            item["low_hz"] = 3_560_000_000
    for binding in payload["protection"]["bindings"]:
        if binding["id"] == "peer_esc" and binding.get("frequency"):
            binding["frequency"]["low_hz"] = 3_560_000_000
    # Assignment origin intentionally left at 3550 MHz.
    assert payload["spectrum"]["channelization"]["origin_hz"] == 3_550_000_000
    doc = parse_profile_document(payload)
    assert primary_spectrum_range(doc).low_hz == 3_560_000_000
    assert doc.spectrum.channelization is not None
    assert doc.spectrum.channelization.origin_hz == 3_550_000_000

    monkeypatch.setattr(
        "profiles.context.get_active_profile_document",
        lambda: doc,
    )
    assert cbrs_band_hz() == (3_560_000_000, 3_700_000_000)
    assert resolve_iap_band_origin_hz() == 3_560_000_000
    assert resolve_iap_band_origin_hz() != doc.spectrum.channelization.origin_hz


@pytest.mark.parametrize(
    "profile_id",
    ("br_anatel_slp_3700", "eu_elsa", "us_tvws_15_711"),
)
def test_non_cbrs_profiles_fail_closed(profile_id: str) -> None:
    set_active_profile_document(profile_id)
    with pytest.raises(ProtectionEntityError, match="aggregate-linear-power"):
        cbrs_band_hz()


def test_missing_aggregate_linear_power_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _cbrs_payload()
    payload["protection"]["mechanisms"] = [
        m for m in payload["protection"]["mechanisms"] if m != "aggregate_linear_power"
    ]
    # Semantics require rf.required when aggregate is present; without it, keep rf.
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "profiles.context.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(ProtectionEntityError, match="aggregate-linear-power"):
        cbrs_band_hz()


def test_ambiguous_multi_range_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _cbrs_payload()
    payload["spectrum"]["ranges"] = [
        {"id": "a", "low_hz": 3_550_000_000, "high_hz": 3_600_000_000},
        {"id": "b", "low_hz": 3_600_000_000, "high_hz": 3_700_000_000},
    ]
    # peer_esc scope must fit a declared range.
    for binding in payload["protection"]["bindings"]:
        if binding["id"] == "peer_esc" and binding.get("frequency"):
            binding["frequency"] = {
                "low_hz": 3_550_000_000,
                "high_hz": 3_600_000_000,
            }
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "profiles.context.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(ProtectionEntityError, match="operating band"):
        cbrs_band_hz()


def test_single_range_without_primary_id_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _cbrs_payload()
    payload["spectrum"]["ranges"] = [
        {"id": "main", "low_hz": 3_550_000_000, "high_hz": 3_700_000_000}
    ]
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "profiles.context.get_active_profile_document",
        lambda: doc,
    )
    assert cbrs_band_hz() == (3_550_000_000, 3_700_000_000)


def test_loader_failure_fail_closed_no_numeric_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom():
        raise ProfileNotFoundError("profile missing for test")

    monkeypatch.setattr(
        "profiles.context.get_active_profile_document",
        _boom,
    )
    with pytest.raises(ProtectionEntityError, match="operating band"):
        cbrs_band_hz()


def test_clip_uses_active_band_and_out_of_band_is_none() -> None:
    assert clip_frequency_to_cbrs(3_600_000_000, 3_800_000_000) == (
        3_600_000_000,
        3_700_000_000,
    )
    assert clip_frequency_to_cbrs(3_400_000_000, 3_500_000_000) is None
