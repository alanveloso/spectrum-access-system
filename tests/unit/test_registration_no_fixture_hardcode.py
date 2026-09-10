"""Gate: registration must not embed harness fixture coordinates."""

from __future__ import annotations

from services import registration_service
from services.terrain import DeterministicHaatProvider, reset_haat_provider, set_haat_provider
from tests.support.repo import REPO_ROOT


def test_registration_service_has_no_fixture_coordinate_table():
    source = (REPO_ROOT / "services" / "registration_service.py").read_text(
        encoding="utf-8"
    )
    assert "_KNOWN_STREET_HAAT" not in source
    # Exact former fixture literals must not reappear in production code.
    assert "38.882162" not in source
    assert "-77.113755" not in source


def test_cat_a_haat_check_fail_closed_when_terrain_unavailable():
    """Arbitrary coords with missing terrain must not silently approve (fail closed)."""
    set_haat_provider(
        DeterministicHaatProvider(
            missing_locations={(10.0, 20.0)},
            default_norm_haat_m=None,
        )
    )
    try:
        assert (
            registration_service._cat_a_outdoor_haat_exceeds_limit(
                {
                    "latitude": 10.0,
                    "longitude": 20.0,
                    "height": 1.0,
                    "heightType": "AGL",
                }
            )
            is True
        )
    finally:
        reset_haat_provider()
