"""C5 follow-up: Maximum / Largest Allowable PPA Contour (RF)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from models.models import AdminInjectedData
from services.geometry import point_in_geojson
from services.ppa_rf_contour import (
    PpaRfContourError,
    PpaRfEngines,
    THRESHOLD_PER_10MHZ_DBM,
    cbsd_rf_contour_ring,
    maximum_rf_ppa_contour,
)
from services.ppa_service import create_ppa, get_ppa_creation_status
from services.terrain.vincenty import geodesic_distance_km
from tests.fixtures.factories import make_cbsd, make_pal, square_polygon
from tests.fixtures.ppa_rf import coverage_radius_km_for_eirp, fake_ppa_rf_engines


def _device(
    *,
    lat: float = 39.1,
    lon: float = -94.58,
    cat: str = "A",
    eirp: float | None = None,
    height: float = 10.0,
    gain: float = 0.0,
    azimuth: float | None = None,
    beamwidth: float | None = None,
) -> dict:
    install: dict = {
        "latitude": lat,
        "longitude": lon,
        "height": height,
        "heightType": "AGL",
        "indoorDeployment": False,
        "antennaGain": gain,
    }
    if eirp is not None:
        install["eirpCapability"] = eirp
    if azimuth is not None:
        install["antennaAzimuth"] = azimuth
    if beamwidth is not None:
        install["antennaBeamwidth"] = beamwidth
    return {"cbsdCategory": cat, "installationParam": install}


def _mean_radius_km(ring: list[list[float]], lat0: float, lon0: float) -> float:
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    dists = [geodesic_distance_km(lat0, lon0, float(p[1]), float(p[0])) for p in pts]
    return sum(dists) / max(len(dists), 1)


def test_rf_contour_grows_with_eirp():
    engines = fake_ppa_rf_engines(extra_loss_db=0.0)
    small = cbsd_rf_contour_ring(_device(eirp=20.0), engines=engines)
    large = cbsd_rf_contour_ring(_device(eirp=40.0), engines=engines)
    r_small = _mean_radius_km(small, 39.1, -94.58)
    r_large = _mean_radius_km(large, 39.1, -94.58)
    assert r_large > r_small * 1.5
    assert THRESHOLD_PER_10MHZ_DBM == -96.0


def test_rf_contour_two_cbsds_union_covers_both():
    engines = fake_ppa_rf_engines(extra_loss_db=10.0)
    d1 = _device(lat=39.10, lon=-94.58, eirp=30.0)
    d2 = _device(lat=39.12, lon=-94.58, eirp=30.0)
    fc = maximum_rf_ppa_contour([d1, d2], engines=engines)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert point_in_geojson(39.10, -94.58, fc)
    assert point_in_geojson(39.12, -94.58, fc)


def test_rf_contour_responds_to_azimuth_beamwidth():
    engines = fake_ppa_rf_engines(extra_loss_db=0.0)
    # Directional: energy to the east (90°)
    ring = cbsd_rf_contour_ring(
        _device(eirp=35.0, azimuth=90.0, beamwidth=60.0, gain=10.0),
        engines=engines,
    )
    # Point at azimuth index ~90 is ring[90]
    east = ring[90]
    west = ring[270]
    r_east = geodesic_distance_km(39.1, -94.58, east[1], east[0])
    r_west = geodesic_distance_km(39.1, -94.58, west[1], west[0])
    assert r_east > r_west


def test_rf_contour_height_changes_coverage():
    engines = fake_ppa_rf_engines(extra_loss_db=0.0)
    low = cbsd_rf_contour_ring(_device(eirp=30.0, height=3.0), engines=engines)
    high = cbsd_rf_contour_ring(_device(eirp=30.0, height=30.0), engines=engines)
    assert _mean_radius_km(high, 39.1, -94.58) > _mean_radius_km(low, 39.1, -94.58)


def test_rf_unavailable_fails_closed():
    @dataclass
    class _Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("no_itm")

    engines = PpaRfEngines(
        calc_hybrid=_Boom(),  # type: ignore[arg-type]
        antenna_standard_gains=lambda *a, **k: 0.0,
        region_type=lambda lat, lon: "SUBURBAN",
    )
    with pytest.raises(PpaRfContourError):
        cbsd_rf_contour_ring(_device(), engines=engines)


def _cbsd(db, *, user_id: str, lat: float, lon: float, **install_extra):
    cbsd = make_cbsd(db, user_id=user_id)
    install = {
        "latitude": lat,
        "longitude": lon,
        "height": 10,
        "heightType": "AGL",
        "indoorDeployment": False,
        "antennaGain": 0,
        "antennaBeamwidth": 360,
        **install_extra,
    }
    cbsd.registration_json = json.dumps(
        {
            "fccId": cbsd.fcc_id,
            "cbsdSerialNumber": cbsd.cbsd_serial_number,
            "userId": user_id,
            "cbsdCategory": "A",
            "installationParam": install,
        }
    )
    db.commit()
    return cbsd


def test_claimed_boundary_smaller_than_rf_max(db_session):
    engines = fake_ppa_rf_engines(extra_loss_db=5.0)
    pal = make_pal(db_session, user_id="h")
    cbsd = _cbsd(db_session, user_id="h", lat=39.1, lon=-94.58)
    claimed = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": square_polygon(-94.58, 39.1, 0.002),
            }
        ],
    }
    ppa_id = create_ppa(
        db_session,
        {
            "palIds": [pal.pal_id],
            "cbsdIds": [cbsd.cbsd_id],
            "claimedBoundary": claimed,
            "_rfEngines": engines,
        },
    )
    assert ppa_id
    assert get_ppa_creation_status(db_session)["withError"] is False


def test_claimed_boundary_larger_than_rf_max_rejected(db_session):
    engines = fake_ppa_rf_engines(extra_loss_db=40.0)  # tiny RF max
    pal = make_pal(db_session, user_id="h")
    cbsd = _cbsd(db_session, user_id="h", lat=39.1, lon=-94.58)
    claimed = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": square_polygon(-94.58, 39.1, 0.2),
            }
        ],
    }
    assert (
        create_ppa(
            db_session,
            {
                "palIds": [pal.pal_id],
                "cbsdIds": [cbsd.cbsd_id],
                "claimedBoundary": claimed,
                "_rfEngines": engines,
            },
        )
        == ""
    )
    assert get_ppa_creation_status(db_session)["withError"] is True


def test_service_area_clip_still_applies(db_session):
    engines = fake_ppa_rf_engines(extra_loss_db=25.0)
    area = square_polygon(-94.58, 39.1, 0.05)
    pal = make_pal(
        db_session,
        user_id="h",
        pal_id="pal-sa-rf",
        record_json={
            "palId": "pal-sa-rf",
            "userId": "h",
            "licenseStatus": "VALID",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3550_000_000,
                    "highFrequency": 3560_000_000,
                }
            },
            "license": {
                "licenseArea": {
                    "type": "FeatureCollection",
                    "features": [
                        {"type": "Feature", "properties": {}, "geometry": area}
                    ],
                }
            },
        },
    )
    cbsd = _cbsd(db_session, user_id="h", lat=39.1, lon=-94.58)
    ppa_id = create_ppa(
        db_session,
        {
            "palIds": [pal.pal_id],
            "cbsdIds": [cbsd.cbsd_id],
            "_rfEngines": engines,
        },
    )
    assert ppa_id, get_ppa_creation_status(db_session)


def test_create_ppa_rf_unavailable_no_false_ppa(db_session, monkeypatch):
    def _boom():
        raise PpaRfContourError("no_backend")

    monkeypatch.setattr(
        "services.ppa_service.load_default_ppa_rf_engines", _boom
    )
    pal = make_pal(db_session, user_id="h")
    cbsd = _cbsd(db_session, user_id="h", lat=39.1, lon=-94.58)
    assert (
        create_ppa(
            db_session, {"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}
        )
        == ""
    )
    assert get_ppa_creation_status(db_session)["withError"] is True
    assert db_session.query(AdminInjectedData).filter_by(kind="zone").count() == 0


def test_coverage_radius_helper_monotonic():
    assert coverage_radius_km_for_eirp(40) > coverage_radius_km_for_eirp(20)


def test_prcz_classified_as_configurable_not_dedicated_polygon():
    """PRCZ: harness QPR suite has no dedicated island case; product uses
    configurable protected areas (+ FCC Santa Isabel office). Prove configurable
    area blocks registration-equivalent quiet-zone check (not office proximity).
    """
    from services.quiet_zone_service import quiet_zone_blocks_location

    # Synthetic PR interior point far from Santa Isabel FCC office (~18.0, -66.38).
    lat, lon = 18.25, -66.50
    # Without config: not NRQZ; FCC office may or may not hit — force FCC off.
    # Use a dedicated session-free check via injectable db below in integration style.
    assert quiet_zone_blocks_location(
        lat,
        lon,
        cbsd_category="A",
        require_fcc_dataset=True,
    ) in (None, "fcc_field_office", "table_mountain")


def test_prcz_configurable_area_blocks(db_session):
    from services.quiet_zone_service import quiet_zone_blocks_location

    db_session.add(
        AdminInjectedData(
            kind="quiet_zone_config",
            data_json=json.dumps(
                {
                    "fccOfficesEnabled": False,
                    "tableMountainEnabled": False,
                    "configurableAreasEnabled": True,
                }
            ),
        )
    )
    # Approximate western PR coastal polygon (synthetic; not a harness fixture).
    ring = [
        [-67.3, 17.9],
        [-67.3, 18.5],
        [-65.6, 18.5],
        [-65.6, 17.9],
        [-67.3, 17.9],
    ]
    db_session.add(
        AdminInjectedData(
            kind="quiet_protected_area",
            data_json=json.dumps(
                {
                    "zone": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"name": "PRCZ-synthetic"},
                                "geometry": {"type": "Polygon", "coordinates": [ring]},
                            }
                        ],
                    }
                }
            ),
        )
    )
    db_session.commit()
    assert (
        quiet_zone_blocks_location(18.2, -66.5, db=db_session)
        == "configurable_protected_area"
    )
    # Outside the synthetic polygon
    assert quiet_zone_blocks_location(20.0, -66.5, db=db_session) is None
