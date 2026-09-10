"""Shared measurement-report helpers for Registration / SIQ / Grant / Heartbeat."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from services.spectrum_inquiry_service import CBRS_HIGH_HZ, CBRS_LOW_HZ

MISSING_PARAM = 102
INVALID_PARAM = 103

MEAS_WITHOUT_GRANT = "RECEIVED_POWER_WITHOUT_GRANT"
MEAS_WITH_GRANT = "RECEIVED_POWER_WITH_GRANT"
FLAG_MEAS_REG = "meas_report_registration"
FLAG_MEAS_HBT = "meas_report_heartbeat"
FLAG_DPA_ACTIVE = "dpa_active"

# Full CBRS coverage for WITHOUT_GRANT: 15×10 MHz channels (3550–3700 MHz).
MIN_WITHOUT_GRANT_REPORTS = 15
EXPECTED_BANDWIDTH_HZ = 10_000_000


def admin_flag_set(db: Session, kind: str) -> bool:
    return db.query(AdminInjectedData).filter_by(kind=kind).first() is not None


def set_admin_flag(db: Session, kind: str, payload: dict[str, Any] | None = None) -> None:
    import json

    existing = db.query(AdminInjectedData).filter_by(kind=kind).first()
    data = json.dumps(payload or {})
    if existing:
        existing.data_json = data
    else:
        db.add(AdminInjectedData(kind=kind, data_json=data))
    db.commit()


def clear_admin_flags(db: Session, kind: str) -> None:
    db.query(AdminInjectedData).filter_by(kind=kind).delete()
    db.commit()


def enable_measurement_report_registration(db: Session) -> None:
    """Arm measReportConfig on subsequent Registration / SIQ / Grant (MES)."""
    from services.clock import utc_now

    set_admin_flag(
        db,
        FLAG_MEAS_REG,
        {"enabled": True, "enabledAt": utc_now().replace(microsecond=0).isoformat()},
    )


def enable_measurement_report_heartbeat(db: Session) -> None:
    """Arm measReportConfig on subsequent Heartbeat responses (MES)."""
    from services.clock import utc_now

    set_admin_flag(
        db,
        FLAG_MEAS_HBT,
        {"enabled": True, "enabledAt": utc_now().replace(microsecond=0).isoformat()},
    )


def cbsd_meas_capabilities(registration_json: str | None) -> list[str]:
    import json

    try:
        data = json.loads(registration_json or "{}")
    except json.JSONDecodeError:
        return []
    meas = data.get("measCapability") or []
    return list(meas) if isinstance(meas, list) else []


def validate_meas_report(
    meas_report: Any | None,
    *,
    require_full_cbrs: bool = False,
) -> int | None:
    """
    Validate a CBSD measReport object.
    Returns responseCode on failure, or None if OK.
    """
    if meas_report is None:
        return MISSING_PARAM
    if not isinstance(meas_report, dict):
        return INVALID_PARAM
    if "rcvdPowerMeasReports" not in meas_report:
        return MISSING_PARAM
    reports = meas_report.get("rcvdPowerMeasReports")
    if not isinstance(reports, list) or len(reports) == 0:
        return MISSING_PARAM
    if require_full_cbrs and len(reports) < MIN_WITHOUT_GRANT_REPORTS:
        return MISSING_PARAM

    for item in reports:
        if not isinstance(item, dict):
            return INVALID_PARAM
        if (
            "measFrequency" not in item
            or "measBandwidth" not in item
            or "measRcvdPower" not in item
        ):
            return MISSING_PARAM
        try:
            freq = int(item["measFrequency"])
            bw = int(item["measBandwidth"])
        except (TypeError, ValueError):
            return INVALID_PARAM
        if bw != EXPECTED_BANDWIDTH_HZ:
            return INVALID_PARAM
        # Frequency must fall within CBRS band.
        if freq < CBRS_LOW_HZ or freq >= CBRS_HIGH_HZ:
            return INVALID_PARAM
    return None
