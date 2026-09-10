"""Admin PPA creation lifecycle."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services.ppa_service import (
    KIND_STATUS,
    KIND_ZONE,
    create_ppa,
    get_ppa_creation_status,
)
from tests.fixtures.factories import (
    make_cbsd,
    make_pal,
    make_ppa_with_pal,
    square_polygon,
)

client = TestClient(app)


def _cbsd_with_location(db, *, user_id: str, lat: float, lon: float):
    cbsd = make_cbsd(db, user_id=user_id)
    cbsd.registration_json = json.dumps(
        {
            "fccId": cbsd.fcc_id,
            "cbsdSerialNumber": cbsd.cbsd_serial_number,
            "userId": user_id,
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": lat,
                "longitude": lon,
                "height": 10,
                "heightType": "AGL",
                "indoorDeployment": False,
                "antennaGain": 0,
                "antennaBeamwidth": 360,
            },
        }
    )
    db.commit()
    return cbsd


def _ppa_body(data: dict | None = None, **kwargs):
    """Attach synthetic RF engines so unit tests do not require ITM/NED."""
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    body = dict(data or {})
    body.update(kwargs)
    body.setdefault("_rfEngines", fake_ppa_rf_engines())
    return body


def _feature_collection(lon: float, lat: float, delta: float = 0.05) -> dict:
    poly = square_polygon(lon, lat, delta)
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": poly},
        ],
    }


def test_get_ppa_status_defaults_to_incomplete(db_session):
    status = get_ppa_creation_status(db_session)
    assert status == {"completed": False, "withError": False}


def test_create_ppa_success_persists_zone_and_normative_id(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)

    ppa_id = create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
    )
    assert ppa_id.startswith("zone/ppa/")
    parts = ppa_id.split("/")
    assert parts[0] == "zone" and parts[1] == "ppa"
    assert pal.pal_id in ppa_id
    assert get_ppa_creation_status(db_session) == {
        "completed": True,
        "withError": False,
    }
    zones = db_session.query(AdminInjectedData).filter_by(kind=KIND_ZONE).all()
    assert len(zones) == 1
    record = json.loads(zones[0].data_json)["record"]
    assert record["usage"] == "PPA"
    assert record["ppaInfo"]["palId"] == [pal.pal_id]
    assert cbsd.cbsd_id in record["ppaInfo"]["cbsdReferenceId"]
    assert record["zone"]["type"] == "FeatureCollection"
    assert len(record["zone"]["features"]) == 1
    assert record["zone"]["features"][0]["geometry"]["type"] in {
        "Polygon",
        "MultiPolygon",
    }


def test_create_ppa_unknown_pal_sets_with_error(db_session):
    cbsd = _cbsd_with_location(db_session, user_id="u1", lat=40.0, lon=-105.0)
    ppa_id = create_ppa(db_session, _ppa_body({"palIds": ["missing-pal"], "cbsdIds": [cbsd.cbsd_id]}),
    )
    assert ppa_id == ""
    assert get_ppa_creation_status(db_session) == {
        "completed": True,
        "withError": True,
    }
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "unknown_palId" in (row.data_json or "")


def test_create_ppa_rejects_cbsd_not_pal_holder(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    outsider = _cbsd_with_location(
        db_session, user_id="other-user", lat=40.0, lon=-105.27
    )
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [outsider.cbsd_id]}),
        )
        == ""
    )
    assert get_ppa_creation_status(db_session)["withError"] is True


def test_create_ppa_rejects_cbsd_outside_service_area(db_session):
    area = square_polygon(-105.27, 40.0, 0.01)
    pal = make_pal(
        db_session,
        user_id="holder-a",
        pal_id="pal-sa-1",
        record_json={
            "palId": "pal-sa-1",
            "userId": "holder-a",
            "licenseStatus": "VALID",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3_550_000_000,
                    "highFrequency": 3_560_000_000,
                }
            },
            "license": {"licenseArea": area},
        },
    )
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=41.0, lon=-104.0)
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "cbsd_outside_service_area" in (row.data_json or "")


def test_create_ppa_accepts_provided_contour(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    # Claimed must lie inside the RF maximum (tiny with default test engines).
    contour = _feature_collection(-105.27, 40.0, 0.0005)
    ppa_id = create_ppa(db_session, _ppa_body({
            "palIds": [pal.pal_id],
            "cbsdIds": [cbsd.cbsd_id],
            "providedContour": contour,
        }),
    )
    assert ppa_id
    assert get_ppa_creation_status(db_session)["withError"] is False


def test_create_ppa_rejects_overlap_existing_ppa(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    make_ppa_with_pal(
        db_session,
        pal=pal,
        cbsd_reference_ids=["existing"],
        zone=square_polygon(-105.27, 40.0, 0.05),
    )
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "overlaps_existing_ppa" in (row.data_json or "")


def test_create_ppa_rejects_edge_crossing_overlap_without_shared_vertices(db_session):
    """Cross-bar overlap must fail even when no vertex lies inside the other polygon."""
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    pal = make_pal(db_session, user_id="holder-a")
    # CBSD inside the crossing region; RF max (low extra loss) overlaps the vertical bar.
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=0.5, lon=2.0)
    existing_vertical = {
        "type": "Polygon",
        "coordinates": [
            [
                [1.5, -1.0],
                [2.5, -1.0],
                [2.5, 2.0],
                [1.5, 2.0],
                [1.5, -1.0],
            ]
        ],
    }
    make_ppa_with_pal(
        db_session,
        pal=pal,
        cbsd_reference_ids=["existing"],
        zone=existing_vertical,
    )
    assert (
        create_ppa(
            db_session,
            _ppa_body(
                {
                    "palIds": [pal.pal_id],
                    "cbsdIds": [cbsd.cbsd_id],
                    "_rfEngines": fake_ppa_rf_engines(extra_loss_db=0.0),
                }
            ),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "overlaps_existing_ppa" in (row.data_json or "")


def test_admin_http_create_and_status(db_session, monkeypatch):
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    monkeypatch.setattr(
        "services.ppa_service.load_default_ppa_rf_engines",
        fake_ppa_rf_engines,
    )
    pal = make_pal(db_session, user_id="holder-a")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)

    assert client.post("/admin/get_ppa_status").json() == {
        "completed": False,
        "withError": False,
    }
    resp = client.post(
        "/admin/trigger/create_ppa",
        json={"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]},
    )
    assert resp.status_code == 200
    ppa_id = resp.json()
    assert isinstance(ppa_id, str) and ppa_id.startswith("zone/ppa/")
    status = client.post("/admin/get_ppa_status").json()
    assert status == {"completed": True, "withError": False}


def test_create_ppa_missing_cbsd_ids_fails(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    assert create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id]})) == ""
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "missing_cbsdIds" in (row.data_json or "")
    assert get_ppa_creation_status(db_session)["withError"] is True


def test_create_ppa_duplicate_cbsd_ids_fails(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id, cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "duplicate_cbsdIds" in (row.data_json or "")


def test_create_ppa_invalid_provided_contour_fails(db_session):
    pal = make_pal(db_session, user_id="holder-a")
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    assert (
        create_ppa(db_session, _ppa_body({
                "palIds": [pal.pal_id],
                "cbsdIds": [cbsd.cbsd_id],
                "providedContour": {"type": "FeatureCollection", "features": []},
            }),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "invalid_providedContour" in (row.data_json or "")


def test_create_ppa_rejects_pal_without_valid_status(db_session):
    pal = make_pal(
        db_session,
        user_id="holder-a",
        license_status="EXPIRED",
        record_json={
            "palId": "pal-expired",
            "userId": "holder-a",
            "licenseStatus": "EXPIRED",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3_550_000_000,
                    "highFrequency": 3_560_000_000,
                }
            },
        },
        pal_id="pal-expired",
    )
    cbsd = _cbsd_with_location(db_session, user_id="holder-a", lat=40.0, lon=-105.27)
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}),
        )
        == ""
    )
    row = db_session.query(AdminInjectedData).filter_by(kind=KIND_STATUS).one()
    assert "inactive_pal" in (row.data_json or "")
