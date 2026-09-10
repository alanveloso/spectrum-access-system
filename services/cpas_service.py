"""CPAS / peer FAD sync — UUT acts as SAS↔SAS client during daily activities.

 transactional pipeline:

1. sync external databases;
2. obtain/validate peer FADs;
3. freeze active-grant snapshot;
4. evaluate protections (peer CBSD / PPA / ESC);
5. apply grant decisions + publish FAD in one durable critical section;
6. update schedule status and append audit log.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData, Cbsd, Grant, PeerFadRecord
from services.clock import utc_now
from services.concurrency import (
    acquire_cpas_pipeline_xact_lock,
    acquire_iap_admission_xact_lock,
    exclusive_iap_admission,
)
from services.fad_client_service import run_peer_fad_sync
from services.fad_service import create_full_activity_dump, fad_cbsd_id
from services.meas_report import clear_admin_flags, set_admin_flag
from profiles import DistanceExclusionBinding, get_active_profile_document

logger = logging.getLogger(__name__)

FLAG_CPAS_RUNNING = "cpas_running"
KIND_CPAS_AUDIT = "cpas_pipeline_audit"
_cpas_dispatch_lock = threading.RLock()
_cpas_pipeline_lock = threading.RLock()  # SQLite / same-process aid


@dataclass(frozen=True)
class FrozenLocalGrantRf:
    """Immutable local grant + installation RF captured at CPAS freeze time.

 Evaluation must use this record as the source of truth for propagation /
 DPA / IAP / peer-geo rules so mid-run registration or EIRP mutations cannot
 mix generation N with N+1.
 """

    grant_pk: int
    grant_id: str
    cbsd_id: str
    fcc_id: str
    cbsd_serial_number: str
    low_hz: int
    high_hz: int
    max_eirp_dbm_mhz: float | None
    lifecycle_state: str
    terminated: bool
    latitude: float | None
    longitude: float | None
    height_m: float | None
    height_type: str
    indoor: bool
    cbsd_category: str
    antenna_azimuth: float | None = None
    antenna_beamwidth: float | None = None
    antenna_gain: float | None = None


@dataclass(frozen=True)
class CpasSnapshot:
    """Frozen view of local grants + peer sync inputs used for decisions."""

    frozen_at: str
    active_grant_pks: tuple[int, ...]
    peer_sync_report: dict[str, Any] = field(default_factory=dict)
    peer_record_count: int = 0
    # Durable peer FAD rows at freeze time:
    # (peer_sas_id, record_type, record_id, data_json).
    # Evaluation must use this set so mid-run peer N→N+1 cannot widen decisions.
    peer_records: tuple[tuple[int, str, str, str], ...] = ()
    # Local RF/install params frozen with membership (generation N).
    local_grants: tuple[FrozenLocalGrantRf, ...] = ()
    # Protection entity payloads at freeze time: (kind, record_id, data_json).
    # IAP ProtectionPoints are built from this set (not live inject mid-run).
    protection_records: tuple[tuple[str, str, str], ...] = ()
    # Peer + injection generation fingerprint at freeze (stamped on success).
    authorization_generation: dict[str, Any] = field(default_factory=dict)


class CpasRfEvaluationError(Exception):
    """Required CPAS RF/DPA evaluation failed; pipeline must abort (no silent skip)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CpasGenerationDriftError(Exception):
    """Peer/protection generation changed after freeze; refuse stale apply/stamp."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_installation_from_registration(
    registration_json: str | None,
    *,
    cbsd_category_fallback: str | None = None,
) -> dict[str, Any]:
    """Extract installation / category fields used by RF evaluation."""
    install: dict[str, Any] = {}
    category = (cbsd_category_fallback or "").upper()
    if registration_json:
        try:
            reg = json.loads(registration_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            reg = {}
        if isinstance(reg, dict):
            raw_install = reg.get("installationParam") or {}
            if isinstance(raw_install, dict):
                install = raw_install
            if reg.get("cbsdCategory"):
                category = str(reg["cbsdCategory"]).upper()
    if category not in {"A", "B"}:
        # Unknown: do not silently force Cat A (ESC 40 km would under-protect).
        # Empty string → ESC IAP uses Cat-B distance (80 km) conservatively.
        category = ""
    return {"install": install, "cbsd_category": category}


def _frozen_local_grant_from_orm(grant: Grant, cbsd: Cbsd) -> FrozenLocalGrantRf:
    parsed = _parse_installation_from_registration(
        cbsd.registration_json, cbsd_category_fallback=cbsd.cbsd_category
    )
    install = parsed["install"]
    lat = lon = height = None
    try:
        lat = float(install["latitude"])
        lon = float(install["longitude"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        height = float(install.get("height"))
    except (TypeError, ValueError):
        height = None
    height_type = str(install.get("heightType") or "AGL").upper()
    if height_type not in {"AGL", "AMSL"}:
        height_type = "AGL"

    def _opt_float(key: str) -> float | None:
        raw = install.get(key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return FrozenLocalGrantRf(
        grant_pk=int(grant.id),
        grant_id=str(grant.grant_id),
        cbsd_id=str(grant.cbsd_id),
        fcc_id=str(cbsd.fcc_id or ""),
        cbsd_serial_number=str(cbsd.cbsd_serial_number or ""),
        low_hz=int(grant.low_frequency),
        high_hz=int(grant.high_frequency),
        max_eirp_dbm_mhz=(
            float(grant.max_eirp) if grant.max_eirp is not None else None
        ),
        lifecycle_state=str(grant.lifecycle_state or ""),
        terminated=bool(grant.terminated),
        latitude=lat,
        longitude=lon,
        height_m=height,
        height_type=height_type,
        indoor=bool(install.get("indoorDeployment", False)),
        cbsd_category=str(parsed["cbsd_category"]),
        antenna_azimuth=_opt_float("antennaAzimuth"),
        antenna_beamwidth=_opt_float("antennaBeamwidth"),
        antenna_gain=_opt_float("antennaGain"),
    )


def frozen_to_dpa_grant_rf(frozen: FrozenLocalGrantRf) -> Any | None:
    """Map a frozen local grant to ``DpaGrantRf`` when coordinates are present."""
    from services.dpa_protection import DpaGrantRf

    if frozen.latitude is None or frozen.longitude is None:
        return None
    height = float(frozen.height_m if frozen.height_m is not None else 0.0)
    return DpaGrantRf(
        grant_id=frozen.grant_id,
        cbsd_id=frozen.cbsd_id,
        latitude=float(frozen.latitude),
        longitude=float(frozen.longitude),
        height_m=height,
        height_is_agl=frozen.height_type != "AMSL",
        indoor=bool(frozen.indoor),
        low_hz=int(frozen.low_hz),
        high_hz=int(frozen.high_hz),
        max_eirp_dbm_mhz=float(
            frozen.max_eirp_dbm_mhz if frozen.max_eirp_dbm_mhz is not None else 0.0
        ),
        is_managing_sas=True,
        cbsd_category=frozen.cbsd_category,
    )


def frozen_to_iap_grant_rf(frozen: FrozenLocalGrantRf) -> Any | None:
    """Map a frozen local grant to IAP ``GrantRfInfo``."""
    from services.iap import GrantRfInfo

    if frozen.latitude is None or frozen.longitude is None:
        return None
    height = float(frozen.height_m if frozen.height_m is not None else 0.0)
    return GrantRfInfo(
        grant_id=frozen.grant_id,
        cbsd_id=frozen.cbsd_id,
        latitude=float(frozen.latitude),
        longitude=float(frozen.longitude),
        height_m=height,
        height_is_agl=frozen.height_type != "AMSL",
        indoor=bool(frozen.indoor),
        low_hz=int(frozen.low_hz),
        high_hz=int(frozen.high_hz),
        max_eirp_dbm_mhz=float(
            frozen.max_eirp_dbm_mhz if frozen.max_eirp_dbm_mhz is not None else 0.0
        ),
        is_managing_sas=True,
        grant_pk=frozen.grant_pk,
        source_sas_id=None,
        cbsd_category=str(frozen.cbsd_category) if frozen.cbsd_category else None,
        antenna_azimuth_deg=frozen.antenna_azimuth,
        antenna_beamwidth_deg=frozen.antenna_beamwidth,
        antenna_gain_dbi=frozen.antenna_gain,
    )

@dataclass(frozen=True)
class CpasDecision:
    grant_pk: int
    grant_id: str
    cbsd_id: str
    reason: str
    # : IAP / protection action. Boolean peer rules use terminate.
    action: str = "terminate"
    authorized_eirp_dbm_mhz: float | None = None
    explanation: str = ""


def is_cpas_running(db: Session) -> bool:
    from services.meas_report import admin_flag_set

    return admin_flag_set(db, FLAG_CPAS_RUNNING)


def get_daily_activities_completed(db: Session) -> bool:
    """Harness async contract: completed=true only when CPAS is not running."""
    return not is_cpas_running(db)


def _clear_cpas_running_flag(db: Session) -> None:
    try:
        clear_admin_flags(db, FLAG_CPAS_RUNNING)
    except Exception:
        logger.exception("Failed to clear CPAS running flag")
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback after CPAS flag clear error")


def trigger_daily_activities(db: Session) -> None:
    """Start CPAS once.

 - ``production`` / ``test``: enqueue Celery ``run_cpas`` (requires broker/worker).
 - ``certification``: run the same ``execute_cpas_pipeline`` in-process
 without a broker, on a worker thread so ``/get_daily_activities_status``
 can still observe ``completed=false`` while CPAS runs.

 Duplicate calls while CPAS is already running are no-ops.
 """
    from config import get_settings

    with _cpas_dispatch_lock:
        if is_cpas_running(db):
            return

        set_admin_flag(db, FLAG_CPAS_RUNNING)
        mode = get_settings().sas_execution_mode

        if mode == "certification":
            try:
                worker = threading.Thread(
                    target=_run_certification_cpas,
                    name="cpas-certification",
                    daemon=True,
                )
                worker.start()
            except Exception:
                logger.exception("Failed to start certification CPAS thread")
                _clear_cpas_running_flag(db)
                raise
            return

        # production and test: Celery worker path
        try:
            from tasks import run_cpas

            run_cpas.delay()
        except Exception:
            logger.exception(
                "Failed to enqueue CPAS Celery task; clearing running flag"
            )
            _clear_cpas_running_flag(db)
            raise
        return


def _run_certification_cpas() -> None:
    """In-process CPAS worker for certification mode (same domain pipeline)."""
    from database import SessionLocal

    session = SessionLocal()
    try:
        execute_cpas_pipeline(session)
    except Exception:
        logger.exception("CPAS certification-mode pipeline failed")
        try:
            session.rollback()
        except Exception:
            logger.exception("Failed to rollback certification CPAS session")
    finally:
        _clear_cpas_running_flag(session)
        session.close()


def _append_cpas_audit(db: Session, event: str, detail: dict[str, Any]) -> None:
    from services.request_context import context_as_dict

    db.add(
        AdminInjectedData(
            kind=KIND_CPAS_AUDIT,
            data_json=json.dumps(
                {
                    "event": event,
                    "at": utc_now().replace(microsecond=0).isoformat(),
                    **context_as_dict(),
                    **detail,
                },
                default=str,
            ),
        )
    )


def freeze_cpas_snapshot(
    db: Session, peer_sync_report: dict[str, Any] | None = None
) -> CpasSnapshot:
    """Capture active grant PKs, local RF params, and peer FAD rows."""
    # SessionLocal uses autoflush=False; pending peer/grant rows must be visible.
    db.flush()
    grants = (
        db.query(Grant)
        .filter_by(terminated=False)
        .order_by(Grant.id)
        .all()
    )
    local_grants: list[FrozenLocalGrantRf] = []
    for grant in grants:
        cbsd = db.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).first()
        if cbsd is None:
            continue
        local_grants.append(_frozen_local_grant_from_orm(grant, cbsd))
    peer_rows = (
        db.query(PeerFadRecord)
        .order_by(PeerFadRecord.peer_sas_id, PeerFadRecord.record_type, PeerFadRecord.id)
        .all()
    )
    peer_records = tuple(
        (int(row.peer_sas_id), row.record_type, row.record_id, row.data_json)
        for row in peer_rows
    )
    from services.iap.protection_points import capture_protection_records_for_freeze
    from services.exclusion_zone_service import (
        ExclusionZoneError,
        ExclusionZoneUnavailable,
    )

    try:
        protection_records = capture_protection_records_for_freeze(db)
    except (ExclusionZoneError, ExclusionZoneUnavailable) as exc:
        raise CpasRfEvaluationError(f"CPAS EXZ freeze failed: {exc}") from exc

    from services.iap.admission import current_generation_fingerprint

    auth_gen = current_generation_fingerprint(db)
    frozen_locals = tuple(local_grants)
    return CpasSnapshot(
        frozen_at=utc_now().replace(microsecond=0).isoformat(),
        active_grant_pks=tuple(g.grant_pk for g in frozen_locals),
        peer_sync_report=dict(peer_sync_report or {}),
        peer_record_count=len(peer_records),
        peer_records=peer_records,
        local_grants=frozen_locals,
        protection_records=protection_records,
        authorization_generation={
            "peer_generations": dict(auth_gen.get("peer_generations") or {}),
            "injection_generations": dict(auth_gen.get("injection_generations") or {}),
        },
    )


def _frozen_peer_records(
    snapshot: CpasSnapshot, record_type: str
) -> list[dict[str, Any]]:
    """Parse peer records of one type from the frozen snapshot (not live DB)."""
    out: list[dict[str, Any]] = []
    for _peer_sas_id, rt, _record_id, data_json in snapshot.peer_records:
        if rt != record_type:
            continue
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _frozen_peer_cbsd_rows(
    snapshot: CpasSnapshot,
) -> list[tuple[int, dict[str, Any]]]:
    """Frozen peer CBSD records with owning peer_sas_id (not live DB)."""
    out: list[tuple[int, dict[str, Any]]] = []
    for peer_sas_id, rt, _record_id, data_json in snapshot.peer_records:
        if rt != "cbsd":
            continue
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append((int(peer_sas_id), data))
    return out


def evaluate_cpas_protections(
    db: Session,
    snapshot: CpasSnapshot,
    *,
    iap_points: list[Any] | None = None,
    iap_coupling: Any | None = None,
    build_iap_points_from_db: bool = True,
    path_loss_fn: Any | None = None,
    rf_port: Any | None = None,
) -> list[CpasDecision]:
    """Compute grant decisions against the frozen snapshot (no DB writes).

 Boolean peer rules (same CBSD / PPA / ESC) terminate first. IAP runs when
 frozen protection entities yield ProtectionPoints and production (or
 override) coupling is available. DPA movelists then use effective EIRPs.

 Precedence for IAP inputs:
 * explicit ``iap_points`` / ``iap_coupling`` (tests) override production;
 * otherwise points come from ``snapshot.protection_records`` and coupling
 from ``make_production_iap_coupling(rf_port=...)``;
 * entities present + coupling unavailable → ``CpasRfEvaluationError``.

 Local RF/install parameters come from ``snapshot.local_grants`` (generation N).
 """
    if not snapshot.active_grant_pks and not snapshot.local_grants:
        return []
    from services.iap.coupling import IapCouplingUnavailable
    from services.mcp_protection import (
        effective_eirp_by_grant_id,
        iap_terminated_grant_ids,
        merge_constraint_decisions,
        resolve_iap_context,
    )

    local_grants = list(snapshot.local_grants)
    if not local_grants and snapshot.active_grant_pks:
        # Legacy snapshots that only carried PKs: hydrate once for this evaluation.
        rows = (
            db.query(Grant)
            .filter(Grant.id.in_(snapshot.active_grant_pks))
            .order_by(Grant.id)
            .all()
        )
        for grant in rows:
            cbsd = db.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).first()
            if cbsd is not None:
                local_grants.append(_frozen_local_grant_from_orm(grant, cbsd))

    peer_cbsd = _frozen_peer_records(snapshot, "cbsd")
    peer_zones = _frozen_peer_records(snapshot, "zone")
    peer_esc = _frozen_peer_records(snapshot, "esc_sensor")
    pal_by_id = _frozen_pal_index(snapshot.protection_records)
    peer_decisions: list[CpasDecision] = []
    decided_pks: set[int] = set()
    for frozen in local_grants:
        if frozen.terminated:
            continue
        reason: str | None = None
        if _frozen_peer_has_grant_for_cbsd(frozen, peer_cbsd):
            reason = "peer_same_cbsd_grant"
        elif _frozen_conflicts_peer_ppa(
            db, frozen, peer_zones, pal_by_id=pal_by_id
        ):
            reason = "peer_ppa"
        elif _frozen_conflicts_peer_esc(frozen, peer_esc):
            reason = "peer_esc"
        if reason:
            peer_decisions.append(
                CpasDecision(
                    grant_pk=frozen.grant_pk,
                    grant_id=frozen.grant_id,
                    cbsd_id=frozen.cbsd_id,
                    reason=reason,
                    action="terminate",
                    explanation=reason,
                )
            )
            decided_pks.add(frozen.grant_pk)

    # Pre-IAP EZ terminations (GWPZ / FSS+GWBL / TTC purge) — local only.
    from services.iap.pre_iap_exclusions import evaluate_pre_iap_exclusions
    from services.iap.protection_points import ProtectionEntityError

    try:
        for frozen, reason in evaluate_pre_iap_exclusions(
            local_grants, snapshot.protection_records, db=db
        ):
            if frozen.grant_pk in decided_pks:
                continue
            peer_decisions.append(
                CpasDecision(
                    grant_pk=frozen.grant_pk,
                    grant_id=frozen.grant_id,
                    cbsd_id=frozen.cbsd_id,
                    reason=reason,
                    action="terminate",
                    explanation=reason,
                )
            )
            decided_pks.add(frozen.grant_pk)
    except ProtectionEntityError as exc:
        raise CpasRfEvaluationError(f"pre-IAP exclusion failed: {exc}") from exc

    # Grants already given a terminal peer/pre-IAP decision do not need later RF
    # stages. If every active grant in this snapshot is terminally resolved here,
    # skip IAP/DPA entirely (RF engine availability is irrelevant).
    unresolved = [
        frozen
        for frozen in local_grants
        if not frozen.terminated and frozen.grant_pk not in decided_pks
    ]
    if not unresolved:
        return merge_constraint_decisions(peer_decisions, [], [])

    # Production IAP: build points from frozen protection_records + production
    # coupling unless explicit test overrides are provided.
    # Any unresolved grant still subject to required RF → fail-closed on coupling
    # unavailability ( INV-FAIL-01); no soft-success / partial apply.
    iap_decisions: list[CpasDecision] = []
    try:
        iap_ctx = resolve_iap_context(
            db,
            protection_records=snapshot.protection_records,
            iap_points=iap_points,
            iap_coupling=iap_coupling,
            build_from_db=build_iap_points_from_db,
            peer_esc_records=peer_esc,
            rf_port=rf_port,
        )
    except IapCouplingUnavailable as exc:
        raise CpasRfEvaluationError(str(exc)) from exc

    if iap_ctx.coupling is not None and iap_ctx.points:
        iap_decisions = _evaluate_iap_decisions_from_frozen(
            local_grants,
            snapshot=snapshot,
            decided_pks=decided_pks,
            iap_points=list(iap_ctx.points),
            iap_coupling=iap_ctx.coupling,
        )
        for d in iap_decisions:
            if d.action == "terminate" and d.grant_pk is not None:
                decided_pks.add(d.grant_pk)

    eirp_map = effective_eirp_by_grant_id(iap_decisions)
    exclude_ids = iap_terminated_grant_ids(iap_decisions)

    # Rel1Ext IPR / MCP: DPA uses the same frozen local RF + peer FAD generation
    # as IAP (never live registration_json / EIRP for RF inputs).
    from services.dpa_protection import (
        dpa_grants_from_frozen_peer_cbsds,
        grant_on_any_movelist,
        refresh_activation_movelists,
    )
    from services.propagation.errors import PropagationUnavailableError
    from services.terrain.exceptions import TerrainError

    frozen_dpa: list[Any] = []
    for frozen in local_grants:
        if frozen.grant_pk in decided_pks:
            continue
        if frozen.grant_id in exclude_ids:
            continue
        rf = frozen_to_dpa_grant_rf(frozen)
        if rf is None:
            continue
        if eirp_map and frozen.grant_id in eirp_map:
            from dataclasses import replace

            rf = replace(rf, max_eirp_dbm_mhz=float(eirp_map[frozen.grant_id]))
        frozen_dpa.append(rf)

    peer_dpa_grants = dpa_grants_from_frozen_peer_cbsds(_frozen_peer_cbsd_rows(snapshot))
    dpa_decisions: list[CpasDecision] = []
    if not frozen_dpa and not peer_dpa_grants:
        return merge_constraint_decisions(peer_decisions, iap_decisions, [])

    try:
        refresh_activation_movelists(
            db,
            path_loss_fn=path_loss_fn,
            local_grants=frozen_dpa,
            peer_grants=peer_dpa_grants,
            commit=False,
        )
        for frozen in local_grants:
            if frozen.grant_pk in decided_pks:
                continue
            if grant_on_any_movelist(db, frozen.grant_id):
                dpa_decisions.append(
                    CpasDecision(
                        grant_pk=frozen.grant_pk,
                        grant_id=frozen.grant_id,
                        cbsd_id=frozen.cbsd_id,
                        reason="dpa_movelist",
                        action="terminate",
                        explanation="dpa_movelist",
                    )
                )
                decided_pks.add(frozen.grant_pk)
    except (PropagationUnavailableError, TerrainError) as exc:
        raise CpasRfEvaluationError(
            f"CPAS DPA RF evaluation unavailable: {exc}"
        ) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise CpasRfEvaluationError(
            f"CPAS DPA RF evaluation failed: {type(exc).__name__}: {exc}"
        ) from exc

    dpa_term_ids = {d.grant_id for d in dpa_decisions}
    iap_kept = [
        d
        for d in iap_decisions
        if not (d.grant_id in dpa_term_ids and d.action == "reduce_power")
    ]
    return merge_constraint_decisions(peer_decisions, iap_kept, dpa_decisions)


def _evaluate_iap_decisions_from_frozen(
    local_grants: list[FrozenLocalGrantRf],
    *,
    snapshot: CpasSnapshot,
    decided_pks: set[int],
    iap_points: list[Any],
    iap_coupling: Any,
) -> list[CpasDecision]:
    from services.iap import run_iap
    from services.iap.peer_fad import grant_rf_infos_from_frozen_peer_cbsds

    rf_grants: list[Any] = []
    for frozen in local_grants:
        if frozen.terminated or frozen.grant_pk in decided_pks:
            continue
        info = frozen_to_iap_grant_rf(frozen)
        if info is not None:
            rf_grants.append(info)

    peer_rf = grant_rf_infos_from_frozen_peer_cbsds(
        list(_frozen_peer_cbsd_rows(snapshot))
    )
    rf_grants.extend(peer_rf)

    rf_grants.sort(
        key=lambda g: (
            0 if g.is_managing_sas else 1,
            g.grant_pk if g.grant_pk is not None else 10**18,
            g.source_sas_id or "",
            g.grant_id,
        )
    )

    if not any(g.is_managing_sas for g in rf_grants):
        return []
    run = run_iap(list(iap_points), rf_grants, coupling=iap_coupling)
    out: list[CpasDecision] = []
    for item in run.merged_decisions:
        if not item.grant_pk:
            continue
        if item.action == "keep":
            continue
        out.append(
            CpasDecision(
                grant_pk=item.grant_pk,
                grant_id=item.grant_id,
                cbsd_id=item.cbsd_id,
                reason="iap",
                action=item.action,
                authorized_eirp_dbm_mhz=item.authorized_eirp_dbm_mhz,
                explanation=item.explanation,
            )
        )
    return out


def _local_grant_to_rf_info(db: Session, grant: Grant) -> Any | None:
    """Legacy helper: build IAP RF from live ORM (non-CPAS callers / tests)."""
    cbsd = db.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).first()
    if cbsd is None:
        return None
    return frozen_to_iap_grant_rf(_frozen_local_grant_from_orm(grant, cbsd))


def _evaluate_iap_decisions(
    db: Session,
    grants: list[Grant],
    *,
    snapshot: CpasSnapshot,
    decided_pks: set[int],
    iap_points: list[Any],
    iap_coupling: Any,
) -> list[CpasDecision]:
    """Legacy path kept for direct unit spies; prefer frozen evaluation."""
    local = []
    for grant in grants:
        cbsd = db.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).first()
        if cbsd is not None:
            local.append(_frozen_local_grant_from_orm(grant, cbsd))
    return _evaluate_iap_decisions_from_frozen(
        local,
        snapshot=snapshot,
        decided_pks=decided_pks,
        iap_points=iap_points,
        iap_coupling=iap_coupling,
    )

def apply_cpas_decisions(db: Session, decisions: list[CpasDecision]) -> int:
    """Apply CPAS decisions via lifecycle / EIRP updates (no commit)."""
    from services.lifecycle import GrantEvent, apply_grant_event, lock_grant_row

    changed = 0
    for decision in decisions:
        # Peer / non-local decisions must never mutate the local grant table.
        if decision.grant_pk is None:
            logger.warning(
                "CPAS skip grant_id=%s: missing grant_pk (peer or invalid)",
                decision.grant_id,
            )
            continue
        grant = lock_grant_row(db, decision.grant_id, decision.cbsd_id)
        if grant is None or grant.id != decision.grant_pk:
            query = db.query(Grant).filter_by(id=decision.grant_pk)
            bind = db.get_bind()
            if bind is not None and bind.dialect.name != "sqlite":
                query = query.with_for_update()
            grant = query.first()
        if grant is None or grant.terminated:
            continue
        # Refuse peer-namespaced grant ids even if a PK somehow matched.
        if str(decision.grant_id).startswith("peer/"):
            logger.warning(
                "CPAS skip grant_pk=%s grant_id=%s: peer grant is immutable locally",
                decision.grant_pk,
                decision.grant_id,
            )
            continue

        action = decision.action or "terminate"
        if action == "reduce_power":
            if decision.authorized_eirp_dbm_mhz is None:
                continue
            if grant.max_eirp is not None and float(grant.max_eirp) <= float(
                decision.authorized_eirp_dbm_mhz
            ) + 1e-9:
                continue
            grant.max_eirp = float(decision.authorized_eirp_dbm_mhz)
            changed += 1
            continue
        if action == "suspend":
            event = GrantEvent.SUSPEND
        elif action == "terminate":
            event = GrantEvent.TERMINATE
        else:
            logger.warning(
                "CPAS skip grant_pk=%s grant_id=%s unknown action=%s",
                decision.grant_pk,
                decision.grant_id,
                action,
            )
            continue
        outcome = apply_grant_event(
            grant,
            event,
            payload={
                "cbsdId": grant.cbsd_id,
                "grantId": grant.grant_id,
                "reason": decision.reason,
                "explanation": decision.explanation,
            },
        )
        if not outcome.ok:
            logger.warning(
                "CPAS skip grant_pk=%s grant_id=%s action=%s lifecycle=%s",
                decision.grant_pk,
                decision.grant_id,
                action,
                outcome.detail or outcome.response_code,
            )
            continue
        changed += 1
    if changed:
        db.flush()
    return changed


def apply_peer_conflict_to_local_grants(db: Session) -> None:
    """Terminate local grants that conflict with peer FAD (same CBSD, PPA, or ESC).

 Convenience wrapper used by older call sites/tests. Prefer the staged
 pipeline in ``execute_cpas_pipeline`` for daily activities.
 """
    from primitives.coordination import run_snapshot_evaluate_apply
    from services.cpas_cycle import CpasCoordinationCycle

    result = run_snapshot_evaluate_apply(CpasCoordinationCycle(db))
    if result.writes:
        db.commit()


def _dialect_name(db: Session) -> str:
    bind = db.get_bind()
    if bind is None:
        return ""
    return bind.dialect.name


def _authorization_generation_matches(
    frozen: dict[str, Any], live: dict[str, Any]
) -> bool:
    return (
        frozen.get("peer_generations") == live.get("peer_generations")
        and frozen.get("injection_generations") == live.get("injection_generations")
    )


def _run_pipeline_critical_section(
    db: Session,
    snapshot: CpasSnapshot,
    *,
    stages_so_far: list[dict[str, Any]] | None = None,
    evaluate_kwargs: dict[str, Any] | None = None,
) -> tuple[int, int, list[CpasDecision], dict[str, Any]]:
    """Revalidate generation, apply, publish FAD, stamp admission — one transaction.

 Lock order: IAP admission → CPAS pipeline → FAD publish (inside create dump).
 FAD is flushed without committing so grant decisions + dump + admission marker
 share one commit under IAP serialization.
 """
    from services.cpas_reevaluation import (
        clear_cpas_reevaluation_required,
        mark_cpas_reevaluation_required,
    )
    from services.iap.admission import (
        current_generation_fingerprint,
        record_iap_admission_generation,
    )

    with exclusive_iap_admission():
        acquire_iap_admission_xact_lock(db)
        acquire_cpas_pipeline_xact_lock(db)

        live = current_generation_fingerprint(db)
        if not _authorization_generation_matches(
            snapshot.authorization_generation, live
        ):
            mark_cpas_reevaluation_required(
                db,
                reason="generation_drift_before_cpas_apply",
                generation={
                    "frozen": snapshot.authorization_generation,
                    "live": {
                        "peer_generations": live.get("peer_generations"),
                        "injection_generations": live.get("injection_generations"),
                    },
                },
            )
            db.commit()
            raise CpasGenerationDriftError(
                "CPAS refused stale apply: peer/protection generation drifted "
                "after freeze"
            )

        # Recompute under coordination so TOCTOU after freeze cannot widen the set;
        # still constrained to snapshot.active_grant_pks.
        from services.cpas_cycle import CpasCoordinationCycle

        cycle = CpasCoordinationCycle(db, evaluate_kwargs=evaluate_kwargs)
        decisions = cycle.evaluate(snapshot)
        terminated = cycle.apply(snapshot, decisions).writes
        dump = create_full_activity_dump(db, commit=False)

        from services.cpas_schedule_service import mark_scheduled_success_if_applicable

        mark_scheduled_success_if_applicable(db)
        clear_cpas_reevaluation_required(db)
        # Stamp the exact generation frozen/evaluated — not a newer live map.
        admission_gen = record_iap_admission_generation(
            db, fingerprint=snapshot.authorization_generation
        )
        stage_names = [s["name"] for s in (stages_so_far or [])]
        stage_names.extend(
            ["apply_decisions_and_generate_fad", "finalize_status_audit"]
        )
        decision_rows = [
            {
                "grant_id": d.grant_id,
                "cbsd_id": d.cbsd_id,
                "reason": d.reason,
            }
            for d in decisions
        ]
        _append_cpas_audit(
            db,
            "cpas_completed",
            {
                "dumpId": int(dump.id),
                "terminatedGrants": terminated,
                "stages": stage_names,
                "decisions": decision_rows,
                "iapAdmissionGeneration": {
                    "peer_generations": admission_gen.get("peer_generations"),
                    "injection_generations": admission_gen.get(
                        "injection_generations"
                    ),
                },
            },
        )
        db.commit()
        return terminated, int(dump.id), decisions, admission_gen


def execute_cpas_pipeline(db: Session) -> dict[str, Any]:
    """Run the transactional CPAS pipeline; return a structured stage report.

 Peer/database sync may commit durable inputs. Grant terminations, the new
 local FAD, and the IAP admission-generation stamp are applied in one critical
 section under IAP admission serialization so a failed FAD publish rolls back
 the grant decisions and no concurrent Grant can observe a partial baseline.
 """
    from runtime.context import lookup_runtime_composition, runtime_composition_scope
    from runtime.data_access import ProviderNotBoundError
    from services.database_sync_service import sync_injected_database_urls

    result: dict[str, Any] = {
        "ok": False,
        "stages": [],
        "dump_id": None,
        "terminated_grants": 0,
        "decisions": [],
    }

    def _stage(name: str, **extra: Any) -> None:
        result["stages"].append({"name": name, "ok": True, **extra})

    composition = lookup_runtime_composition()
    if composition is None:
        raise ProviderNotBoundError(
            "RuntimeComposition is not bound; process startup must initialize "
            "composition before CPAS executes"
        )

    with runtime_composition_scope(composition):
        try:
            sync_injected_database_urls(db)
            _stage("sync_databases")

            peer_report = run_peer_fad_sync(db)
            _stage(
                "sync_peer_fads",
                peers=peer_report.get("peers"),
                ok_peers=peer_report.get("ok"),
                failed_peers=peer_report.get("failed"),
            )

            from services.cpas_cycle import CpasCoordinationCycle

            evaluate_kwargs = {"rf_port": composition.rf}
            cycle = CpasCoordinationCycle(
                db,
                peer_sync_report=peer_report,
                evaluate_kwargs=evaluate_kwargs,
            )
            snapshot = cycle.snapshot()
            _stage(
                "freeze_snapshot",
                frozen_at=snapshot.frozen_at,
                active_grants=len(snapshot.active_grant_pks),
                peer_records=snapshot.peer_record_count,
            )

            # Preview outside the lock (observability); authoritative set is recomputed inside.
            preview = cycle.evaluate(snapshot)
            _stage("evaluate_protections", decision_count=len(preview))

            if _dialect_name(db) == "sqlite":
                with _cpas_pipeline_lock:
                    terminated, dump_id, decisions, admission_gen = (
                        _run_pipeline_critical_section(
                            db,
                            snapshot,
                            stages_so_far=list(result["stages"]),
                            evaluate_kwargs=evaluate_kwargs,
                        )
                    )
            else:
                terminated, dump_id, decisions, admission_gen = (
                    _run_pipeline_critical_section(
                        db,
                        snapshot,
                        stages_so_far=list(result["stages"]),
                        evaluate_kwargs=evaluate_kwargs,
                    )
                )

            result["decisions"] = [
                {
                    "grant_id": d.grant_id,
                    "cbsd_id": d.cbsd_id,
                    "reason": d.reason,
                }
                for d in decisions
            ]
            result["terminated_grants"] = terminated
            result["dump_id"] = dump_id
            _stage(
                "apply_decisions_and_generate_fad",
                dump_id=dump_id,
                terminated=terminated,
                decision_count=len(decisions),
                iap_admission_generation={
                    "peer_generations": admission_gen.get("peer_generations"),
                    "injection_generations": admission_gen.get(
                        "injection_generations"
                    ),
                },
            )
            result["ok"] = True
            _stage("finalize_status_audit")
            logger.info(
                "CPAS pipeline completed dump_id=%s terminated=%s decisions=%s",
                dump_id,
                terminated,
                len(decisions),
            )
            return result
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                logger.exception("CPAS pipeline rollback failed")
            try:
                _append_cpas_audit(
                    db,
                    "cpas_failed",
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "stages": result.get("stages") or [],
                    },
                )
                db.commit()
            except Exception:
                logger.exception("CPAS failure audit could not be persisted")
            logger.exception("CPAS pipeline failed")
            raise


def _peer_cbsd_records(db: Session) -> list[dict[str, Any]]:
    rows = db.query(PeerFadRecord).filter_by(record_type="cbsd").all()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _active_peer_grants(record: dict[str, Any]) -> list[dict[str, Any]]:
    grants = record.get("grants") or []
    if not isinstance(grants, list):
        return []
    active: list[dict[str, Any]] = []
    for g in grants:
        if not isinstance(g, dict):
            continue
        if g.get("terminated") is True:
            continue
        active.append(g)
    return active


def peer_has_grant_for_cbsd(
    db: Session,
    cbsd: Cbsd,
    *,
    peer_cbsd_records: list[dict[str, Any]] | None = None,
) -> bool:
    """True when any peer FAD CBSD record matches this local CBSD and has an active grant."""
    target_id = fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number)
    records = (
        peer_cbsd_records
        if peer_cbsd_records is not None
        else _peer_cbsd_records(db)
    )
    for record in records:
        if record.get("id") != target_id:
            continue
        if _active_peer_grants(record):
            return True
    return False


def _frozen_peer_has_grant_for_cbsd(
    frozen: FrozenLocalGrantRf, peer_cbsd_records: list[dict[str, Any]]
) -> bool:
    target_id = fad_cbsd_id(frozen.fcc_id, frozen.cbsd_serial_number)
    for record in peer_cbsd_records:
        if record.get("id") != target_id:
            continue
        if _active_peer_grants(record):
            return True
    return False


def _frozen_pal_index(
    protection_records: tuple[tuple[str, str, str], ...],
) -> dict[str, dict[str, Any]]:
    from services.iap.protection_points import KIND_PAL

    out: dict[str, dict[str, Any]] = {}
    for kind, _rid, data_json in protection_records:
        if kind != KIND_PAL:
            continue
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        pid = data.get("palId")
        if pid:
            out[str(pid)] = data
    return out


def _frozen_conflicts_peer_ppa(
    db: Session,
    frozen: FrozenLocalGrantRf,
    peer_zones: list[dict[str, Any]],
    *,
    pal_by_id: dict[str, dict[str, Any]] | None = None,
) -> bool:
    from services.geometry import within_geojson_buffer_m

    if frozen.latitude is None or frozen.longitude is None:
        return False
    buffer_m = _peer_ppa_buffer_m()
    for record in peer_zones:
        if record.get("usage") != "PPA" and "ppaInfo" not in record:
            continue
        if record.get("terminated") is True:
            continue
        if not within_geojson_buffer_m(
            float(frozen.latitude),
            float(frozen.longitude),
            record.get("zone"),
            buffer_m,
        ):
            continue
        for low, high in _ppa_protected_ranges(db, record, pal_by_id=pal_by_id):
            if _freq_overlaps(frozen.low_hz, frozen.high_hz, low, high):
                return True
    return False


def _frozen_conflicts_peer_esc(
    frozen: FrozenLocalGrantRf,
    peer_esc_records: list[dict[str, Any]],
) -> bool:
    from services.geometry import haversine_m

    esc_radius_m, esc_low, esc_high = _peer_esc_params()
    if not _freq_overlaps(frozen.low_hz, frozen.high_hz, esc_low, esc_high):
        return False
    if frozen.latitude is None or frozen.longitude is None:
        return False
    for record in peer_esc_records:
        inst = record.get("installationParam") or {}
        esc_lat, esc_lon = inst.get("latitude"), inst.get("longitude")
        if esc_lat is None or esc_lon is None:
            continue
        if (
            haversine_m(
                float(frozen.latitude),
                float(frozen.longitude),
                float(esc_lat),
                float(esc_lon),
            )
            <= esc_radius_m
        ):
            return True
    return False


def _required_distance_exclusion_binding(binding_id: str) -> DistanceExclusionBinding:
    """Resolve a required CBRS peer protection binding from the active canonical Profile."""
    profile = get_active_profile_document()
    if profile.protection is None:
        raise CpasRfEvaluationError(
            f"required protection binding '{binding_id}' is missing "
            f"(active profile '{profile.metadata.id}' has no protection section)"
        )
    for item in profile.protection.bindings:
        if item.id == binding_id:
            return item
    raise CpasRfEvaluationError(
        f"required protection binding '{binding_id}' is missing "
        f"(active profile '{profile.metadata.id}')"
    )


def _peer_esc_params() -> tuple[float, int, int]:
    binding = _required_distance_exclusion_binding("peer_esc")
    if binding.frequency is None:
        raise CpasRfEvaluationError(
            "required protection binding 'peer_esc' is missing a frequency scope"
        )
    return (
        float(binding.distance_m),
        int(binding.frequency.low_hz),
        int(binding.frequency.high_hz),
    )


def _peer_ppa_buffer_m() -> float:
    binding = _required_distance_exclusion_binding("peer_ppa")
    if binding.frequency is not None:
        raise CpasRfEvaluationError(
            "peer_ppa distance binding must not declare a frequency scope"
        )
    return float(binding.distance_m)


def _peer_records_of_type(db: Session, record_type: str) -> list[dict[str, Any]]:
    rows = db.query(PeerFadRecord).filter_by(record_type=record_type).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _cbsd_lat_lon(cbsd: Cbsd) -> tuple[float | None, float | None]:
    from adapters.device import MappingDeviceAdapter
    from runtime.context import get_bound_runtime_composition
    from services.spectrum_inquiry_service import _cbsd_location

    composition = get_bound_runtime_composition()
    adapter = (
        composition.device_adapter
        if composition is not None and composition.device_adapter is not None
        else MappingDeviceAdapter()
    )
    return _cbsd_location(cbsd, adapter)


def _freq_overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _ppa_protected_ranges(
    db: Session,
    ppa: dict[str, Any],
    *,
    pal_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[int, int]]:
    """Resolve PPA-protected frequencies via PAL records (frozen preferred)."""
    from services.iap.protection_points import _pal_freq_from_record
    from services.pal_service import load_pal_records
    from services.spectrum_inquiry_service import _pal_freq

    ppa_info = ppa.get("ppaInfo") or {}
    pal_ids = ppa_info.get("palId") or []
    if not pal_ids:
        return []
    if pal_by_id is None:
        pals = load_pal_records(db)
        pal_by_id = {str(p.get("palId")): p for p in pals if p.get("palId")}
    ranges: list[tuple[int, int]] = []
    for pal_id in pal_ids:
        pal = pal_by_id.get(str(pal_id)) or pal_by_id.get(pal_id)
        if not pal:
            continue
        pf = _pal_freq_from_record(pal) or _pal_freq(pal)
        if pf:
            ranges.append(pf)
    return ranges


def _grant_conflicts_peer_ppa(
    db: Session,
    cbsd: Cbsd,
    grant: Grant,
    peer_zones: list[dict[str, Any]] | None = None,
) -> bool:
    """True when CBSD is in/near a peer PPA and the grant overlaps the PPA PAL band."""
    from services.geometry import within_geojson_buffer_m

    lat, lon = _cbsd_lat_lon(cbsd)
    if lat is None or lon is None:
        return False
    buffer_m = _peer_ppa_buffer_m()
    zones = peer_zones if peer_zones is not None else _peer_records_of_type(db, "zone")
    for record in zones:
        if record.get("usage") != "PPA" and "ppaInfo" not in record:
            continue
        if record.get("terminated") is True:
            continue
        if not within_geojson_buffer_m(lat, lon, record.get("zone"), buffer_m):
            continue
        for low, high in _ppa_protected_ranges(db, record):
            if _freq_overlaps(grant.low_frequency, grant.high_frequency, low, high):
                return True
    return False


def _grant_conflicts_peer_esc(
    db: Session,
    cbsd: Cbsd,
    grant: Grant,
    peer_esc_records: list[dict[str, Any]] | None = None,
) -> bool:
    """True when CBSD is within ESC protection distance of a peer ESC sensor."""
    from services.geometry import haversine_m

    esc_radius_m, esc_low, esc_high = _peer_esc_params()
    if not _freq_overlaps(
        grant.low_frequency, grant.high_frequency, esc_low, esc_high
    ):
        return False
    lat, lon = _cbsd_lat_lon(cbsd)
    if lat is None or lon is None:
        return False
    sensors = (
        peer_esc_records
        if peer_esc_records is not None
        else _peer_records_of_type(db, "esc_sensor")
    )
    for record in sensors:
        inst = record.get("installationParam") or {}
        esc_lat, esc_lon = inst.get("latitude"), inst.get("longitude")
        if esc_lat is None or esc_lon is None:
            continue
        if haversine_m(lat, lon, float(esc_lat), float(esc_lon)) <= esc_radius_m:
            return True
    return False
