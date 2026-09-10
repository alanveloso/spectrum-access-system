"""Heartbeat business logic aligned with WINNF_FT_S_HBT / MES expectations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from models.models import Grant
from services.blacklist_service import is_cbsd_blacklisted
from services.dpa_neighborhood import compute_transmit_expire_time
from services.grant_service import DEFAULT_GRANT_REFRESH
from services.meas_report import (
    FLAG_MEAS_HBT,
    MEAS_WITH_GRANT,
    admin_flag_set,
    cbsd_meas_capabilities,
    validate_meas_report,
)
from services.spectrum_inquiry_service import (
    _load_injected,
    _overlaps,
    _wisp_freq,
)

SUCCESS = 0
VERSION_UNSUPPORTED = 100
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
TERMINATED_GRANT = 500
SUSPENDED_GRANT = 501
UNSYNC_OP_PARAM = 502


def _fmt(dt: datetime) -> str:
    from services.clock import ensure_utc

    return ensure_utc(dt).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_tx() -> datetime:
    from services.clock import utc_now

    return utc_now().replace(microsecond=0) - timedelta(seconds=1)


def _persist_federal_termination(
    grant: Grant,
    *,
    cbsd_id: str,
    grant_id: str,
) -> None:
    """Persist TERMINATED for a federal 500 heartbeat decision.

 Wall-clock expiry makes ``resolve_grant_state`` report EXPIRED while the
 column may still be GRANTED; TERMINATE is blocked from terminal states.
 Force TERMINATED so the federal termination decision is durable.
 """
    from services.lifecycle import (
        GrantEvent,
        GrantState,
        apply_grant_event,
        apply_grant_state,
    )

    outcome = apply_grant_event(
        grant,
        GrantEvent.TERMINATE,
        payload={"cbsdId": cbsd_id, "grantId": grant_id},
    )
    if not outcome.ok:
        apply_grant_state(grant, GrantState.TERMINATED)


def _future_tx(
    db: Session,
    cbsd,
    grant: Grant,
    *,
    now: datetime | None = None,
) -> datetime:
    return compute_transmit_expire_time(
        db,
        cbsd,
        grant.grant_expire_time,
        low_hz=int(grant.low_frequency),
        high_hz=int(grant.high_frequency),
        now=now,
    )


def _base(
    code: int,
    *,
    cbsd_id: str | None = None,
    grant_id: str | None = None,
    tx: datetime | None = None,
    grant_expire: datetime | None = None,
    heartbeat_interval: int | None = None,
    meas_config: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "transmitExpireTime": _fmt(tx if tx is not None else _past_tx()),
        "response": {"responseCode": code},
    }
    if cbsd_id is not None:
        out["cbsdId"] = cbsd_id
    if grant_id is not None:
        out["grantId"] = grant_id
    if grant_expire is not None:
        out["grantExpireTime"] = _fmt(grant_expire)
    if heartbeat_interval is not None:
        out["heartbeatInterval"] = heartbeat_interval
    if meas_config is not None:
        out["measReportConfig"] = meas_config
    return out


def _grant_overlaps_wisp(db: Session, grant: Grant) -> bool:
    """True when grant frequency overlaps an injected WISP (post-CPAS simulation)."""
    for wisp in _load_injected(db, "wisp"):
        freq = _wisp_freq(wisp)
        if freq and _overlaps(grant.low_frequency, grant.high_frequency, freq[0], freq[1]):
            return True
    return False


def _grant_overlaps_active_dpa(db: Session, grant: Grant) -> bool:
    from services.dpa_protection import (
        ProtectionReason,
        grant_frequency_overlaps_protected,
        grant_on_any_movelist,
        list_protected_dpa_channels,
    )
    from services.dpa_service import (
        grant_overlaps_active_dpa,
        grant_overlaps_esc_monitored_catalogue,
    )
    from services.esc_admin_service import is_esc_absent, is_esc_disconnected

    if grant_on_any_movelist(db, grant.grant_id):
        return True

    low, high = grant.low_frequency, grant.high_frequency
    if grant_overlaps_active_dpa(db, low, high):
        return True
    # Lost ESC-DE or no ESC present: protect all ESC-monitored catalogue channels.
    if (is_esc_disconnected(db) or is_esc_absent(db)) and grant_overlaps_esc_monitored_catalogue(
        db, low, high
    ):
        return True
    # Always-on / inland DPAs (escMonitored=false) require continuous protection.
    protected = grant_frequency_overlaps_protected(
        list_protected_dpa_channels(db), low, high
    )
    return any(p.reason == ProtectionReason.ALWAYS_ON for p in protected)


def process_heartbeat(
    db: Session,
    requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
) -> list[dict[str, Any]]:
    from services.cbsd_auth import cbsd_certificate_mismatch
    from services.lifecycle import (
        GrantEvent,
        apply_grant_event,
        heartbeat_operation_allowed,
    )

    ask_meas = admin_flag_set(db, FLAG_MEAS_HBT)
    responses: list[dict[str, Any]] = []

    for req in requests:
        cbsd_id = req.get("cbsdId")
        grant_id = req.get("grantId")
        op_state = req.get("operationState")

        # Missing required fields → 102 with past transmitExpireTime.
        if not cbsd_id or not grant_id or not op_state:
            echo_grant = None
            if grant_id and cbsd_id:
                # Echo grantId when both ids present but operationState missing (HBT.4).
                if db.query(Grant).filter_by(grant_id=grant_id, cbsd_id=cbsd_id).first():
                    echo_grant = grant_id
            responses.append(
                _base(MISSING_PARAM, cbsd_id=cbsd_id, grant_id=echo_grant)
            )
            continue

        from services.concurrency import (
            acquire_cbsd_xact_lock,
            acquire_grant_xact_lock,
            exclusive_cbsd_and_grant,
            lock_cbsd_row,
            lock_grant_row,
        )

        with exclusive_cbsd_and_grant(cbsd_id, grant_id):
            try:
                acquire_cbsd_xact_lock(db, cbsd_id)
                acquire_grant_xact_lock(db, grant_id)
                cbsd = lock_cbsd_row(db, cbsd_id)
                # Wrong client cert → 103 without echoing ids (before grant existence probe).
                if cbsd and cbsd_certificate_mismatch(cbsd, certificate_hash):
                    responses.append(_base(INVALID_PARAM))
                    continue

                grant = lock_grant_row(db, grant_id, cbsd_id)
                if not grant:
                    # Invalid grantId → 103 without echoing grantId (HBT.7).
                    responses.append(_base(INVALID_PARAM, cbsd_id=cbsd_id))
                    continue

                if cbsd is None:
                    # Orphan grant row without a registration cannot be heartbeated.
                    responses.append(_base(INVALID_PARAM, cbsd_id=cbsd_id))
                    continue

                if is_cbsd_blacklisted(
                    db, cbsd.fcc_id, cbsd.cbsd_serial_number
                ):
                    responses.append(
                        _base(BLACKLISTED, cbsd_id=cbsd_id, grant_id=grant_id)
                    )
                    continue

                # GRA_6: peer FAD reports an active grant for the same CBSD → terminate (500).
                from services.cpas_service import peer_has_grant_for_cbsd

                if peer_has_grant_for_cbsd(db, cbsd):
                    apply_grant_event(
                        grant,
                        GrantEvent.TERMINATE,
                        payload={"cbsdId": cbsd_id, "grantId": grant_id},
                    )
                    responses.append(
                        _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
                    )
                    continue

                life = heartbeat_operation_allowed(grant, operation_state=str(op_state))
                if not life.ok:
                    if life.response_code == INVALID_PARAM:
                        # Relinquished / unresolvable terminal: do not echo grantId.
                        responses.append(_base(INVALID_PARAM, cbsd_id=cbsd_id))
                    elif life.response_code == TERMINATED_GRANT:
                        # Existing TERMINATED grant: 500 with original grantId (HBT_9 / FDB_1).
                        # Do not authorize, renew, or mutate persisted termination.
                        responses.append(
                            _base(
                                TERMINATED_GRANT,
                                cbsd_id=cbsd_id,
                                grant_id=grant_id,
                            )
                        )
                    elif life.response_code == SUSPENDED_GRANT:
                        responses.append(
                            _base(
                                SUSPENDED_GRANT,
                                cbsd_id=cbsd_id,
                                grant_id=grant_id,
                                grant_expire=grant.grant_expire_time,
                                heartbeat_interval=DEFAULT_GRANT_REFRESH.advertised(
                                    grant.heartbeat_interval
                                ),
                            )
                        )
                    else:
                        responses.append(
                            _base(
                                life.response_code,
                                cbsd_id=cbsd_id,
                                grant_id=grant_id,
                            )
                        )
                    continue

                # DPA activation → suspend grant (501), do not terminate (GRA.1).
                # Transient suspension: response only; clear when DPA inactive (no persist).
                # HBT.12 also accepts 501; transmitExpireTime stays in the past via _base.
                if _grant_overlaps_active_dpa(db, grant):
                    responses.append(
                        _base(
                            SUSPENDED_GRANT,
                            cbsd_id=cbsd_id,
                            grant_id=grant_id,
                            grant_expire=grant.grant_expire_time,
                            heartbeat_interval=DEFAULT_GRANT_REFRESH.advertised(
                                grant.heartbeat_interval
                            ),
                        )
                    )
                    continue

                # evaluate federal once. Termination-grade 500 preempts generic
                # expiration so overnight scheduled-federal heartbeats can still observe
                # TERMINATED_GRANT (HBT_6 also accepts 500). Federal 501 must not override
                # expiration — an already-expired grant stays 103.
                from services.federal_db_service import heartbeat_federal_code

                federal_code = heartbeat_federal_code(db, cbsd, grant)
                if federal_code == TERMINATED_GRANT:
                    _persist_federal_termination(
                        grant, cbsd_id=cbsd_id, grant_id=grant_id
                    )
                    responses.append(
                        _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
                    )
                    continue

                from services.lifecycle import _grant_expired

                if _grant_expired(grant):
                    apply_grant_event(
                        grant,
                        GrantEvent.EXPIRE,
                        payload={"cbsdId": cbsd_id, "grantId": grant_id},
                    )
                    responses.append(
                        _base(INVALID_PARAM, cbsd_id=cbsd_id, grant_id=grant_id)
                    )
                    continue

                if _grant_overlaps_wisp(db, grant):
                    apply_grant_event(
                        grant,
                        GrantEvent.TERMINATE,
                        payload={"cbsdId": cbsd_id, "grantId": grant_id},
                    )
                    responses.append(
                        _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
                    )
                    continue

                if federal_code == SUSPENDED_GRANT:
                    # Transient federal suspend — do not persist SUSPENDED.
                    responses.append(
                        _base(
                            SUSPENDED_GRANT,
                            cbsd_id=cbsd_id,
                            grant_id=grant_id,
                            grant_expire=grant.grant_expire_time,
                            heartbeat_interval=DEFAULT_GRANT_REFRESH.advertised(
                                grant.heartbeat_interval
                            ),
                        )
                    )
                    continue

                capabilities = cbsd_meas_capabilities(
                    cbsd.registration_json if cbsd else None
                )

                # After SAS asked for WITH_GRANT reports, validate subsequent heartbeats.
                if grant.meas_report_requested and MEAS_WITH_GRANT in capabilities:
                    meas_err = validate_meas_report(
                        req.get("measReport"), require_full_cbrs=False
                    )
                    if meas_err is not None:
                        responses.append(
                            _base(meas_err, cbsd_id=cbsd_id, grant_id=grant_id)
                        )
                        continue

                if req.get("grantRenew") is True:
                    from services.grant_renewal import apply_renewal

                    renew = apply_renewal(db, grant)
                    if not renew.ok:
                        responses.append(
                            _base(
                                renew.response_code,
                                cbsd_id=cbsd_id,
                                grant_id=grant_id,
                            )
                        )
                        continue

                tx = _future_tx(db, cbsd, grant)
                grant.transmit_expire_time = tx
                apply_grant_event(
                    grant,
                    GrantEvent.AUTHORIZE,
                    payload={
                        "cbsdId": cbsd_id,
                        "grantId": grant_id,
                        "operationState": op_state,
                    },
                )

                meas_config: list[str] | None = None
                if ask_meas and MEAS_WITH_GRANT in capabilities:
                    meas_config = [MEAS_WITH_GRANT]
                    grant.meas_report_requested = True

                responses.append(
                    _base(
                        SUCCESS,
                        cbsd_id=cbsd_id,
                        grant_id=grant_id,
                        tx=tx,
                        grant_expire=grant.grant_expire_time
                        if req.get("grantRenew") is True
                        else None,
                        heartbeat_interval=DEFAULT_GRANT_REFRESH.advertised(
                            grant.heartbeat_interval
                        ),
                        meas_config=meas_config,
                    )
                )
            finally:
                # Persist mutations before releasing locks for peer sessions.
                db.commit()

    return responses
