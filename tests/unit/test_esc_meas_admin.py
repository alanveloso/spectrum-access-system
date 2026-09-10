"""ESC/meas Admin triggers — verifiable domain mutations for operational checks."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services.esc_admin_service import (
    FLAG_ESC_DETECTION,
    FLAG_ESC_DISCONNECTED,
    KIND_ESC_AUDIT,
    apply_esc_detection,
    disconnect_esc,
    esc_detection_active,
    is_esc_disconnected,
    reset_esc_zone,
)
from services.meas_report import (
    FLAG_MEAS_HBT,
    FLAG_MEAS_REG,
    admin_flag_set,
    enable_measurement_report_heartbeat,
    enable_measurement_report_registration,
)
from tests.support.repo import REPO_ROOT
from tools.winnforum.admin_inventory import classify_uut_route

client = TestClient(app)


def test_meas_enable_persists_flags(db_session):
    enable_measurement_report_registration(db_session)
    enable_measurement_report_heartbeat(db_session)
    assert admin_flag_set(db_session, FLAG_MEAS_REG)
    assert admin_flag_set(db_session, FLAG_MEAS_HBT)
    row = db_session.query(AdminInjectedData).filter_by(kind=FLAG_MEAS_REG).one()
    assert "enabled" in (row.data_json or "")


def test_esc_detection_reset_disconnect(db_session):
    apply_esc_detection(db_session, {"dpaId": "dpa-under-test", "channels": [1]})
    assert esc_detection_active(db_session)
    reset_esc_zone(db_session)
    assert not esc_detection_active(db_session)
    disconnect_esc(db_session)
    assert is_esc_disconnected(db_session)
    audits = db_session.query(AdminInjectedData).filter_by(kind=KIND_ESC_AUDIT).all()
    assert len(audits) >= 3
    assert db_session.query(AdminInjectedData).filter_by(kind=FLAG_ESC_DISCONNECTED).count() == 1
    assert db_session.query(AdminInjectedData).filter_by(kind=FLAG_ESC_DETECTION).count() == 0


def test_admin_http_esc_and_meas(db_session):
    assert (
        client.post("/admin/trigger/meas_report_in_registration_response").status_code
        == 200
    )
    assert (
        client.post("/admin/trigger/meas_report_in_heartbeat_response").status_code
        == 200
    )
    assert (
        client.post("/admin/trigger/esc_detection", json={"dpaId": "x"}).status_code
        == 200
    )
    assert client.post("/admin/trigger/esc_reset").status_code == 200
    assert client.post("/admin/trigger/disconnect_esc").status_code == 200
    assert admin_flag_set(db_session, FLAG_MEAS_REG)
    assert is_esc_disconnected(db_session)


def test_inventory_classifies_esc_meas_implemented():
    routes = REPO_ROOT / "routes" / "admin_routes.py"
    for path in (
        "trigger/esc_detection",
        "trigger/esc_reset",
        "trigger/disconnect_esc",
        "trigger/meas_report_in_registration_response",
        "trigger/meas_report_in_heartbeat_response",
    ):
        status, _ = classify_uut_route(path, routes)
        assert status == "implemented", path
