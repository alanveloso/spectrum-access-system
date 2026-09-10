"""ROOT-1: Cat A outdoor registration accepts when HAAT ≤ 6 with real NED tiles.

Derived from official harness SIQ.3 outdoor fixtures (device_c AMSL / device_g AGL)
relocated to Kansas coords. No harness fixture IDs are hard-coded in production.
"""

from __future__ import annotations

import pytest

from models.models import Cbsd
from services.registration_service import INVALID_PARAM, SUCCESS, process_registration
from services.terrain import (
    DeterministicHaatProvider,
    last_haat_rejection_reason,
    reset_haat_provider,
    set_haat_provider,
)
from tests.fixtures.factories import cat_a_install, make_fcc_id, make_user_id
from tests.support.repo import REPO_ROOT

# SIQ.3 outdoor relocation coords (harness WINNF_FT_S_SIQ_3).
_SIQ3_C_LAT, _SIQ3_C_LON = 40.0, -97.87  # device_c style AMSL
_SIQ3_G_LAT, _SIQ3_G_LON = 38.2, -99.5  # device_g style AGL


def _payload(
    fcc_id: str,
    serial: str,
    user_id: str,
    *,
    lat: float,
    lon: float,
    indoor: bool,
    height: float,
    height_type: str = "AGL",
    gain: float | None = None,
):
    install = cat_a_install(lat, lon, indoor=indoor, height=height)
    install["heightType"] = height_type
    if gain is not None:
        install["antennaGain"] = gain
    return {
        "userId": user_id,
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "measCapability": [],
        "installationParam": install,
    }


@pytest.fixture
def real_ned_or_skip():
    """Require UUT NED tiles for SIQ.3 outdoor HAAT footprints."""
    ned = REPO_ROOT / "data" / "geo" / "ned"
    # Site tiles for SIQ.3 outdoor points (either official naming form).
    need = [
        ("n40w098", _SIQ3_C_LAT, _SIQ3_C_LON),
        ("n39w100", _SIQ3_G_LAT, _SIQ3_G_LON),
    ]
    for enc, _, _ in need:
        usgs = ned / f"usgs_ned_1_{enc}_gridfloat_std.flt"
        flt = ned / f"float{enc}_1_std.flt"
        if not usgs.is_file() and not flt.is_file():
            pytest.skip(
                f"missing UUT NED tile {enc}; run tools/winnforum/sync_uut_ned_tiles.sh"
            )
    reset_haat_provider()
    yield
    reset_haat_provider()


def test_siq3_style_outdoor_amsl_registers_when_ned_present(db_session, real_ned_or_skip):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _payload(
        fcc.fcc_id,
        "sn-root1-amsl",
        user.user_id,
        lat=_SIQ3_C_LAT,
        lon=_SIQ3_C_LON,
        indoor=False,
        height=4.5,
        height_type="AMSL",
        gain=-127,
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert "cbsdId" in resp[0]
    assert (
        db_session.query(Cbsd)
        .filter_by(cbsd_serial_number="sn-root1-amsl")
        .first()
        is not None
    )


def test_siq3_style_outdoor_agl_registers_when_ned_present(db_session, real_ned_or_skip):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _payload(
        fcc.fcc_id,
        "sn-root1-agl",
        user.user_id,
        lat=_SIQ3_G_LAT,
        lon=_SIQ3_G_LON,
        indoor=False,
        height=4.1,
        height_type="AGL",
        gain=45,
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert "cbsdId" in resp[0]


def test_outdoor_haat_still_rejects_when_terrain_missing(db_session):
    set_haat_provider(
        DeterministicHaatProvider(
            missing_locations={(10.0, 20.0)},
            default_norm_haat_m=None,
        )
    )
    try:
        fcc = make_fcc_id(db_session)
        user = make_user_id(db_session)
        payload = _payload(
            fcc.fcc_id,
            "sn-root1-missing",
            user.user_id,
            lat=10.0,
            lon=20.0,
            indoor=False,
            height=4.0,
        )
        resp = process_registration(db_session, [payload])
        assert resp[0]["response"]["responseCode"] == INVALID_PARAM
        assert "cbsdId" not in resp[0]
        reason = last_haat_rejection_reason() or ""
        assert "terrain_unavailable" in reason
    finally:
        reset_haat_provider()


def test_outdoor_agl_above_six_still_rejected_without_ned(db_session):
    """AGL height > 6 m is INVALID before HAAT radial sampling."""
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _payload(
        fcc.fcc_id,
        "sn-root1-tall",
        user.user_id,
        lat=38.0,
        lon=-97.0,
        indoor=False,
        height=6.1,
        height_type="AGL",
    )
    resp = process_registration(db_session, [payload])
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
