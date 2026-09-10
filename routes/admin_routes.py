"""Admin / test-control routes expected by the WINNF harness."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from database import get_db, reset_db
from models.models import (
    ConditionalRegistration,
    CpiUser,
    EscSensor,
    FccIdRecord,
    PeerSas,
    UserIdRecord,
)
from schemas.admin import (
    BlacklistFccIdRequest,
    BlacklistFccIdSerialRequest,
    ConditionalRegistrationRequest,
    InjectCpiUserRequest,
    InjectFccIdRequest,
    InjectUserIdRequest,
)
from services.blacklist_service import add_fcc_id_blacklist, add_fcc_id_serial_blacklist
from services.cpas_service import (
    get_daily_activities_completed,
    trigger_daily_activities,
)
from services.fad_service import (
    create_full_activity_dump,
    rewrite_esc_sensor_id,
)
from services.mtls_auth import require_admin_certificate

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_certificate)],
)

def _empty_ok() -> Response:
    return Response(status_code=200, content=b"", media_type="application/json")


async def _read_json_object(request: Request) -> dict[str, Any]:
    """Parse JSON object body; invalid/non-object payloads become {} (harness inject)."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@router.post("/reset")
def admin_reset():
    # Do not open a DB session here: reset_db() drop_all/create_all on the
    # process engine while a request-scoped Session is open is unsafe.
    import logging

    from services.request_context import get_request_id

    logging.getLogger(__name__).info(
        "admin_reset request_id=%s", get_request_id() or "-"
    )
    reset_db()
    return _empty_ok()


@router.get("/metrics")
def admin_metrics():
    """Operational metrics snapshot (latency/error counters). Admin mTLS only."""
    from services.metrics import get_metrics

    return JSONResponse(get_metrics().snapshot())


@router.post("/injectdata/fcc_id")
def inject_fcc_id(body: InjectFccIdRequest, db: Session = Depends(get_db)):
    from services.audit_log import append_admin_audit

    existing = db.query(FccIdRecord).filter_by(fcc_id=body.fccId).first()
    if existing:
        existing.fcc_max_eirp = body.fccMaxEirp
    else:
        db.add(FccIdRecord(fcc_id=body.fccId, fcc_max_eirp=body.fccMaxEirp))
    append_admin_audit(
        db,
        "inject_fcc_id",
        {"fccId": body.fccId, "fccMaxEirp": body.fccMaxEirp},
    )
    db.commit()
    return _empty_ok()


@router.post("/injectdata/user_id")
def inject_user_id(body: InjectUserIdRequest, db: Session = Depends(get_db)):
    if not db.query(UserIdRecord).filter_by(user_id=body.userId).first():
        db.add(UserIdRecord(user_id=body.userId))
        db.commit()
    return _empty_ok()


@router.post("/injectdata/conditional_registration")
def inject_conditional_registration(
    body: ConditionalRegistrationRequest, db: Session = Depends(get_db)
):
    for item in body.registrationData:
        fcc_id = item.get("fccId")
        serial = item.get("cbsdSerialNumber")
        if not fcc_id or not serial:
            continue
        existing = (
            db.query(ConditionalRegistration)
            .filter_by(fcc_id=fcc_id, cbsd_serial_number=serial)
            .first()
        )
        payload = json.dumps(item)
        if existing:
            existing.data_json = payload
        else:
            db.add(
                ConditionalRegistration(
                    fcc_id=fcc_id,
                    cbsd_serial_number=serial,
                    data_json=payload,
                )
            )
    db.commit()
    return _empty_ok()


@router.post("/injectdata/cpi_user")
def inject_cpi_user(body: InjectCpiUserRequest, db: Session = Depends(get_db)):
    existing = db.query(CpiUser).filter_by(cpi_id=body.cpiId).first()
    if existing:
        existing.cpi_name = body.cpiName
        existing.cpi_public_key = body.cpiPublicKey
    else:
        db.add(
            CpiUser(
                cpi_id=body.cpiId,
                cpi_name=body.cpiName,
                cpi_public_key=body.cpiPublicKey,
            )
        )
    db.commit()
    return _empty_ok()


@router.post("/injectdata/blacklist_fcc_id")
def blacklist_fcc_id(body: BlacklistFccIdRequest, db: Session = Depends(get_db)):
    add_fcc_id_blacklist(db, body.fccId)
    return _empty_ok()


@router.post("/trigger/meas_report_in_registration_response")
def trigger_meas_report_in_registration(db: Session = Depends(get_db)):
    from services.meas_report import enable_measurement_report_registration

    enable_measurement_report_registration(db)
    return _empty_ok()


@router.post("/trigger/meas_report_in_heartbeat_response")
def trigger_meas_report_in_heartbeat(db: Session = Depends(get_db)):
    from services.meas_report import enable_measurement_report_heartbeat

    enable_measurement_report_heartbeat(db)
    return _empty_ok()


@router.post("/injectdata/fss")
async def inject_fss(request: Request, db: Session = Depends(get_db)):
    from services.data_injection_service import upsert_fss_record

    body = await _read_json_object(request)
    upsert_fss_record(db, body)
    return _empty_ok()


@router.post("/injectdata/wisp")
async def inject_wisp(request: Request, db: Session = Depends(get_db)):
    from services.data_injection_service import upsert_wisp_record

    body = await _read_json_object(request)
    upsert_wisp_record(db, body)
    return _empty_ok()


@router.post("/injectdata/pal_database_record")
async def inject_pal_database_record(request: Request, db: Session = Depends(get_db)):
    from services.pal_service import upsert_pal_records

    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        pass
    upsert_pal_records(db, body)
    return _empty_ok()


@router.post("/injectdata/zone")
async def inject_zone(request: Request, db: Session = Depends(get_db)):
    from services.data_injection_service import persist_zone_data

    body = await _read_json_object(request)
    zone_id = persist_zone_data(db, body)
    return JSONResponse(zone_id)


@router.post("/injectdata/exclusion_zone")
async def inject_exclusion_zone(request: Request, db: Session = Depends(get_db)):
    """Persist GeoJSON exclusion zone + frequencyRanges (EXZ_1)."""
    from fastapi import HTTPException

    from services.exclusion_zone_service import (
        ExclusionZoneError,
        persist_exclusion_zone,
    )

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        persist_exclusion_zone(db, body if isinstance(body, dict) else {})
    except ExclusionZoneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _empty_ok()


@router.post("/trigger/enable_ntia_15_517")
def trigger_enable_ntia_15_517(db: Session = Depends(get_db)):
    """Enable NTIA TR 15-517 coastal exclusion zones (EXZ_2)."""
    from fastapi import HTTPException

    from services.exclusion_zone_service import (
        ExclusionZoneUnavailable,
        enable_ntia_exclusion_zones,
    )

    try:
        enable_ntia_exclusion_zones(db)
    except ExclusionZoneUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _empty_ok()


@router.post("/injectdata/peer_sas")
async def inject_peer_sas(request: Request, db: Session = Depends(get_db)):
    """Persist peer SAS certificateHash and structurally valid peer base URL.

 ``certificateHash`` authorizes inbound SAS↔SAS (FAD). The peer ``url`` is
 required for later CPAS FAD pulls (GRA_5/GRA_6/FAD_2 coordination) but must
 not be gated on DNS at inject time — WInnForum TH placeholders and lab
 loopback bases are registered here; outbound ``fad_client_service`` still
 fail-closes on scheme/origin/resolved-IP SSRF before connecting.
 """
    from services.ssrf import SsrfError, assert_peer_sas_inject_url_structurally_allowed

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    cert_hash = (body.get("certificateHash") or "").strip()
    url = (body.get("url") or "").strip()
    if not cert_hash:
        return _empty_ok()

    url_allowed = False
    if url:
        try:
            assert_peer_sas_inject_url_structurally_allowed(url)
            url_allowed = True
        except SsrfError:
            # Malformed / metadata peer URL: keep identity, do not store URL.
            url_allowed = False

    existing = db.query(PeerSas).filter_by(certificate_hash=cert_hash).first()
    if existing:
        if url_allowed:
            existing.url = url
        # Rejected/missing URL: leave any prior safe url untouched.
    else:
        db.add(
            PeerSas(
                certificate_hash=cert_hash,
                url=url if url_allowed else "",
            )
        )
    db.commit()
    return _empty_ok()


@router.get("/security/trust_material")
def security_trust_material():
    """Inspect on-disk CA/CRL material (Admin mTLS)."""
    from services.trust_reload import trust_material_status

    return trust_material_status()


@router.post("/security/reload_trust_material")
def security_reload_trust_material():
    """Re-read CA/CRL from disk for app-layer validators (Admin mTLS)."""
    from services.trust_reload import reload_trust_material

    return reload_trust_material()


@router.post("/injectdata/esc_sensor")
async def inject_esc_sensor(request: Request, db: Session = Depends(get_db)):
    """Persist EscSensorRecord for inclusion in Full Activity Dump."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    record = body.get("record") or body
    if not isinstance(record, dict):
        return _empty_ok()
    record = dict(record)
    record_id = rewrite_esc_sensor_id(record.get("id"))
    record["id"] = record_id
    from services.concurrency import (
        acquire_iap_admission_xact_lock,
        exclusive_iap_admission,
    )
    from services.data_injection_service import bump_injection_generation

    with exclusive_iap_admission():
        acquire_iap_admission_xact_lock(db)
        existing = db.query(EscSensor).filter_by(record_id=record_id).first()
        payload = json.dumps(record)
        if existing:
            existing.data_json = payload
        else:
            db.add(EscSensor(record_id=record_id, data_json=payload))
        # Invalidate coherent admission markers that predate this ESC geometry.
        bump_injection_generation(db, "esc_sensor")
        db.commit()
    return _empty_ok()


@router.post("/trigger/create_full_activity_dump")
def trigger_create_full_activity_dump(db: Session = Depends(get_db)):
    """Generate FullActivityDump manifesto + activity dump files."""
    create_full_activity_dump(db)
    return _empty_ok()


@router.post("/trigger/daily_activities_immediately")
def trigger_daily_activities_immediately(db: Session = Depends(get_db)):
    """Start CPAS: pull peer FADs and apply conflict resolution."""
    from services.audit_log import append_admin_audit

    # Commit audit before dispatch: trigger_daily_activities commits the
    # running flag via set_admin_flag; avoid a trailing commit racing the
    # certification worker session.
    append_admin_audit(db, "trigger_daily_activities", {}, commit=True)
    trigger_daily_activities(db)
    return _empty_ok()


@router.post("/get_daily_activities_status")
def get_daily_activities_status(db: Session = Depends(get_db)):
    """Return completed=true only after peer FAD sync / conflict application finishes."""
    return JSONResponse({"completed": get_daily_activities_completed(db)})


@router.post("/trigger/load_dpas")
def trigger_load_dpas(db: Session = Depends(get_db)):
    """Load ESC-monitored DPA catalogue from KML and activate all channels."""
    from services.dpa_service import load_dpas

    load_dpas(db)
    return _empty_ok()


@router.post("/trigger/dpa_activation")
async def trigger_dpa_activation(request: Request, db: Session = Depends(get_db)):
    """Activate one catalogue DPA on one validated channel."""
    from services.dpa_service import activate_dpa

    body = await _read_json_object(request)
    activate_dpa(db, body)
    return _empty_ok()


@router.post("/trigger/bulk_dpa_activation")
async def trigger_bulk_dpa_activation(request: Request, db: Session = Depends(get_db)):
    """Bulk activate/deactivate all ESC-monitored DPAs on all catalogue channels."""
    from services.dpa_service import bulk_dpa_activation

    body = await _read_json_object(request)
    raw = body.get("activate") if isinstance(body, dict) else None
    activate = raw if isinstance(raw, bool) else None
    bulk_dpa_activation(db, activate=activate)
    return _empty_ok()


@router.post("/get_ppa_status")
def get_ppa_status(db: Session = Depends(get_db)):
    """WDB/PCR: poll until PPA creation finishes."""
    from services.ppa_service import get_ppa_creation_status

    return JSONResponse(get_ppa_creation_status(db))


@router.post("/trigger/create_ppa")
async def create_ppa(request: Request, db: Session = Depends(get_db)):
    """Create PPA zone from PAL + cluster (+ optional providedContour)."""
    from services.ppa_service import create_ppa as create_ppa_zone

    body = await _read_json_object(request)
    ppa_id = create_ppa_zone(db, body)
    return JSONResponse(ppa_id)


@router.post("/injectdata/database_url")
async def inject_database_url(request: Request, db: Session = Depends(get_db)):
    """FDB/WDB/IPR: accept external DB URL injection (FSS, GWBL, PAL, CPI, …)."""
    from services.data_injection_service import persist_database_url

    body = await _read_json_object(request)
    persist_database_url(db, body)
    return _empty_ok()


@router.post("/trigger/enable_scheduled_daily_activities")
def trigger_enable_scheduled_daily_activities(db: Session = Depends(get_db)):
    """FDB_8: arm CPAS schedule (US/Pacific 02:00–04:00 by default)."""
    from fastapi import HTTPException

    from services.cpas_schedule_service import enable_scheduled_daily_activities

    try:
        enable_scheduled_daily_activities(db)
    except ValueError as exc:
        # Fail closed on bad SAS_CPAS_TIMEZONE — never empty-200 as if armed.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _empty_ok()


@router.post("/injectdata/esc_zone")
async def inject_esc_zone(request: Request, db: Session = Depends(get_db)):
    """Persist ESC zone injection payload (harness InjectEscZone)."""
    from services.data_injection_service import persist_esc_zone

    body = await _read_json_object(request)
    persist_esc_zone(db, body)
    return _empty_ok()


@router.post("/injectdata/cluster_list")
async def inject_cluster_list(request: Request, db: Session = Depends(get_db)):
    """Persist PPA cluster list injection (harness InjectClusterList)."""
    from services.data_injection_service import persist_cluster_list

    body = await _read_json_object(request)
    persist_cluster_list(db, body)
    return _empty_ok()


@router.post("/injectdata/blacklist_fcc_id_and_serial_number")
def blacklist_fcc_id_and_serial_number(
    body: BlacklistFccIdSerialRequest, db: Session = Depends(get_db)
):
    """Blacklist a specific CBSD (fccId + cbsdSerialNumber); enforced on REG/SIQ/GRA/HBT."""
    add_fcc_id_serial_blacklist(db, body.fccId, body.cbsdSerialNumber)
    return _empty_ok()


@router.post("/injectdata/sas_admin")
async def inject_sas_admin(request: Request, db: Session = Depends(get_db)):
    """Persist SasAdministrator record injection."""
    from services.data_injection_service import persist_sas_admin

    body = await _read_json_object(request)
    persist_sas_admin(db, body)
    return _empty_ok()


@router.post("/trigger/esc_detection")
async def trigger_esc_detection(request: Request, db: Session = Depends(get_db)):
    from services.esc_admin_service import apply_esc_detection

    body = await _read_json_object(request)
    apply_esc_detection(db, body)
    return _empty_ok()


@router.post("/trigger/esc_reset")
def trigger_esc_reset(db: Session = Depends(get_db)):
    from services.esc_admin_service import reset_esc_zone

    reset_esc_zone(db)
    return _empty_ok()


@router.post("/trigger/dpa_deactivation")
async def trigger_dpa_deactivation(request: Request, db: Session = Depends(get_db)):
    """Deactivate one DPA on one channel (selective; harness TriggerDpaDeactivation)."""
    from services.dpa_service import deactivate_dpa

    body = await _read_json_object(request)
    deactivate_dpa(db, body)
    return _empty_ok()


@router.post("/trigger/disconnect_esc")
def trigger_disconnect_esc(db: Session = Depends(get_db)):
    from services.esc_admin_service import disconnect_esc

    disconnect_esc(db)
    return _empty_ok()


@router.post("/query/propagation_and_antenna_model")
async def query_propagation_and_antenna_model(request: Request):
    """PAT Admin query — path loss + antenna gains (modelType 1/2/3)."""
    from services.propagation import (
        PropagationRequestError,
        PropagationUnavailableError,
        compute_propagation_and_antenna_model,
        load_reference_engines,
    )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request must be a JSON object"}, status_code=400)

    try:
        engines = load_reference_engines()
        result = compute_propagation_and_antenna_model(body, engines=engines)
    except PropagationRequestError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except PropagationUnavailableError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)
    return JSONResponse(result, status_code=200)
