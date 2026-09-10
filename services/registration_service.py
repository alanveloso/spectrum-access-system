"""Registration business logic aligned with WINNF_FT_S_REG expectations."""

from __future__ import annotations

import json
import re
from typing import Any

from adapters.device import ConsumerAdapter
from sqlalchemy.orm import Session

from models.models import (
    Cbsd,
    ConditionalRegistration,
    CpiUser,
    FccIdRecord,
    UserIdRecord,
)
from services.blacklist_service import is_cbsd_blacklisted
from services.consumer_semantics import (
    consumer_view_from_payload,
    geo_coordinates,
)
from services.cpi_signature import (
    decode_cpi_signed_data as _decode_cpi_signed_data,
    structural_cpi_error,
    verify_cpi_signature,
)

# WINNF response codes
SUCCESS = 0
VERSION_UNSUPPORTED = 100
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
PENDING = 200

VALID_MEAS = {
    "RECEIVED_POWER_WITHOUT_GRANT",
    "RECEIVED_POWER_WITH_GRANT",
}
VALID_USER_ID = re.compile(r"^[A-Za-z0-9_:-]+$")

def _cat_a_outdoor_haat_exceeds_limit(installation: dict[str, Any]) -> bool:
    """Return True if Cat A outdoor HAAT exceeds the 6 m Part 96 limit.

 Uses the injectable ``HaatProvider`` (production: USGS NED 1″ / WInnForum
 algorithm). Fail-closed when terrain data is missing or unreadable.
 Fixture-coordinate lookup tables are forbidden (AGENTS.md).
 """
    from services.terrain.haat import cat_a_outdoor_haat_invalid

    return cat_a_outdoor_haat_invalid(installation)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_conditionals(db: Session, fcc_id: str, serial: str) -> dict[str, Any]:
    row = (
        db.query(ConditionalRegistration)
        .filter_by(fcc_id=fcc_id, cbsd_serial_number=serial)
        .first()
    )
    if not row:
        return {}
    payload = json.loads(row.data_json)
    return payload if isinstance(payload, dict) else {}


def _merge_registration(
    request: dict[str, Any],
    conditionals: dict[str, Any],
    *,
    verified_cpi_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge request with preloaded conditionals and verified CPI-signed params."""
    merged = _deep_merge(conditionals, {k: v for k, v in request.items() if v is not None})

    signed = verified_cpi_payload
    if signed:
        if "installationParam" in signed:
            existing = merged.get("installationParam") or {}
            # Prefer CPI-signed installation params over cleartext/conditionals
            merged["installationParam"] = _deep_merge(
                existing, signed["installationParam"]
            )
        if "fccId" in signed and not merged.get("fccId"):
            merged["fccId"] = signed["fccId"]
        if "cbsdSerialNumber" in signed and not merged.get("cbsdSerialNumber"):
            merged["cbsdSerialNumber"] = signed["cbsdSerialNumber"]

    return merged


def _missing_required_fields(request: dict[str, Any]) -> bool:
    return not (
        request.get("userId")
        and request.get("fccId")
        and request.get("cbsdSerialNumber")
    )


def _cpi_missing_params(request: dict[str, Any], db: Session) -> int | None:
    """Return MISSING_PARAM/INVALID_PARAM if CPI structure is incomplete, else None."""
    del db  # lookup happens in cryptographic verify
    cpi_sig = request.get("cpiSignatureData")
    if not cpi_sig:
        return None
    return structural_cpi_error(cpi_sig)


def _verify_request_cpi(
    request: dict[str, Any], db: Session
) -> tuple[int | None, dict[str, Any] | None]:
    """Cryptographically verify ``cpiSignatureData`` when present.

 Returns ``(error_code, verified_payload)``. Payload is only set on success.
 """
    cpi_sig = request.get("cpiSignatureData")
    if not cpi_sig:
        return None, None

    structural = structural_cpi_error(cpi_sig)
    if structural is not None:
        return structural, None

    # Peek cpiId only to load the injected public key; payload is not trusted yet.
    peek = _decode_cpi_signed_data(cpi_sig) or {}
    prof = peek.get("professionalInstallerData") or {}
    cpi_id = prof.get("cpiId")
    cpi_user = db.query(CpiUser).filter_by(cpi_id=cpi_id).first() if cpi_id else None
    public_pem = cpi_user.cpi_public_key if cpi_user else None

    result = verify_cpi_signature(
        cpi_sig,
        public_key_pem=public_pem,
        request_fcc_id=str(request.get("fccId") or ""),
        request_serial=str(request.get("cbsdSerialNumber") or ""),
    )
    if not result.ok:
        return result.response_code or INVALID_PARAM, None
    return None, result.payload


def _field_missing(container: dict[str, Any], field: str) -> bool:
    """True when ``field`` is absent or explicitly null (schema dump artefact)."""
    return field not in container or container.get(field) is None


def _has_pending_params(merged: dict[str, Any]) -> bool:
    """True when conditional/required installation params are incomplete."""
    category = merged.get("cbsdCategory")
    installation = merged.get("installationParam") or {}
    air = merged.get("airInterface")

    if not category or not air or _field_missing(air or {}, "radioTechnology"):
        return True
    if not installation:
        return True

    required_common = ["latitude", "longitude", "height", "heightType"]
    for field in required_common:
        if _field_missing(installation, field):
            return True

    if category == "A":
        if _field_missing(installation, "indoorDeployment"):
            return True
    elif category == "B":
        for field in ("antennaAzimuth", "antennaGain", "antennaBeamwidth"):
            if _field_missing(installation, field):
                return True

    return False


def _validate_params(
    request: dict[str, Any], merged: dict[str, Any], db: Session
) -> int | None:
    """Return INVALID_PARAM code if validation fails, else None."""
    fcc_id = merged.get("fccId") or ""
    user_id = merged.get("userId") or ""
    serial = merged.get("cbsdSerialNumber") or ""
    category = merged.get("cbsdCategory")
    installation = merged.get("installationParam") or {}
    meas = merged.get("measCapability")

    if len(str(serial)) > 64:
        return INVALID_PARAM
    if len(str(fcc_id)) > 20:
        return INVALID_PARAM
    if not VALID_USER_ID.match(str(user_id)):
        return INVALID_PARAM

    fcc_row = db.query(FccIdRecord).filter_by(fcc_id=fcc_id).first()
    user_row = db.query(UserIdRecord).filter_by(user_id=user_id).first()
    if not fcc_row or not user_row:
        return INVALID_PARAM

    if meas is not None:
        if not isinstance(meas, list):
            return INVALID_PARAM
        for item in meas:
            if item not in VALID_MEAS:
                return INVALID_PARAM

    # Optional fields may be present-but-None once a request has round-tripped
    # through the Pydantic schema layer (which emits every declared key). Treat
    # None the same as "not provided" so schema-validated and raw-dict callers
    # behave identically.
    if installation.get("latitude") is not None:
        lat = installation["latitude"]
        if not isinstance(lat, (int, float)) or lat < -90 or lat > 90:
            return INVALID_PARAM
    if installation.get("longitude") is not None:
        lon = installation["longitude"]
        if not isinstance(lon, (int, float)) or lon < -180 or lon > 180:
            return INVALID_PARAM
    if installation.get("antennaAzimuth") is not None:
        az = installation["antennaAzimuth"]
        if not isinstance(az, (int, float)) or az < 0 or az >= 360:
            return INVALID_PARAM
    if installation.get("heightType") is not None and installation["heightType"] not in (
        "AGL",
        "AMSL",
    ):
        return INVALID_PARAM

    eirp = installation.get("eirpCapability")
    if eirp is not None:
        max_eirp = fcc_row.fcc_max_eirp if fcc_row else 47.0
        if eirp > max_eirp:
            return INVALID_PARAM
        if category == "A" and eirp > 30:
            return INVALID_PARAM

    # Cat A outdoor: HAAT must be ≤ 6 m (47 CFR § 96.43 / WINNF REG.7 CBSD#8).
    # Only evaluate when installation geometry required for HAAT is present;
    # incomplete params fall through to PENDING via _has_pending_params.
    if category == "A" and installation.get("indoorDeployment") is False:
        height = installation.get("height")
        height_type = installation.get("heightType")
        lat = installation.get("latitude")
        lon = installation.get("longitude")
        if height_type == "AGL" and isinstance(height, (int, float)) and height > 6:
            return INVALID_PARAM
        haat_inputs_ready = (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and isinstance(height, (int, float))
            and height_type in ("AGL", "AMSL")
        )
        if haat_inputs_ready and _cat_a_outdoor_haat_exceeds_limit(installation):
            return INVALID_PARAM

    # Cat B must not claim indoorDeployment True in many WINNF scenarios
    if category == "B" and installation.get("indoorDeployment") is True:
        return INVALID_PARAM

    cpi_sig = request.get("cpiSignatureData")
    if cpi_sig:
        # Cat B: installationParam must not also appear in cleartext with CPI sig
        if request.get("installationParam") is not None and category == "B":
            return INVALID_PARAM
        # Also invalid if both present regardless (REG_7 device_11)
        if request.get("installationParam") is not None:
            return INVALID_PARAM
        # Cryptographic CPI checks run in process_registration via _verify_request_cpi.

    # Cat B without CPI signature: if installation params provided in clear → invalid
    if category == "B" and not cpi_sig:
        if request.get("installationParam") is not None:
            return INVALID_PARAM
        # Only conditionals without CPI for Cat B is also invalid when full install present
        # via conditionals alone without professional installer (REG_11 device_d / REG_7 d14)
        cond = _get_conditionals(db, fcc_id, serial)
        if cond.get("installationParam") and not request.get("cpiSignatureData"):
            # Multi-step Cat B via conditionals alone is allowed in REG_1/REG_2 paths
            # when conditionals include category/airInterface/measCapability.
            # REG_1 preloads all conditionals and strips them from request → success.
            # REG_7 device_14: conditionals only have installationParam (+fcc/serial) and
            # request still has full cleartext fields including installationParam → already
            # caught above. If request stripped install but category from request is B:
            pass

    return None


def _make_cbsd_id(fcc_id: str, serial: str) -> str:
    return f"{fcc_id}/{serial}"


def _terminate_grants(db: Session, cbsd_id: str) -> None:
    from services.concurrency import lock_grants_for_cbsd
    from services.lifecycle import GrantEvent, apply_grant_event

    for grant in lock_grants_for_cbsd(db, cbsd_id):
        if grant.terminated:
            continue
        apply_grant_event(
            grant,
            GrantEvent.TERMINATE,
            payload={"cbsdId": cbsd_id, "grantId": grant.grant_id},
        )


def process_registration(
    db: Session,
    registration_requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
    device_adapter: ConsumerAdapter | None = None,
) -> list[dict[str, Any]]:
    from runtime.consumer_access import require_composed_device_adapter

    adapter = device_adapter or require_composed_device_adapter()
    responses: list[dict[str, Any]] = []

    for raw in registration_requests:
        request = dict(raw)

        if _missing_required_fields(request):
            responses.append({"response": {"responseCode": MISSING_PARAM}})
            continue

        fcc_id = request["fccId"]
        serial = request["cbsdSerialNumber"]

        if is_cbsd_blacklisted(db, fcc_id, serial):
            responses.append({"response": {"responseCode": BLACKLISTED}})
            continue

        cpi_error, verified_cpi = _verify_request_cpi(request, db)
        if cpi_error is not None:
            responses.append({"response": {"responseCode": cpi_error}})
            continue

        conditionals = _get_conditionals(db, fcc_id, serial)
        merged = _merge_registration(
            request, conditionals, verified_cpi_payload=verified_cpi
        )

        # Category B registering with clear installationParam + no CPI is invalid
        # (checked in _validate_params). Pending checked before invalid where appropriate.

        invalid = _validate_params(request, merged, db)
        if invalid is not None:
            responses.append({"response": {"responseCode": invalid}})
            continue

        if _has_pending_params(merged):
            responses.append({"response": {"responseCode": PENDING}})
            continue

        # QPR: NRQZ / FCC offices (Cat A 2.4 / Cat B 4.8) / Table Mountain / config areas.
        from services.quiet_zone_service import registration_blocked_by_quiet_zone

        try:
            consumer = consumer_view_from_payload(merged, adapter)
            lat, lon = geo_coordinates(consumer)
            cbsd_id = consumer.holder_id
        except ValueError:
            responses.append({"response": {"responseCode": INVALID_PARAM}})
            continue
        if registration_blocked_by_quiet_zone(
            {"latitude": lat, "longitude": lon},
            cbsd_category=merged.get("cbsdCategory"),
            db=db,
        ):
            responses.append({"response": {"responseCode": INVALID_PARAM}})
            continue
        from sqlalchemy.exc import IntegrityError

        from services.concurrency import (
            acquire_cbsd_xact_lock,
            exclusive_cbsd,
            lock_cbsd_row,
        )

        with exclusive_cbsd(cbsd_id):
            try:
                acquire_cbsd_xact_lock(db, cbsd_id)
                existing = lock_cbsd_row(db, cbsd_id)
                if existing:
                    from services.cbsd_auth import cbsd_certificate_mismatch
                    from services.lifecycle import (
                        CbsdEvent,
                        CbsdState,
                        apply_cbsd_state,
                        evaluate_cbsd_transition,
                        resolve_cbsd_state,
                    )

                    # Prevent certificate takeover of an already-bound cbsdId.
                    if cbsd_certificate_mismatch(existing, certificate_hash):
                        responses.append({"response": {"responseCode": INVALID_PARAM}})
                        continue
                    outcome = evaluate_cbsd_transition(
                        CbsdEvent.REREGISTER,
                        current=resolve_cbsd_state(existing),
                        payload=request,
                    )
                    if not outcome.ok:
                        responses.append(
                            {"response": {"responseCode": outcome.response_code}}
                        )
                        continue
                    existing.user_id = request["userId"]
                    existing.cbsd_category = merged.get("cbsdCategory")
                    existing.registration_json = json.dumps(merged)
                    if certificate_hash is not None:
                        existing.certificate_hash = certificate_hash
                    apply_cbsd_state(existing, CbsdState.REGISTERED)
                    _terminate_grants(db, cbsd_id)
                else:
                    from services.lifecycle import (
                        CbsdEvent,
                        CbsdState,
                        evaluate_cbsd_transition,
                    )

                    outcome = evaluate_cbsd_transition(
                        CbsdEvent.REGISTER,
                        current=CbsdState.UNREGISTERED,
                        payload=request,
                    )
                    if not outcome.ok:
                        responses.append(
                            {"response": {"responseCode": outcome.response_code}}
                        )
                        continue
                    db.add(
                        Cbsd(
                            cbsd_id=cbsd_id,
                            fcc_id=fcc_id,
                            user_id=request["userId"],
                            cbsd_serial_number=serial,
                            cbsd_category=merged.get("cbsdCategory"),
                            certificate_hash=certificate_hash,
                            lifecycle_state=CbsdState.REGISTERED.value,
                            registration_json=json.dumps(merged),
                        )
                    )
                    try:
                        # Surface unique races (multi-worker create) as protocol 103.
                        db.flush()
                    except IntegrityError:
                        db.rollback()
                        responses.append(
                            {"response": {"responseCode": INVALID_PARAM}}
                        )
                        continue

                responses.append(
                    {
                        "cbsdId": cbsd_id,
                        "response": {"responseCode": SUCCESS},
                    }
                )
            finally:
                db.commit()

    # MES_1: when triggered, ask for WITHOUT_GRANT measurement reports.
    from services.meas_report import (
        FLAG_MEAS_REG,
        MEAS_WITHOUT_GRANT,
        admin_flag_set,
    )

    if admin_flag_set(db, FLAG_MEAS_REG):
        for raw, resp in zip(registration_requests, responses):
            if resp.get("response", {}).get("responseCode") != SUCCESS:
                continue
            meas = raw.get("measCapability") or []
            if MEAS_WITHOUT_GRANT in meas:
                resp["measReportConfig"] = [MEAS_WITHOUT_GRANT]

    db.commit()
    return responses
