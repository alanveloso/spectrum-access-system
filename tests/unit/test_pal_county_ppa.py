"""PAL county service area + claimed-contour semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from config import clear_settings_cache
from models.models import AdminInjectedData
from services.county_geometry import (
    CountyGeometryError,
    canonicalize_fips,
    load_county_geometry,
)
from services.ppa_geometry import (
    intersect_geojson,
    polygon_within_service_area,
    union_geojson,
)
from services.ppa_service import (
    KIND_STATUS,
    KIND_ZONE,
    _pal_service_area,
    _union_service_areas,
    create_ppa,
    get_ppa_creation_status,
)
from tests.fixtures.factories import make_pal, square_polygon
from tests.unit.test_ppa_creation import _cbsd_with_location, _feature_collection, _ppa_body


def _write_county(root: Path, fips: str, geom: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": geom}],
    }
    (root / f"{fips}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def county_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SAS_COUNTY_DIR", str(tmp_path))
    clear_settings_cache()
    yield tmp_path
    clear_settings_cache()


def _pal_with_fips(db: Session, *, fips: str, pal_id: str, user_id: str = "holder-a"):
    return make_pal(
        db,
        user_id=user_id,
        pal_id=pal_id,
        record_json={
            "palId": pal_id,
            "userId": user_id,
            "licenseStatus": "VALID",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3_550_000_000,
                    "highFrequency": 3_560_000_000,
                }
            },
            "license": {"licenseAreaIdentifier": fips},
        },
    )


def test_a_pal_county_identifier_resolves_polygon(db_session: Session, county_root: Path) -> None:
    geom = square_polygon(-99.5, 40.2, 0.2)
    _write_county(county_root, "99901", geom)
    pal = {
        "license": {"licenseAreaIdentifier": "99901"},
        "licenseArea": square_polygon(0.0, 0.0, 9.0),
    }
    resolved = _pal_service_area(pal)
    assert resolved is not None
    # Identifier wins over inline geometry (reference PAL county semantics).
    assert resolved["type"] in {"Polygon", "MultiPolygon"}
    loaded = load_county_geometry("99901", county_dir=county_root)
    assert loaded["type"] == resolved["type"]


def test_b_multiple_counties_union_multipolygon(county_root: Path) -> None:
    _write_county(county_root, "99911", square_polygon(-100.0, 40.0, 0.05))
    _write_county(county_root, "99912", square_polygon(-99.0, 41.0, 0.05))
    pals = [
        {"license": {"licenseAreaIdentifier": "99911"}},
        {"license": {"licenseAreaIdentifier": "99912"}},
    ]
    unioned = _union_service_areas(pals)
    assert unioned is not None
    assert unioned["type"] == "MultiPolygon"


def test_c_generated_rf_intersected_with_county(
    db_session: Session, county_root: Path
) -> None:
    county = square_polygon(-105.27, 40.0, 0.02)
    _write_county(county_root, "99921", county)
    pal = _pal_with_fips(db_session, fips="99921", pal_id="pal-clip-1")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    ppa_id = create_ppa(
        db_session,
        _ppa_body(
            {
                "palIds": [pal.pal_id],
                "cbsdIds": [cbsd.cbsd_id],
                "_rfEngines": fake_ppa_rf_engines(extra_loss_db=0.0),
            }
        ),
    )
    assert ppa_id
    assert get_ppa_creation_status(db_session)["withError"] is False
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).one()
    zone = json.loads(row.data_json)["record"]["zone"]
    assert polygon_within_service_area(zone, county)
    clipped = intersect_geojson(zone, county)
    assert clipped is not None


def test_d_topological_containment_not_vertex_only() -> None:
    sa = {
        "type": "Polygon",
        "coordinates": [
            [
                [0.0, 0.0],
                [2.0, 0.0],
                [2.0, 1.0],
                [1.0, 1.0],
                [1.0, 2.0],
                [2.0, 2.0],
                [2.0, 3.0],
                [0.0, 3.0],
                [0.0, 0.0],
            ]
        ],
    }
    # All four vertices sit on the C-shape arms; the east edge crosses the notch.
    crossing = {
        "type": "Polygon",
        "coordinates": [
            [
                [1.4, 0.4],
                [1.6, 0.4],
                [1.6, 2.6],
                [1.4, 2.6],
                [1.4, 0.4],
            ]
        ],
    }
    assert polygon_within_service_area(crossing, sa) is False


def test_e_cbsd_outside_county_fails(db_session: Session, county_root: Path) -> None:
    _write_county(county_root, "99931", square_polygon(-105.27, 40.0, 0.01))
    pal = _pal_with_fips(db_session, fips="99931", pal_id="pal-out-1")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=41.0, lon=-104.0)
    assert (
        create_ppa(
            db_session,
            _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    status = get_ppa_creation_status(db_session)
    assert status["withError"] is True
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "cbsd_outside_service_area" in (row.data_json or "")
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).count() == 0


def test_f_claimed_may_exclude_cluster_cbsd(
    db_session: Session, county_root: Path
) -> None:
    county = square_polygon(-105.27, 40.0, 0.05)
    _write_county(county_root, "99941", county)
    pal = _pal_with_fips(db_session, fips="99941", pal_id="pal-claim-1")
    inside = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    near = _cbsd_with_location(db_session, user_id="holder-a", lat=40.002, lon=-105.268)
    tiny = _feature_collection(-105.27, 40.0, 0.0004)
    ppa_id = create_ppa(
        db_session,
        _ppa_body(
            {
                "palIds": [pal.pal_id],
                "cbsdIds": [inside.cbsd_id, near.cbsd_id],
                "providedContour": tiny,
            }
        ),
    )
    assert ppa_id
    assert get_ppa_creation_status(db_session)["withError"] is False
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).one()
    zone = json.loads(row.data_json)["record"]["zone"]
    assert polygon_within_service_area(zone, county)


def test_g_claimed_exceeds_rf_max_fails(db_session: Session, county_root: Path) -> None:
    _write_county(county_root, "99951", square_polygon(-105.27, 40.0, 0.05))
    pal = _pal_with_fips(db_session, fips="99951", pal_id="pal-rf-1")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    huge = _feature_collection(-105.27, 40.0, 0.04)
    assert (
        create_ppa(
            db_session,
            _ppa_body(
                {
                    "palIds": [pal.pal_id],
                    "cbsdIds": [cbsd.cbsd_id],
                    "providedContour": huge,
                }
            ),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "claimedBoundary_exceeds_rf_maximum" in (row.data_json or "")
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).count() == 0


def test_h_claimed_outside_service_area_rejected(
    db_session: Session, county_root: Path
) -> None:
    _write_county(county_root, "99961", square_polygon(-105.27, 40.0, 0.01))
    pal = _pal_with_fips(db_session, fips="99961", pal_id="pal-sa-claim")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    elsewhere = _feature_collection(-104.0, 41.0, 0.0005)
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    assert (
        create_ppa(
            db_session,
            _ppa_body(
                {
                    "palIds": [pal.pal_id],
                    "cbsdIds": [cbsd.cbsd_id],
                    "providedContour": elsewhere,
                    "_rfEngines": fake_ppa_rf_engines(extra_loss_db=0.0),
                }
            ),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "claimedBoundary_outside_service_area" in (row.data_json or "")


def test_i_missing_county_fail_closed(db_session: Session, county_root: Path) -> None:
    pal = _pal_with_fips(db_session, fips="99971", pal_id="pal-miss-1")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    assert (
        create_ppa(
            db_session,
            _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "service_area_unavailable" in (row.data_json or "")
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).count() == 0


def test_j_holder_mismatch_still_wins_over_geometry(
    db_session: Session, county_root: Path
) -> None:
    _write_county(county_root, "99981", square_polygon(-105.27, 40.0, 0.05))
    pal = _pal_with_fips(db_session, fips="99981", pal_id="pal-hold-1", user_id="holder-a")
    outsider = _cbsd_with_location(db_session, user_id="other-user", lat=40.0, lon=-105.27)
    assert (
        create_ppa(
            db_session,
            _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [outsider.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "cbsd_not_pal_holder" in (row.data_json or "")


def test_j_overlap_still_rejected_with_county_sa(
    db_session: Session, county_root: Path
) -> None:
    from tests.fixtures.factories import make_ppa_with_pal

    county = square_polygon(-105.27, 40.0, 0.05)
    _write_county(county_root, "99991", county)
    pal = _pal_with_fips(db_session, fips="99991", pal_id="pal-ov-1")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    make_ppa_with_pal(
        db_session,
        pal=pal,
        cbsd_reference_ids=["existing"],
        zone=square_polygon(-105.27, 40.0, 0.05),
    )
    assert (
        create_ppa(
            db_session,
            _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "overlaps_existing_ppa" in (row.data_json or "")


def test_adjacent_counties_union_is_topological(county_root: Path) -> None:
    left = square_polygon(-1.0, 0.0, 0.5)
    right = square_polygon(0.0, 0.0, 0.5)
    _write_county(county_root, "99801", left)
    _write_county(county_root, "99802", right)
    unioned = union_geojson(left, right)
    assert unioned is not None
    assert unioned["type"] in {"Polygon", "MultiPolygon"}


def test_fips_canonicalization_rejects_path_escape() -> None:
    assert canonicalize_fips("../etc/passwd") is None
    assert canonicalize_fips("99901.json") is None
    assert canonicalize_fips("99901") == "99901"
    with pytest.raises(CountyGeometryError):
        load_county_geometry("../99901")


def test_malformed_identifier_fail_closed(
    db_session: Session, county_root: Path
) -> None:
    pal = _pal_with_fips(db_session, fips="not-a-fips", pal_id="pal-bad-id")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    assert (
        create_ppa(
            db_session,
            _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "service_area_unavailable" in (row.data_json or "")
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).count() == 0


def test_unrelated_pal_not_unioned_into_service_area(
    db_session: Session, county_root: Path
) -> None:
    """PPA for PAL A must clip to A only, even if PAL B exists in the DB."""
    county_a = square_polygon(-105.27, 40.0, 0.02)
    county_b = square_polygon(-104.0, 41.0, 0.5)
    _write_county(county_root, "99701", county_a)
    _write_county(county_root, "99702", county_b)
    pal_a = _pal_with_fips(db_session, fips="99701", pal_id="pal-only-a")
    _pal_with_fips(db_session, fips="99702", pal_id="pal-other-b")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    ppa_id = create_ppa(
        db_session,
        _ppa_body(
            {
                "palIds": [pal_a.pal_id],
                "cbsdIds": [cbsd.cbsd_id],
                "_rfEngines": fake_ppa_rf_engines(extra_loss_db=0.0),
            }
        ),
    )
    assert ppa_id
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).one()
    zone = json.loads(row.data_json)["record"]["zone"]
    assert polygon_within_service_area(zone, county_a)
    assert polygon_within_service_area(zone, county_b) is False
