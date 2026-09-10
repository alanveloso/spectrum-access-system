"""correlation ID, audit, metrics, redaction, failure dumps."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services.audit_log import KIND_ADMIN_AUDIT, append_admin_audit, redact_audit_detail
from services.logging_redaction import RedactingFilter, redact_mapping
from services.metrics import get_metrics
from services.request_context import (
    RequestContext,
    get_request_id,
    reset_request_context,
    set_request_context,
)
from tools.winnforum.failure_dump import write_failure_dumps
from tools.winnforum.unittest_parse import parse_unittest_output

client = TestClient(app)

SAMPLE_LOG = """\
test_WINNF_FT_S_REG_1 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... ok
test_WINNF_FT_S_REG_2 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... FAIL
test_WINNF_FT_S_REG_3 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... ERROR

======================================================================
FAIL: test_WINNF_FT_S_REG_2
----------------------------------------------------------------------
AssertionError: expected

----------------------------------------------------------------------
Ran 3 tests in 1.234s

FAILED (failures=1, errors=1)
"""


@pytest.fixture(autouse=True)
def _reset_metrics():
    get_metrics().reset()
    yield
    get_metrics().reset()


def test_correlation_id_echo_and_generate():
    resp = client.get("/admin/metrics", headers={"X-Request-ID": "corr-fixed-1"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == "corr-fixed-1"

    resp2 = client.get("/admin/metrics")
    assert resp2.status_code == 200
    generated = resp2.headers.get("X-Request-ID")
    assert generated
    assert generated != "corr-fixed-1"


def test_metrics_increment_after_request():
    before = get_metrics().snapshot()
    assert before["httpRequests"] == []
    client.get("/admin/metrics")
    snap = get_metrics().snapshot()
    assert any(r["path"] == "/admin/metrics" for r in snap["httpRequests"])
    assert "/admin/metrics" in snap["httpLatencyMs"]


def test_admin_inject_writes_audit(db_session):
    append_admin_audit(
        db_session,
        "inject_fcc_id",
        {"fccId": "fcc-obs-1", "password": "should-not-persist", "fccMaxEirp": 30},
    )
    db_session.commit()
    rows = db_session.query(AdminInjectedData).filter_by(kind=KIND_ADMIN_AUDIT).all()
    assert rows
    payload = json.loads(rows[-1].data_json)
    assert payload["event"] == "inject_fcc_id"
    assert payload["fccId"] == "fcc-obs-1"
    assert payload["password"] == "[REDACTED]"
    assert "should-not-persist" not in rows[-1].data_json


def test_admin_http_inject_audit_via_client(db_session):
    token = set_request_context(RequestContext(request_id="http-audit-1"))
    try:
        resp = client.post(
            "/admin/injectdata/fcc_id",
            json={"fccId": "fcc-http-1", "fccMaxEirp": 37},
            headers={"X-Request-ID": "http-audit-1"},
        )
    finally:
        reset_request_context(token)
    assert resp.status_code == 200
    # Client uses process DB; query via dependency session may differ — use client path
    # by reading through a fresh SessionLocal.
    import database

    session = database.SessionLocal()
    try:
        rows = (
            session.query(AdminInjectedData)
            .filter_by(kind=KIND_ADMIN_AUDIT)
            .all()
        )
        assert any(
            "inject_fcc_id" in (r.data_json or "") and "fcc-http-1" in (r.data_json or "")
            for r in rows
        )
    finally:
        session.close()


def test_admin_reset_logs_correlation_without_open_session(caplog):
    """Regression: reset must not hold a request Session across drop_all."""
    with caplog.at_level(logging.INFO, logger="routes.admin_routes"):
        resp = client.post(
            "/admin/reset",
            headers={"X-Request-ID": "reset-corr-1"},
        )
    assert resp.status_code == 200
    assert resp.content == b""
    assert any(
        "admin_reset" in r.getMessage() and "reset-corr-1" in r.getMessage()
        for r in caplog.records
    )


def test_daily_activities_audit_commits_before_dispatch(db_session, monkeypatch):
    """Audit row must be committed before trigger_daily_activities runs."""
    seen: list[str] = []

    def _fake_trigger(db):
        rows = db.query(AdminInjectedData).filter_by(kind=KIND_ADMIN_AUDIT).all()
        assert any("trigger_daily_activities" in (r.data_json or "") for r in rows)
        seen.append("ok")

    monkeypatch.setattr(
        "routes.admin_routes.trigger_daily_activities",
        _fake_trigger,
    )
    token = set_request_context(RequestContext(request_id="daily-audit-1"))
    try:
        resp = client.post(
            "/admin/trigger/daily_activities_immediately",
            headers={"X-Request-ID": "daily-audit-1"},
        )
    finally:
        reset_request_context(token)
    assert resp.status_code == 200
    assert seen == ["ok"]


def test_redaction_masks_pem_and_password():
    data = redact_mapping(
        {
            "cpiPrivateKey": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            "okField": "visible",
            "nested": {"db_sync_password": "secret"},
        }
    )
    assert data["cpiPrivateKey"] == "[REDACTED]"
    assert data["okField"] == "visible"
    assert data["nested"]["db_sync_password"] == "[REDACTED]"
    assert redact_audit_detail({"token": "xyz"})["token"] == "[REDACTED]"


def test_redacting_filter_on_logger(caplog):
    logger = logging.getLogger("test_p8_redact")
    handler = logging.StreamHandler()
    filt = RedactingFilter()
    filt.name = "sas_redacting_filter"
    handler.addFilter(filt)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger="test_p8_redact"):
        logger.info("password=super-secret-value hello")
    # Filter mutates record.msg; caplog may see original — assert filter returns True
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="password=super-secret-value",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "[REDACTED]" in str(record.msg)
    logger.removeHandler(handler)


def test_request_context_bind_item():
    token = set_request_context(RequestContext(request_id="r1"))
    try:
        assert get_request_id() == "r1"
        from services.request_context import bind_batch, bind_item_index, context_as_dict

        bind_batch(batch_id="r1")
        bind_item_index(2)
        assert context_as_dict() == {
            "requestId": "r1",
            "batchId": "r1",
            "itemIndex": 2,
        }
    finally:
        reset_request_context(token)


def test_failure_dump_writes_per_case(tmp_path: Path):
    parsed = parse_unittest_output(SAMPLE_LOG)
    dirs = write_failure_dumps(tmp_path, parsed, harness_log_text=SAMPLE_LOG)
    assert len(dirs) == 2
    names = {d.name for d in dirs}
    assert "test_WINNF_FT_S_REG_2" in names
    assert "test_WINNF_FT_S_REG_3" in names
    case_json = json.loads((dirs[0] / "case.json").read_text(encoding="utf-8"))
    assert case_json["status"] in {"failed", "error"}
    excerpt = (tmp_path / "failures" / "test_WINNF_FT_S_REG_2" / "harness_excerpt.txt").read_text(
        encoding="utf-8"
    )
    assert "REG_2" in excerpt or "AssertionError" in excerpt
