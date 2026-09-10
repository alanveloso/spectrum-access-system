"""CPAS peer ESC/PPA parameters from the canonical Profile."""

from __future__ import annotations

import pytest

from services.cpas_service import (
    CpasRfEvaluationError,
    _peer_esc_params,
    _peer_ppa_buffer_m,
    _required_distance_exclusion_binding,
)
from profiles.selection import clear_profile_override
from profiles import (
    DistanceExclusionBinding,
    get_active_profile_document,
    load_profile,
    parse_profile_document,
    set_active_profile_document,
)


@pytest.fixture(autouse=True)
def _reset_active_profile():
    clear_profile_override()
    yield
    clear_profile_override()


def _cbrs_payload() -> dict:
    return load_profile("cbrs_winnforum").model_dump(mode="json", exclude_none=True)


def _with_bindings(bindings: list[dict]) -> object:
    payload = _cbrs_payload()
    payload["protection"]["bindings"] = bindings
    return parse_profile_document(payload)


def test_peer_esc_params_from_canonical_cbrs() -> None:
    radius_m, low_hz, high_hz = _peer_esc_params()
    assert radius_m == 40_000.0
    assert low_hz == 3_550_000_000
    assert high_hz == 3_700_000_000
    binding = _required_distance_exclusion_binding("peer_esc")
    assert isinstance(binding, DistanceExclusionBinding)
    assert binding.mechanism == "distance_exclusion"


def test_peer_ppa_buffer_from_canonical_cbrs() -> None:
    assert _peer_ppa_buffer_m() == 1_000.0
    binding = _required_distance_exclusion_binding("peer_ppa")
    assert binding.frequency is None
    assert binding.mechanism == "distance_exclusion"


def test_missing_protection_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _cbrs_payload()
    payload["protection"] = None
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "services.cpas_service.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(CpasRfEvaluationError, match="peer_esc"):
        _peer_esc_params()
    with pytest.raises(CpasRfEvaluationError, match="peer_ppa"):
        _peer_ppa_buffer_m()


def test_missing_peer_esc_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _cbrs_payload()
    payload["protection"]["bindings"] = [
        b for b in payload["protection"]["bindings"] if b["id"] != "peer_esc"
    ]
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "services.cpas_service.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(CpasRfEvaluationError, match="peer_esc"):
        _peer_esc_params()


def test_missing_peer_ppa_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _cbrs_payload()
    payload["protection"]["bindings"] = [
        b for b in payload["protection"]["bindings"] if b["id"] != "peer_ppa"
    ]
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "services.cpas_service.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(CpasRfEvaluationError, match="peer_ppa"):
        _peer_ppa_buffer_m()


def test_peer_esc_missing_frequency_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _cbrs_payload()
    for binding in payload["protection"]["bindings"]:
        if binding["id"] == "peer_esc":
            binding.pop("frequency", None)
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "services.cpas_service.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(CpasRfEvaluationError, match="frequency"):
        _peer_esc_params()


def test_peer_ppa_unexpected_frequency_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _cbrs_payload()
    for binding in payload["protection"]["bindings"]:
        if binding["id"] == "peer_ppa":
            binding["frequency"] = {
                "low_hz": 3_550_000_000,
                "high_hz": 3_700_000_000,
            }
    doc = parse_profile_document(payload)
    monkeypatch.setattr(
        "services.cpas_service.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(CpasRfEvaluationError, match="frequency scope"):
        _peer_ppa_buffer_m()


def test_non_cbrs_active_profile_fail_closed() -> None:
    set_active_profile_document("eu_elsa")
    assert get_active_profile_document().metadata.id == "eu_elsa"
    with pytest.raises(CpasRfEvaluationError, match="peer_esc"):
        _peer_esc_params()
    with pytest.raises(CpasRfEvaluationError, match="peer_ppa"):
        _peer_ppa_buffer_m()


def test_no_hardcoded_fallback_when_binding_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _with_bindings([])
    monkeypatch.setattr(
        "services.cpas_service.get_active_profile_document",
        lambda: doc,
    )
    with pytest.raises(CpasRfEvaluationError, match="peer_esc"):
        _peer_esc_params()
    with pytest.raises(CpasRfEvaluationError, match="peer_ppa"):
        _peer_ppa_buffer_m()
