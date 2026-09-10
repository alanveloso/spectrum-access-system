"""GrantRequestItem must accept protocol measReport (extra=forbid otherwise)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from schemas.common import MeasReport
from schemas.grant import GrantRequestItem
from services.cbsd_batch import parse_item_batch
from services.error_handlers import INVALID_VALUE, MISSING_PARAM
from services.grant_service import process_grant
from services.meas_report import (
    FLAG_MEAS_REG,
    MEAS_WITHOUT_GRANT,
    set_admin_flag,
    validate_meas_report,
)
from services.spectrum_inquiry_service import CBRS_LOW_HZ
from tests.fixtures.factories import make_cbsd

SUCCESS = 0
CHANNEL_HZ = 10_000_000
FULL_CBRS_REPORTS = 15


def _op_param(*, low_hz=CBRS_LOW_HZ, high_hz=CBRS_LOW_HZ + CHANNEL_HZ, max_eirp=20.0):
    return {
        "maxEirp": max_eirp,
        "operationFrequencyRange": {"lowFrequency": low_hz, "highFrequency": high_hz},
    }


def _full_cbrs_reports(
    *, n: int = FULL_CBRS_REPORTS, bandwidth_hz: int = CHANNEL_HZ, include_power: bool = True
) -> list[dict]:
    rows = []
    for i in range(n):
        item: dict = {
            "measFrequency": CBRS_LOW_HZ + i * CHANNEL_HZ,
            "measBandwidth": bandwidth_hz,
        }
        if include_power:
            item["measRcvdPower"] = -100
        rows.append(item)
    return rows


def _grant_item(cbsd_id: str, meas_report: dict | None, *, include_meas_key: bool = True) -> dict:
    body: dict = {"cbsdId": cbsd_id, "operationParam": _op_param()}
    if include_meas_key:
        body["measReport"] = meas_report
    return body


def _cbsd_with_without_grant(db, **kwargs):
    cbsd = make_cbsd(db, cbsd_category="A", **kwargs)
    registration = json.loads(cbsd.registration_json or "{}")
    registration["measCapability"] = [MEAS_WITHOUT_GRANT]
    cbsd.registration_json = json.dumps(registration)
    db.commit()
    return cbsd


def test_a_unknown_field_still_extra_forbidden():
    with pytest.raises(ValidationError) as exc:
        GrantRequestItem.model_validate(
            {
                "cbsdId": "fcc.serial",
                "operationParam": _op_param(),
                "someUnknownField": True,
            }
        )
    assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())
    parsed = parse_item_batch(
        [{"cbsdId": "fcc.serial", "operationParam": _op_param(), "someUnknownField": True}],
        item_model=GrantRequestItem,
    )
    assert parsed.schema_error_codes[0] == INVALID_VALUE
    assert parsed.items_for_service == []


def test_b_meas_report_parses_and_is_preserved():
    report = {"rcvdPowerMeasReports": _full_cbrs_reports()}
    item = GrantRequestItem.model_validate(
        {"cbsdId": "fcc.serial", "operationParam": _op_param(), "measReport": report}
    )
    assert isinstance(item.measReport, MeasReport)
    assert item.measReport.rcvdPowerMeasReports is not None
    assert len(item.measReport.rcvdPowerMeasReports) == FULL_CBRS_REPORTS


def test_c_batch_adapter_forwards_meas_report_to_service_dict():
    report = {"rcvdPowerMeasReports": _full_cbrs_reports()}
    raw = [_grant_item("fcc.serial", report)]
    parsed = parse_item_batch(raw, item_model=GrantRequestItem)
    assert parsed.schema_error_codes == [None]
    forwarded = parsed.items_for_service[0]
    assert "measReport" in forwarded
    assert validate_meas_report(forwarded["measReport"], require_full_cbrs=True) is None


def test_d_validate_meas_report_receives_forwarded_payload(db_session, monkeypatch):
    cbsd = _cbsd_with_without_grant(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)
    report = {"rcvdPowerMeasReports": _full_cbrs_reports()}
    parsed = parse_item_batch(
        [_grant_item(cbsd.cbsd_id, report)], item_model=GrantRequestItem
    )
    seen: list = []
    real = validate_meas_report

    def _capture(meas_report, *, require_full_cbrs=False):
        seen.append({"meas_report": meas_report, "require_full_cbrs": require_full_cbrs})
        return real(meas_report, require_full_cbrs=require_full_cbrs)

    monkeypatch.setattr("services.meas_report.validate_meas_report", _capture)

    resp = process_grant(db_session, parsed.items_for_service)
    assert seen, "validate_meas_report was not invoked"
    assert seen[0]["require_full_cbrs"] is True
    assert seen[0]["meas_report"]["rcvdPowerMeasReports"]
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0].get("grantId")


def test_e_valid_full_report_grant_succeeds(db_session):
    cbsd = _cbsd_with_without_grant(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)
    report = {"rcvdPowerMeasReports": _full_cbrs_reports()}
    assert validate_meas_report(report, require_full_cbrs=True) is None
    parsed = parse_item_batch(
        [_grant_item(cbsd.cbsd_id, report)], item_model=GrantRequestItem
    )
    resp = process_grant(db_session, parsed.items_for_service)
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0].get("grantId")


def test_f_invalid_matrix_generic_codes(db_session):
    cbsd = _cbsd_with_without_grant(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)
    cases = [
        (_grant_item(cbsd.cbsd_id, {"rcvdPowerMeasReports": _full_cbrs_reports(include_power=False)}), MISSING_PARAM),
        (_grant_item(cbsd.cbsd_id, {"rcvdPowerMeasReports": _full_cbrs_reports(n=10)}), MISSING_PARAM),
        (_grant_item(cbsd.cbsd_id, {}), MISSING_PARAM),
        (_grant_item(cbsd.cbsd_id, None, include_meas_key=False), MISSING_PARAM),
        (
            _grant_item(
                cbsd.cbsd_id,
                {"rcvdPowerMeasReports": _full_cbrs_reports(bandwidth_hz=15_000_000)},
            ),
            103,
        ),
    ]
    for raw, expected in cases:
        parsed = parse_item_batch([raw], item_model=GrantRequestItem)
        assert parsed.schema_error_codes == [None], raw
        resp = process_grant(db_session, parsed.items_for_service)
        assert resp[0]["response"]["responseCode"] == expected, raw


def test_g_no_trigger_grant_without_meas_report_unchanged(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    parsed = parse_item_batch(
        [_grant_item(cbsd.cbsd_id, None, include_meas_key=False)],
        item_model=GrantRequestItem,
    )
    assert parsed.schema_error_codes == [None]
    assert "measReport" not in parsed.items_for_service[0]
    resp = process_grant(db_session, parsed.items_for_service)
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0].get("grantId")
