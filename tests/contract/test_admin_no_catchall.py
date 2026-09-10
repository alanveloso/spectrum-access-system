"""Admin catch-all removal and official path inventory tests ()."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from main import app
from services.admin_api_inventory import (
    EXPLICIT_UNIMPLEMENTED_ADMIN_POST_PATHS,
    OFFICIAL_ADMIN_POST_PATHS,
    missing_official_admin_paths,
)
from services.blacklist_service import add_fcc_id_serial_blacklist
from services.registration_service import BLACKLISTED, process_registration
import database
from tests.support.repo import REPO_ROOT as ROOT

ADMIN_ROUTES = ROOT / "routes" / "admin_routes.py"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _ensure_process_db_schema():
    """Module-level TestClient shares process engine; rebind tests may leave it empty."""
    database.init_db(retries=1, delay_seconds=0)
    yield


def test_admin_stub_catch_all_removed_from_source():
    text = ADMIN_ROUTES.read_text(encoding="utf-8")
    assert "admin_stub" not in text
    assert "full_path:path" not in text


def test_unknown_admin_path_returns_404():
    response = client.post("/admin/this_path_does_not_exist_anywhere")
    assert response.status_code == 404


def test_propagation_query_is_implemented_not_501(monkeypatch):
    """endpoint must not remain an unimplemented 501 stub."""
    from types import SimpleNamespace

    from services.propagation.service import ACTIVITY_LOSS_FACTOR_DEFAULT, PropagationEngines

    def _pl(db_loss=100.0):
        return SimpleNamespace(
            db_loss=db_loss,
            incidence_angles=SimpleNamespace(hor_cbsd=0.0, hor_rx=0.0, ver_rx=0.0),
        )

    engines = PropagationEngines(
        calc_itm=lambda *a, **k: _pl(),
        calc_hybrid=lambda *a, **k: _pl(90.0),
        calc_p2108=lambda *a, **k: 0.0,
        activity_loss_factor=ACTIVITY_LOSS_FACTOR_DEFAULT,
        antenna_standard_gains=lambda *a, **k: 0.0,
        antenna_fss_gains=lambda *a, **k: 0.0,
        grid_polygon=lambda *_a, **_k: [(-77.0, 38.0)],
        region_nlcd_vote=lambda *_a, **_k: "RURAL",
        terrain_elevation_m=lambda *_a, **_k: 0.0,
    )
    monkeypatch.setattr(
        "services.propagation.load_reference_engines",
        lambda: engines,
    )
    response = client.post(
        "/admin/query/propagation_and_antenna_model",
        json={
            "modelType": "1",
            "reliabilityLevel": 0.05,
            "cbsd": {
                "latitude": 38.9,
                "longitude": -77.1,
                "height": 4.0,
                "heightType": "AGL",
                "indoorDeployment": False,
                "antennaAzimuth": 0,
                "antennaBeamwidth": 360,
                "antennaGain": 0,
            },
            "fss": {"latitude": 38.91, "longitude": -77.11, "height": 5.0},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "pathlossDb" in body
    assert "txAntennaGainDbi" in body


def test_new_official_inject_paths_are_explicit_and_ok():
    for path in (
        "/admin/injectdata/esc_zone",
        "/admin/injectdata/cluster_list",
        "/admin/injectdata/sas_admin",
    ):
        response = client.post(path, json={"record": {"id": "x"}})
        assert response.status_code == 200, path


def test_blacklist_fcc_id_and_serial_requires_schema_fields():
    missing = client.post(
        "/admin/injectdata/blacklist_fcc_id_and_serial_number",
        json={"fccId": "only-fcc"},
    )
    assert missing.status_code in (400, 422)

    empty = client.post(
        "/admin/injectdata/blacklist_fcc_id_and_serial_number",
        json={"fccId": "", "cbsdSerialNumber": ""},
    )
    assert empty.status_code in (400, 422)

    ok = client.post(
        "/admin/injectdata/blacklist_fcc_id_and_serial_number",
        json={"fccId": "test-fcc-bl", "cbsdSerialNumber": "serial-bl-1"},
    )
    assert ok.status_code == 200

    # Idempotent re-inject
    again = client.post(
        "/admin/injectdata/blacklist_fcc_id_and_serial_number",
        json={"fccId": "test-fcc-bl", "cbsdSerialNumber": "serial-bl-1"},
    )
    assert again.status_code == 200


def test_blacklisted_fcc_serial_rejects_registration():
    # Prefer database.SessionLocal after rebind_engine (module-level imports go stale).
    database.init_db()
    db = database.SessionLocal()
    try:
        from models.models import FccIdRecord, UserIdRecord
        from services.registration_service import SUCCESS

        add_fcc_id_serial_blacklist(db, "fcc-serial-bl", "SN-BL-1")
        if not db.query(FccIdRecord).filter_by(fcc_id="fcc-serial-bl").first():
            db.add(FccIdRecord(fcc_id="fcc-serial-bl", fcc_max_eirp=47))
        if not db.query(UserIdRecord).filter_by(user_id="user-bl").first():
            db.add(UserIdRecord(user_id="user-bl"))
        db.commit()

        base_req = {
            "fccId": "fcc-serial-bl",
            "userId": "user-bl",
            "cbsdCategory": "A",
            "airInterface": {"radioTechnology": "E_UTRA"},
            "measCapability": ["RECEIVED_POWER_WITHOUT_GRANT"],
            "installationParam": {
                "latitude": 39.1,
                "longitude": -77.1,
                "height": 10,
                "heightType": "AGL",
                "indoorDeployment": True,
            },
        }

        responses = process_registration(
            db, [{**base_req, "cbsdSerialNumber": "SN-BL-1"}]
        )
        assert responses[0]["response"]["responseCode"] == BLACKLISTED

        # Different serial of same FCC must not be blacklisted by serial rule.
        responses_ok = process_registration(
            db, [{**base_req, "cbsdSerialNumber": "SN-OTHER"}]
        )
        assert responses_ok[0]["response"]["responseCode"] == SUCCESS
    finally:
        db.close()


def test_is_cbsd_blacklisted_covers_fcc_only_and_pair():
    database.init_db()
    db = database.SessionLocal()
    try:
        from services.blacklist_service import (
            add_fcc_id_blacklist,
            is_cbsd_blacklisted,
        )

        add_fcc_id_blacklist(db, "fcc-only")
        assert is_cbsd_blacklisted(db, "fcc-only", "any-serial")
        assert not is_cbsd_blacklisted(db, "fcc-other", "any-serial")

        add_fcc_id_serial_blacklist(db, "fcc-pair", "SN-1")
        assert is_cbsd_blacklisted(db, "fcc-pair", "SN-1")
        assert not is_cbsd_blacklisted(db, "fcc-pair", "SN-2")
    finally:
        db.close()


def test_dpa_deactivation_and_esc_triggers_are_explicit():
    assert client.post("/admin/trigger/dpa_deactivation", json={}).status_code == 200
    assert (
        client.post("/admin/trigger/esc_detection", json={"dpaId": "x"}).status_code
        == 200
    )
    assert client.post("/admin/trigger/esc_reset").status_code == 200
    assert client.post("/admin/trigger/disconnect_esc").status_code == 200


def test_official_inventory_has_no_uncovered_paths():
    missing = missing_official_admin_paths()
    assert not missing, f"official admin paths without explicit route: {sorted(missing)}"


def test_router_declares_all_official_paths():
    text = ADMIN_ROUTES.read_text(encoding="utf-8")
    declared = set(re.findall(r'@router\.post\("/([^"]+)"\)', text))
    missing_declared = OFFICIAL_ADMIN_POST_PATHS - declared
    assert not missing_declared, sorted(missing_declared)
    assert EXPLICIT_UNIMPLEMENTED_ADMIN_POST_PATHS <= declared
