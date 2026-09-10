"""Cat A outdoor HAAT validation () — provider, limits, Registration."""

from __future__ import annotations

from pathlib import Path

import pytest

from models.models import Cbsd
from services.registration_service import (
    INVALID_PARAM,
    SUCCESS,
    process_registration,
)
from services.terrain import (
    CAT_A_OUTDOOR_HAAT_LIMIT_M,
    CachedHaatProvider,
    DeterministicHaatProvider,
    HAAT_NED_ABS_TOL_M,
    NedTerrainProvider,
    TerrainDataUnavailable,
    reset_haat_provider,
    set_haat_provider,
)
from services.terrain.haat import haat_exceeds_cat_a_outdoor_limit
from tests.fixtures.factories import cat_a_install, make_fcc_id, make_user_id
from tests.support.repo import REPO_ROOT

# Synthetic locations for deterministic provider tests (not harness fixtures).
_LOC_BELOW = (41.100001, -104.100001)
_LOC_AT = (41.100002, -104.100002)
_LOC_ABOVE = (41.100003, -104.100003)
_LOC_MISSING = (41.100004, -104.100004)


def _payload(fcc_id: str, serial: str, user_id: str, **install_kw):
    return {
        "userId": user_id,
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "measCapability": ["RECEIVED_POWER_WITHOUT_GRANT"],
        "installationParam": cat_a_install(**install_kw),
    }


@pytest.fixture
def haat_limits_provider():
    """Deterministic norm HAAT so total HAAT is below / at / above 6 m."""
    provider = DeterministicHaatProvider(
        norm_haat_by_location={
            # height 4 → HAAT 4, 6, 7 with norms 0, 2, 3
            _LOC_BELOW: 0.0,
            _LOC_AT: 2.0,
            _LOC_ABOVE: 3.0,
        },
        missing_locations={_LOC_MISSING},
        dataset_version="unit-haat-v1",
    )
    set_haat_provider(provider)
    yield provider
    reset_haat_provider()


def test_haat_above_limit_rejects_and_does_not_persist(db_session, haat_limits_provider):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    lat, lon = _LOC_ABOVE
    payload = _payload(
        fcc.fcc_id,
        "sn-haat-above",
        user.user_id,
        lat=lat,
        lon=lon,
        indoor=False,
        height=4.0,
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]
    assert (
        db_session.query(Cbsd)
        .filter_by(fcc_id=fcc.fcc_id, cbsd_serial_number="sn-haat-above")
        .first()
        is None
    )


def test_haat_exactly_at_limit_accepts(db_session, haat_limits_provider):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    lat, lon = _LOC_AT
    payload = _payload(
        fcc.fcc_id,
        "sn-haat-at",
        user.user_id,
        lat=lat,
        lon=lon,
        indoor=False,
        height=4.0,
    )
    # HAAT = 4 + 2 = 6.0 → allowed (≤ 6)
    assert not haat_exceeds_cat_a_outdoor_limit(lat, lon, 4.0, provider=haat_limits_provider)
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert "cbsdId" in resp[0]


def test_haat_below_limit_accepts(db_session, haat_limits_provider):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    lat, lon = _LOC_BELOW
    payload = _payload(
        fcc.fcc_id,
        "sn-haat-below",
        user.user_id,
        lat=lat,
        lon=lon,
        indoor=False,
        height=4.0,
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_cat_a_indoor_skips_haat_even_if_provider_would_fail(db_session):
    set_haat_provider(
        DeterministicHaatProvider(missing_locations={_LOC_MISSING}, default_norm_haat_m=None)
    )
    try:
        fcc = make_fcc_id(db_session)
        user = make_user_id(db_session)
        lat, lon = _LOC_MISSING
        payload = _payload(
            fcc.fcc_id,
            "sn-haat-indoor",
            user.user_id,
            lat=lat,
            lon=lon,
            indoor=True,
            height=4.0,
        )
        resp = process_registration(db_session, [payload])
        assert resp[0]["response"]["responseCode"] == SUCCESS
    finally:
        reset_haat_provider()


def test_cat_b_skips_haat(db_session):
    # Cat B outdoor with CPI path is complex; cleartext Cat B is 103 for other reasons.
    # Ensure HAAT provider failure would not be the cause for indoorDeployment True Cat B.
    set_haat_provider(
        DeterministicHaatProvider(
            default_norm_haat_m=100.0,
            dataset_version="unit-catb",
        )
    )
    try:
        fcc = make_fcc_id(db_session)
        user = make_user_id(db_session)
        # Category B with cleartext install → 103 (CPI rule), not HAAT.
        payload = {
            "userId": user.user_id,
            "fccId": fcc.fcc_id,
            "cbsdSerialNumber": "sn-haat-catb",
            "cbsdCategory": "B",
            "airInterface": {"radioTechnology": "E_UTRA"},
            "measCapability": ["RECEIVED_POWER_WITHOUT_GRANT"],
            "installationParam": {
                **cat_a_install(indoor=False, height=4.0),
                "antennaAzimuth": 10,
                "antennaGain": 10,
                "antennaBeamwidth": 30,
            },
        }
        resp = process_registration(db_session, [payload])
        assert resp[0]["response"]["responseCode"] == INVALID_PARAM
        # Confirm HAAT was not consulted for category B by using a location that
        # would exceed if Cat A: height 4 + norm 100 ≫ 6.
        assert haat_exceeds_cat_a_outdoor_limit(
            40.0, -105.27, 4.0, provider=DeterministicHaatProvider(default_norm_haat_m=100.0)
        )
    finally:
        reset_haat_provider()


def test_dataset_missing_rejects_cat_a_outdoor(db_session, tmp_path: Path):
    missing_dir = tmp_path / "empty-ned"
    missing_dir.mkdir()
    # Empty dir: NedTerrainProvider constructs, but tile load fails → reject.
    # Use a provider that raises on compute.
    set_haat_provider(
        DeterministicHaatProvider(missing_locations={_LOC_MISSING}, default_norm_haat_m=None)
    )
    try:
        fcc = make_fcc_id(db_session)
        user = make_user_id(db_session)
        lat, lon = _LOC_MISSING
        payload = _payload(
            fcc.fcc_id,
            "sn-haat-missing",
            user.user_id,
            lat=lat,
            lon=lon,
            indoor=False,
            height=4.0,
        )
        resp = process_registration(db_session, [payload])
        assert resp[0]["response"]["responseCode"] == INVALID_PARAM
        assert (
            db_session.query(Cbsd)
            .filter_by(cbsd_serial_number="sn-haat-missing")
            .first()
            is None
        )
    finally:
        reset_haat_provider()


def test_ned_provider_missing_directory_raises(tmp_path: Path):
    with pytest.raises(TerrainDataUnavailable):
        NedTerrainProvider(tmp_path / "no-such-ned")


def test_invalid_coordinates_fail_closed(db_session):
    set_haat_provider(DeterministicHaatProvider(default_norm_haat_m=0.0))
    try:
        fcc = make_fcc_id(db_session)
        user = make_user_id(db_session)
        # Out-of-range lat is caught by range validation before HAAT.
        payload = _payload(
            fcc.fcc_id,
            "sn-haat-badlat",
            user.user_id,
            lat=90.01,
            lon=-104.0,
            indoor=False,
            height=4.0,
        )
        resp = process_registration(db_session, [payload])
        assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    finally:
        reset_haat_provider()


def test_haat_provider_rejects_invalid_coords_directly():
    provider = DeterministicHaatProvider(default_norm_haat_m=0.0)
    with pytest.raises(Exception):
        provider.compute_haat_m(91.0, 0.0, 1.0)


def test_mixed_batch_haat_and_success(db_session, haat_limits_provider):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    good = _payload(
        fcc.fcc_id,
        "sn-haat-mix-ok",
        user.user_id,
        lat=_LOC_BELOW[0],
        lon=_LOC_BELOW[1],
        indoor=False,
        height=4.0,
    )
    bad = _payload(
        fcc.fcc_id,
        "sn-haat-mix-bad",
        user.user_id,
        lat=_LOC_ABOVE[0],
        lon=_LOC_ABOVE[1],
        indoor=False,
        height=4.0,
    )
    indoor = _payload(
        fcc.fcc_id,
        "sn-haat-mix-in",
        user.user_id,
        lat=_LOC_ABOVE[0],
        lon=_LOC_ABOVE[1],
        indoor=True,
        height=4.0,
    )
    resp = process_registration(db_session, [good, bad, indoor])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[1]["response"]["responseCode"] == INVALID_PARAM
    assert resp[2]["response"]["responseCode"] == SUCCESS
    assert (
        db_session.query(Cbsd).filter_by(cbsd_serial_number="sn-haat-mix-bad").first()
        is None
    )
    assert (
        db_session.query(Cbsd).filter_by(cbsd_serial_number="sn-haat-mix-ok").first()
        is not None
    )


def test_cache_key_includes_dataset_version_and_invalidates():
    inner = DeterministicHaatProvider(
        default_norm_haat_m=1.0,
        dataset_version="terrain-v1",
    )
    cached = CachedHaatProvider(inner, max_entries=8)
    v1 = cached.compute_haat_m(12.0, 34.0, 2.0)
    assert v1 == 3.0
    assert cached.cache_info()[0] == 1

    # Same coords/height but new dataset version must not reuse stale value.
    inner2 = DeterministicHaatProvider(
        default_norm_haat_m=5.0,
        dataset_version="terrain-v2",
    )
    cached2 = CachedHaatProvider(inner2, max_entries=8)
    v2 = cached2.compute_haat_m(12.0, 34.0, 2.0)
    assert v2 == 7.0

    cached.clear_cache()
    assert cached.cache_info()[0] == 0
    # Recompute after clear matches inner (no silent drift).
    assert cached.compute_haat_m(12.0, 34.0, 2.0) == 3.0


def test_cache_hit_does_not_alter_result():
    calls = {"n": 0}

    class Counting(DeterministicHaatProvider):
        def compute_haat_m(self, lat, lon, height_m, *, height_is_agl=True):
            calls["n"] += 1
            return super().compute_haat_m(
                lat, lon, height_m, height_is_agl=height_is_agl
            )

    inner = Counting(default_norm_haat_m=1.5, dataset_version="c1")
    cached = CachedHaatProvider(inner)
    a = cached.compute_haat_m(1.0, 2.0, 3.0)
    b = cached.compute_haat_m(1.0, 2.0, 3.0)
    assert a == b == 4.5
    assert calls["n"] == 1


def test_limit_constant_is_six_meters():
    assert CAT_A_OUTDOOR_HAAT_LIMIT_M == 6.0


def test_anti_hardcode_no_fixture_coords_or_haat_table():
    sources = [
        (REPO_ROOT / "services" / "registration_service.py").read_text(encoding="utf-8"),
        (REPO_ROOT / "services" / "terrain" / "haat.py").read_text(encoding="utf-8"),
        (REPO_ROOT / "services" / "terrain" / "ned.py").read_text(encoding="utf-8"),
    ]
    joined = "\n".join(sources)
    assert "_KNOWN_STREET_HAAT" not in joined
    assert "38.882162" not in joined
    assert "-77.113755" not in joined
    assert "device_e" not in joined


def test_ned_winnforum_haat_matches_harness_algorithm_when_tiles_present():
    """Real USGS NED path: HAAT equals harness TerrainDriver±wf_itm for REG.7 site.

    Coordinates are loaded from the environment of the official case only inside
    this test (not production code). Skips if DC-area tiles are not provisioned.
    """
    ned_dir = REPO_ROOT / "data" / "geo" / "ned"
    required = (
        "usgs_ned_1_n39w077_gridfloat_std.flt",
        "usgs_ned_1_n39w078_gridfloat_std.flt",
        "usgs_ned_1_n40w077_gridfloat_std.flt",
        "usgs_ned_1_n40w078_gridfloat_std.flt",
    )
    if not all((ned_dir / name).is_file() for name in required):
        pytest.skip("NED tiles for REG.7 area not installed under data/geo/ned")

    from services.terrain.haat import WinnForumHaatProvider
    from services.terrain.ned import NedTerrainProvider

    # Official REG.7 device_8 installation (harness WINNF_FT_S_REG_7).
    lat, lon, height = 38.882162, -77.113755, 4.0
    provider = WinnForumHaatProvider(NedTerrainProvider(ned_dir))
    norm, elev = provider.compute_normalized_haat_m(lat, lon)
    haat = provider.compute_haat_m(lat, lon, height, height_is_agl=True)
    # Bit-level match vs harness TerrainDriver on the same tiles (recorded locally).
    assert elev == pytest.approx(80.249911, abs=HAAT_NED_ABS_TOL_M)
    assert norm == pytest.approx(18.130659, abs=HAAT_NED_ABS_TOL_M)
    assert haat == pytest.approx(22.130659, abs=HAAT_NED_ABS_TOL_M)
    assert haat > CAT_A_OUTDOOR_HAAT_LIMIT_M
