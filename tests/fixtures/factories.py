"""Deterministic entity factories for SAS domain tests (no harness fixture IDs)."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from models.models import (
    AdminInjectedData,
    Cbsd,
    ConditionalRegistration,
    EscSensor,
    FccIdRecord,
    Grant,
    PalRecord,
    PeerSas,
    UserIdRecord,
)

_counter = itertools.count(1)


def reset_factory_counter(start: int = 1) -> None:
    """Reset synthetic ID sequence (call from isolated test fixtures)."""
    global _counter
    _counter = itertools.count(start)


def _next_token(prefix: str) -> str:
    return f"{prefix}-{next(_counter):04d}"


def utc_now() -> datetime:
    """Naive UTC wall-clock matching ORM columns (models still store naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_cbsd(
    db: Session,
    *,
    fcc_id: str | None = None,
    cbsd_serial_number: str | None = None,
    user_id: str | None = None,
    cbsd_id: str | None = None,
    cbsd_category: str = "A",
    certificate_hash: str | None = None,
    registration: dict[str, Any] | None = None,
    commit: bool = True,
) -> Cbsd:
    serial = cbsd_serial_number or _next_token("serial")
    fcc = fcc_id or _next_token("fcc")
    uid = user_id or _next_token("user")
    cid = cbsd_id or f"{fcc}.{serial}"
    payload = registration or {
        "fccId": fcc,
        "cbsdSerialNumber": serial,
        "userId": uid,
        "cbsdCategory": cbsd_category,
    }
    row = Cbsd(
        cbsd_id=cid,
        fcc_id=fcc,
        user_id=uid,
        cbsd_serial_number=serial,
        cbsd_category=cbsd_category,
        certificate_hash=certificate_hash,
        registration_json=json.dumps(payload),
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_grant(
    db: Session,
    cbsd: Cbsd,
    *,
    grant_id: str | None = None,
    low_hz: int = 3550_000_000,
    high_hz: int = 3560_000_000,
    max_eirp: float = 20.0,
    channel_type: str = "GAA",
    authorized: bool = True,
    terminated: bool = False,
    lifecycle_state: str | None = None,
    commit: bool = True,
) -> Grant:
    gid = grant_id or _next_token("grant")
    if cbsd.id is None:
        db.flush()
    if lifecycle_state is None:
        if terminated:
            lifecycle_state = "TERMINATED"
        elif authorized:
            lifecycle_state = "AUTHORIZED"
        else:
            lifecycle_state = "GRANTED"
    row = Grant(
        grant_id=gid,
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        channel_type=channel_type,
        low_frequency=low_hz,
        high_frequency=high_hz,
        max_eirp=max_eirp,
        grant_expire_time=utc_now() + timedelta(hours=1),
        heartbeat_interval=60,
        authorized=authorized,
        terminated=terminated,
        lifecycle_state=lifecycle_state,
        grant_json="{}",
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_pal(
    db: Session,
    *,
    pal_id: str | None = None,
    user_id: str | None = None,
    low_hz: int = 3550_000_000,
    high_hz: int = 3560_000_000,
    license_status: str = "VALID",
    license_expiration: str | None = None,
    record_json: dict[str, Any] | None = None,
    commit: bool = True,
) -> PalRecord:
    """Create a PAL record. ``record_json``, when given, is stored verbatim so
    ``services.pal_service.load_pal_records`` returns exactly that shape;
    otherwise ``pal_record_to_dict`` derives channelAssignment/palId/userId
    from the row columns (see services/pal_service.py).
    """
    pid = pal_id or _next_token("pal")
    uid = user_id or _next_token("pal-user")
    body = record_json if record_json is not None else {
        "palId": pid,
        "userId": uid,
        "channelAssignment": {
            "primaryAssignment": {
                "lowFrequency": low_hz,
                "highFrequency": high_hz,
            }
        },
        "licenseStatus": license_status,
    }
    if license_expiration:
        body.setdefault("license", {"licenseExpiration": license_expiration})
    row = PalRecord(
        pal_id=pid,
        user_id=uid,
        low_frequency=low_hz,
        high_frequency=high_hz,
        license_status=license_status,
        license_expiration=license_expiration,
        record_json=json.dumps(body),
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_ppa_zone(
    db: Session,
    *,
    zone_id: str | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> AdminInjectedData:
    """PPA is represented as admin-injected zone data (kind=zone)."""
    zid = zone_id or _next_token("ppa")
    body = payload or {"record": {"id": zid, "type": "PPA"}}
    row = AdminInjectedData(kind="zone", data_json=json.dumps(body))
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_ppa_with_pal(
    db: Session,
    *,
    pal: PalRecord,
    cbsd_reference_ids: list[str],
    zone: dict[str, Any] | None = None,
    zone_id: str | None = None,
    commit: bool = True,
) -> AdminInjectedData:
    """Admin-injected PPA (kind=zone) linking a PAL and cluster of cbsdIds."""
    zid = zone_id or _next_token("ppa")
    body = {
        "record": {
            "id": zid,
            "type": "PPA",
            "usage": "PPA",
            "ppaInfo": {
                "palId": [pal.pal_id],
                "cbsdReferenceId": cbsd_reference_ids,
            },
            "zone": zone or square_polygon(-105.27, 40.0),
        }
    }
    row = AdminInjectedData(kind="zone", data_json=json.dumps(body))
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_dpa(
    db: Session,
    *,
    dpa_id: str | None = None,
    active: bool = False,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> AdminInjectedData:
    """DPA state stored as admin-injected / flag payloads (no dedicated ORM table)."""
    did = dpa_id or _next_token("dpa")
    body = payload or {"dpaId": did, "active": active}
    kind = "dpa_active" if active else "dpa"
    row = AdminInjectedData(kind=kind, data_json=json.dumps(body))
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_fss(
    db: Session,
    *,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> AdminInjectedData:
    body = payload or {
        "record": {
            "id": _next_token("fss"),
            "type": "FSS",
        }
    }
    row = AdminInjectedData(kind="fss", data_json=json.dumps(body))
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def esc_installation_param(
    latitude: float,
    longitude: float,
    *,
    height: float = 3.0,
    height_type: str = "AGL",
    antenna_azimuth: float = 0.0,
    pattern_gain_dbi: float = 30.0,
    **extra: Any,
) -> dict[str, Any]:
    """Minimal official-shaped ESC installationParam for unit tests."""
    install: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "height": height,
        "heightType": height_type,
        "antennaAzimuth": antenna_azimuth,
        "azimuthRadiationPattern": [
            {"angle": i, "gain": pattern_gain_dbi} for i in range(360)
        ],
    }
    install.update(extra)
    return install


def make_esc_sensor(
    db: Session,
    *,
    record_id: str | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> EscSensor:
    rid = record_id or _next_token("esc")
    data = payload or {
        "id": rid,
        "installationParam": esc_installation_param(39.0, -77.0),
    }
    row = EscSensor(record_id=rid, data_json=json.dumps(data))
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_peer_sas(
    db: Session,
    *,
    certificate_hash: str | None = None,
    url: str | None = None,
    commit: bool = True,
) -> PeerSas:
    row = PeerSas(
        certificate_hash=certificate_hash or _next_token("peer-cert"),
        url=url or f"https://{_next_token('peer')}.example.test/v1.3",
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_fcc_id(
    db: Session,
    *,
    fcc_id: str | None = None,
    fcc_max_eirp: float = 47.0,
    commit: bool = True,
) -> FccIdRecord:
    row = FccIdRecord(fcc_id=fcc_id or _next_token("fcc"), fcc_max_eirp=fcc_max_eirp)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_user_id(
    db: Session,
    *,
    user_id: str | None = None,
    commit: bool = True,
) -> UserIdRecord:
    row = UserIdRecord(user_id=user_id or _next_token("user"))
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def make_conditionals(
    db: Session,
    *,
    fcc_id: str,
    cbsd_serial_number: str,
    data: dict[str, Any],
    commit: bool = True,
) -> ConditionalRegistration:
    row = ConditionalRegistration(
        fcc_id=fcc_id,
        cbsd_serial_number=cbsd_serial_number,
        data_json=json.dumps(data),
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def square_polygon(lon: float, lat: float, delta: float = 0.05) -> dict[str, Any]:
    """Closed GeoJSON Polygon ring (RFC 7946 [lon, lat] order) centered on a point."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon - delta, lat - delta],
                [lon - delta, lat + delta],
                [lon + delta, lat + delta],
                [lon + delta, lat - delta],
                [lon - delta, lat - delta],
            ]
        ],
    }


def full_cbrs_meas_report() -> dict[str, Any]:
    """15 x 10 MHz measReport entries spanning the full 3550-3700 MHz CBRS band."""
    reports = []
    low = 3_550_000_000
    step = 10_000_000
    for i in range(15):
        freq = low + i * step
        reports.append(
            {
                "measFrequency": freq,
                "measBandwidth": step,
                "measRcvdPower": -80.0,
            }
        )
    return {"rcvdPowerMeasReports": reports}


def cat_a_install(
    lat: float = 39.1,
    lon: float = -94.58,
    *,
    indoor: bool = True,
    height: float = 3.0,
) -> dict[str, Any]:
    """Category A installationParam with common AGL height + indoor flag.

    Default coordinates are in the US Midwest — outside NRQZ, Table Mountain
    coordination distances, and FCC field-office radii (not a harness fixture).
    """
    return {
        "latitude": lat,
        "longitude": lon,
        "height": height,
        "heightType": "AGL",
        "indoorDeployment": indoor,
    }
