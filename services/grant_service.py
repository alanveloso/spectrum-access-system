"""Grant business logic aligned with WINNF_FT_S_GRA expectations (MVP simulation)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from primitives.admission import power_exceeds
from primitives.frequency import FrequencyRange
from primitives.power import PowerDbm
from primitives.refresh import PeriodicRefresh
from adapters.device import ConsumerAdapter
from models.models import Cbsd, FccIdRecord, Grant
from services.blacklist_service import is_cbsd_blacklisted
from services.consumer_semantics import cbsd_geo_coordinates
from services.dpa_neighborhood import (
    compute_transmit_expire_time,
    fmt_transmit_expire,
)
from services.geometry import point_in_geojson
from services.spectrum_inquiry_service import (
    CBRS_HIGH_HZ,
    CBRS_LOW_HZ,
    _load_injected,
    _overlaps,
    _pal_freq,
)

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300
INTERFERENCE = 400
GRANT_CONFLICT = 401

DEFAULT_GRANT_REFRESH = PeriodicRefresh(interval_seconds=60)
HEARTBEAT_INTERVAL_SEC = DEFAULT_GRANT_REFRESH.interval_seconds
# Long enough for HBT.12 (sleep 240s + optional 300s) yet < 24h for HBT.6.
DEFAULT_GRANT_DURATION_SEC = 900
CAT_A_MAX_EIRP_10MHZ = 30.0
CAT_B_MAX_EIRP_10MHZ = 47.0


def _resp(code: int, *, cbsd_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"response": {"responseCode": code}}
    if cbsd_id is not None:
        out["cbsdId"] = cbsd_id
    return out


def _cbsd_reg(cbsd: Cbsd) -> dict[str, Any]:
    try:
        payload = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cbsd_location(cbsd: Cbsd, adapter: ConsumerAdapter) -> tuple[float | None, float | None]:
    try:
        lat, lon = cbsd_geo_coordinates(cbsd, adapter)
    except ValueError:
        return None, None
    return lat, lon


def _max_allowed_eirp_mhz(cbsd: Cbsd, fcc_max_eirp: float) -> float:
    """WINNF: maxEirp is dBm/MHz; category limits are dBm/10 MHz."""
    reg = _cbsd_reg(cbsd)
    cat = (reg.get("cbsdCategory") or cbsd.cbsd_category or "A").upper()
    default = CAT_A_MAX_EIRP_10MHZ if cat == "A" else CAT_B_MAX_EIRP_10MHZ
    caps = [default, float(fcc_max_eirp)]
    eirp_cap = (reg.get("installationParam") or {}).get("eirpCapability")
    if eirp_cap is not None:
        caps.append(float(eirp_cap))
    return min(caps) - 10.0


def _parse_freq(
    op: dict[str, Any] | None,
) -> tuple[int | None, int | None, int | None]:
    """Return (error_code, low, high). error_code set on failure."""
    if op is None:
        return MISSING_PARAM, None, None
    freq = op.get("operationFrequencyRange")
    if not isinstance(freq, dict):
        return MISSING_PARAM, None, None
    if "lowFrequency" not in freq or "highFrequency" not in freq:
        return MISSING_PARAM, None, None
    low, high = freq.get("lowFrequency"), freq.get("highFrequency")
    if low is None or high is None:
        return MISSING_PARAM, None, None
    try:
        low_i, high_i = int(low), int(high)
    except (TypeError, ValueError):
        return INVALID_PARAM, None, None
    if high_i <= low_i:
        return INVALID_PARAM, None, None
    try:
        asked = FrequencyRange(low_hz=low_i, high_hz=high_i)
        band = FrequencyRange(low_hz=CBRS_LOW_HZ, high_hz=CBRS_HIGH_HZ)
    except ValueError:
        return INVALID_PARAM, None, None
    if not band.contains(asked):
        return UNSUPPORTED_SPECTRUM, None, None
    return None, low_i, high_i


def _active_grants(db: Session, cbsd_id: str) -> list[Grant]:
    return (
        db.query(Grant)
        .filter_by(cbsd_id=cbsd_id, terminated=False)
        .all()
    )


def _has_freq_conflict(
    existing: list[Grant],
    low: int,
    high: int,
    *,
    also_pending: list[tuple[int, int]] | None = None,
) -> bool:
    for g in existing:
        if _overlaps(g.low_frequency, g.high_frequency, low, high):
            return True
    for plow, phigh in also_pending or []:
        if _overlaps(plow, phigh, low, high):
            return True
    return False


def _ppa_pal_context(
    db: Session, cbsd: Cbsd, adapter: ConsumerAdapter
) -> list[dict[str, Any]]:
    """Return list of {low, high, in_cluster, in_ppa, license_exp} for linked PPAs."""
    lat, lon = _cbsd_location(cbsd, adapter)
    pals = _load_injected(db, "pal")
    zones = _load_injected(db, "zone")
    pal_by_id = {p.get("palId"): p for p in pals if p.get("palId")}
    contexts: list[dict[str, Any]] = []

    for zone_payload in zones:
        record = zone_payload.get("record") or zone_payload
        if record.get("usage") != "PPA" and "ppaInfo" not in record:
            continue
        ppa_info = record.get("ppaInfo") or {}
        cluster = set(ppa_info.get("cbsdReferenceId") or [])
        in_cluster = cbsd.cbsd_id in cluster
        in_ppa = False
        if lat is not None and lon is not None:
            in_ppa = point_in_geojson(lat, lon, record.get("zone"))

        for pal_id in ppa_info.get("palId") or []:
            pal = pal_by_id.get(pal_id)
            if not pal:
                continue
            pf = _pal_freq(pal)
            if not pf:
                continue
            license_exp = (pal.get("license") or {}).get("licenseExpiration")
            contexts.append(
                {
                    "low": pf[0],
                    "high": pf[1],
                    "in_cluster": in_cluster,
                    "in_ppa": in_ppa,
                    "license_exp": license_exp,
                    "user_id": pal.get("userId"),
                    "pal_id": pal_id,
                }
            )
    return contexts


def _resolve_channel(
    contexts: list[dict[str, Any]], low: int, high: int, *, cbsd_user_id: str
) -> tuple[int | None, str | None, datetime | None, str | None]:
    """
    Return (error_code, channel_type, pal_license_exp, pal_id).
    error_code None means OK.
    """
    covering_pal: list[dict[str, Any]] = []
    overlapping_pal: list[dict[str, Any]] = []
    for ctx in contexts:
        if _overlaps(low, high, ctx["low"], ctx["high"]):
            overlapping_pal.append(ctx)
            if low >= ctx["low"] and high <= ctx["high"]:
                covering_pal.append(ctx)

    # Inside claimed PPA but not in cluster → interference on PAL overlap.
    for ctx in overlapping_pal:
        if ctx["in_ppa"] and not ctx["in_cluster"]:
            return INTERFERENCE, None, None, None

    # Mix of PAL + GAA: request overlaps PAL for cluster member but not fully inside.
    for ctx in overlapping_pal:
        if ctx["in_cluster"] and not (low >= ctx["low"] and high <= ctx["high"]):
            return INVALID_PARAM, None, None, None

    if covering_pal:
        for ctx in covering_pal:
            if ctx["in_cluster"] and ctx.get("user_id") == cbsd_user_id:
                exp = None
                if ctx.get("license_exp"):
                    try:
                        exp = datetime.strptime(
                            ctx["license_exp"], "%Y-%m-%dT%H:%M:%SZ"
                        )
                    except (TypeError, ValueError):
                        exp = None
                return None, "PAL", exp, ctx.get("pal_id")
        # Fully inside a PAL channel but not authorized → interference.
        return INTERFERENCE, None, None, None

    return None, "GAA", None, None


def _grant_expire_time(pal_license_exp: datetime | None) -> datetime:
    from services.clock import ensure_utc, utc_now

    default = utc_now() + timedelta(seconds=DEFAULT_GRANT_DURATION_SEC)
    if pal_license_exp is None:
        return default
    # Must be ≤ PAL licenseExpiration (GRA.13).
    return min(default, ensure_utc(pal_license_exp))


def process_grant(
    db: Session,
    requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
    rf_port: object | None = None,
    device_adapter: ConsumerAdapter | None = None,
) -> list[dict[str, Any]]:
    from runtime.consumer_access import require_composed_device_adapter
    from services.cbsd_auth import cbsd_certificate_mismatch
    from services.meas_report import (
        FLAG_MEAS_REG,
        MEAS_WITHOUT_GRANT,
        admin_flag_set,
        cbsd_meas_capabilities,
        validate_meas_report,
    )

    ask_meas = admin_flag_set(db, FLAG_MEAS_REG)
    adapter = device_adapter or require_composed_device_adapter()
    responses: list[dict[str, Any]] = []
    # Frequencies approved earlier in this same batch (conflict within batch).
    pending_by_cbsd: dict[str, list[tuple[int, int]]] = {}

    for req in requests:
        cbsd_id = req.get("cbsdId")
        if not cbsd_id:
            responses.append(_resp(MISSING_PARAM))
            continue

        cbsd = db.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
        if not cbsd:
            # Unknown CBSD → 103 without echoing cbsdId (GRA.3).
            responses.append(_resp(INVALID_PARAM))
            continue

        # Wrong client cert for this cbsdId → 103 without cbsdId/grantId (GRA.4).
        if cbsd_certificate_mismatch(cbsd, certificate_hash):
            responses.append(_resp(INVALID_PARAM))
            continue

        if is_cbsd_blacklisted(db, cbsd.fcc_id, cbsd.cbsd_serial_number):
            responses.append(_resp(BLACKLISTED, cbsd_id=cbsd_id))
            continue

        caps = cbsd_meas_capabilities(cbsd.registration_json)
        if ask_meas and MEAS_WITHOUT_GRANT in caps:
            meas_err = validate_meas_report(
                req.get("measReport"), require_full_cbrs=True
            )
            if meas_err is not None:
                responses.append(_resp(meas_err, cbsd_id=cbsd_id))
                continue

        op = req.get("operationParam")
        if not isinstance(op, dict):
            responses.append(_resp(MISSING_PARAM, cbsd_id=cbsd_id))
            continue
        if "maxEirp" not in op or op.get("maxEirp") is None:
            responses.append(_resp(MISSING_PARAM, cbsd_id=cbsd_id))
            continue

        freq_err, low, high = _parse_freq(op)
        if freq_err is not None:
            responses.append(_resp(freq_err, cbsd_id=cbsd_id))
            continue
        assert low is not None and high is not None

        try:
            max_eirp = float(op["maxEirp"])
        except (TypeError, ValueError):
            responses.append(_resp(INVALID_PARAM, cbsd_id=cbsd_id))
            continue

        fcc = db.query(FccIdRecord).filter_by(fcc_id=cbsd.fcc_id).first()
        fcc_max = float(fcc.fcc_max_eirp) if fcc else CAT_B_MAX_EIRP_10MHZ
        if power_exceeds(PowerDbm(max_eirp), PowerDbm(_max_allowed_eirp_mhz(cbsd, fcc_max))):
            responses.append(_resp(INVALID_PARAM, cbsd_id=cbsd_id))
            continue

        contexts = _ppa_pal_context(db, cbsd, adapter)
        ch_err, channel_type, pal_exp, pal_id = _resolve_channel(
            contexts, low, high, cbsd_user_id=cbsd.user_id
        )
        if ch_err is not None:
            responses.append(_resp(ch_err, cbsd_id=cbsd_id))
            continue
        assert channel_type is not None

        # EXZ: CBSD inside / within 50 m of an exclusion zone with overlapping freq → 400.
        lat, lon = _cbsd_location(cbsd, adapter)
        if lat is not None and lon is not None:
            from services.exclusion_zone_service import (
                ExclusionZoneError,
                ExclusionZoneUnavailable,
                point_hits_exclusion_zone,
            )

            try:
                if point_hits_exclusion_zone(db, lat, lon, low, high):
                    responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                    continue
            except (ExclusionZoneError, ExclusionZoneUnavailable):
                # Fail-closed: required EXZ data missing/invalid → deny grant.
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

            # GWPZ: CBSD inside injected WISP zone + overlapping freq → 400.
            from services.gwpz_protection import (
                GwpzProtectionError,
                grant_blocked_by_gwpz,
            )

            try:
                if grant_blocked_by_gwpz(db, lat, lon, low, high):
                    responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                    continue
            except GwpzProtectionError:
                # Fail-closed: malformed GWPZ prevents required determination.
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

            # QPR: quiet-zone / FCC / Table Mountain / configurable protected areas.
            from services.quiet_zone_service import grant_blocked_by_quiet_zone

            if grant_blocked_by_quiet_zone(
                lat,
                lon,
                cbsd_category=cbsd.cbsd_category or (_cbsd_reg(cbsd).get("cbsdCategory")),
                low_hz=low,
                high_hz=high,
                db=db,
            ):
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

            # FDB.6: FSS with neighboring GWBL within 150 km → 400 on 3650–3700 MHz.
            from services.federal_db_service import grant_blocked_by_fss_gwbl

            if grant_blocked_by_fss_gwbl(db, lat, lon, low, high):
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

            # IPR.3/4: Rel1Ext DPA neighborhood aggregate violation → 400.
            from services.dpa_protection import proposed_grant_violates_dpa

            if proposed_grant_violates_dpa(
                db, cbsd, low_hz=low, high_hz=high, max_eirp_dbm_mhz=max_eirp
            ):
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

        # BPR: Canadian Border Sharing Zone PFD > -80 dBm/m²/MHz → 400.
        reg = _cbsd_reg(cbsd)
        installation = reg.get("installationParam") or {}
        if installation:
            from services.border_protection import violates_canadian_border_pfd

            if violates_canadian_border_pfd(
                installation, max_eirp, low, high, rf_port=rf_port
            ):
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

        # Serialize IAP admission then per-CBSD create (fixed lock order).
        from services.concurrency import (
            acquire_cbsd_xact_lock,
            acquire_iap_admission_xact_lock,
            exclusive_cbsd,
            exclusive_iap_admission,
            lock_cbsd_row,
        )
        from services.cpas_service import peer_has_grant_for_cbsd
        from services.federal_db_service import grant_sync_stamp
        from services.grant_renewal import AUTH_CONTEXT_KEY, build_auth_context
        from services.iap.admission import proposed_grant_violates_iap
        from services.lifecycle import GrantState

        # IAP admission lock first, then CBSD — avoid deadlock with concurrent grants.
        with exclusive_iap_admission():
            with exclusive_cbsd(cbsd_id):
                try:
                    acquire_iap_admission_xact_lock(db)
                    acquire_cbsd_xact_lock(db, cbsd_id)
                    cbsd = lock_cbsd_row(db, cbsd_id)
                    if not cbsd:
                        responses.append(_resp(INVALID_PARAM))
                        continue
                    if cbsd_certificate_mismatch(cbsd, certificate_hash):
                        responses.append(_resp(INVALID_PARAM))
                        continue

                    existing = _active_grants(db, cbsd_id)
                    pending = pending_by_cbsd.get(cbsd_id, [])
                    if _has_freq_conflict(existing, low, high, also_pending=pending):
                        responses.append(_resp(GRANT_CONFLICT, cbsd_id=cbsd_id))
                        continue

                    # GRA_5: CBSD already has an active grant on a peer SAS (same cbsdReferenceId).
                    if peer_has_grant_for_cbsd(db, cbsd):
                        responses.append(_resp(GRANT_CONFLICT, cbsd_id=cbsd_id))
                        continue

                    # post-CPAS IAP/ESC admission at requested maxEirp.
                    if proposed_grant_violates_iap(
                        db,
                        cbsd,
                        low_hz=int(low),
                        high_hz=int(high),
                        max_eirp_dbm_mhz=float(max_eirp),
                        rf_port=rf_port,
                    ):
                        responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                        continue

                    grant_id = f"grant/{uuid.uuid4().hex}"
                    expire = _grant_expire_time(pal_exp)
                    tx_expire = compute_transmit_expire_time(
                        db,
                        cbsd,
                        expire,
                        low_hz=int(low),
                        high_hz=int(high),
                    )

                    stamp = grant_sync_stamp(db)
                    grant_payload = dict(req) if isinstance(req, dict) else {}
                    grant_payload["fss_gen"] = stamp.get("fss", 0)
                    grant_payload["gwbl_gen"] = stamp.get("gwbl", 0)
                    grant_payload["exz_gen"] = stamp.get("exz", 0)
                    grant_payload["dpa_gen"] = stamp.get("dpa", 0)

                    grant_payload[AUTH_CONTEXT_KEY] = build_auth_context(
                        channel_type=channel_type or "GAA",
                        pal_license_exp=pal_exp,
                        pal_id=str(pal_id) if pal_id else None,
                    )
                    db.add(
                        Grant(
                            grant_id=grant_id,
                            cbsd_pk=cbsd.id,
                            cbsd_id=cbsd_id,
                            channel_type=channel_type,
                            low_frequency=low,
                            high_frequency=high,
                            max_eirp=max_eirp,
                            grant_expire_time=expire,
                            transmit_expire_time=tx_expire,
                            heartbeat_interval=HEARTBEAT_INTERVAL_SEC,
                            lifecycle_state=GrantState.GRANTED.value,
                            grant_json=json.dumps(grant_payload),
                        )
                    )
                    pending_by_cbsd.setdefault(cbsd_id, []).append((low, high))
                    responses.append(
                        {
                            "cbsdId": cbsd_id,
                            "grantId": grant_id,
                            "grantExpireTime": expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "transmitExpireTime": fmt_transmit_expire(tx_expire),
                            "heartbeatInterval": HEARTBEAT_INTERVAL_SEC,
                            "channelType": channel_type,
                            "response": {"responseCode": SUCCESS},
                        }
                    )
                finally:
                    db.commit()

    return responses
