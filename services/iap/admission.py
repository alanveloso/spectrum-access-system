"""Grant-time IAP admission gate ().

Evaluates a *proposed* Grant at its requested maxEirp against residual IAP
headroom derived from the coherent post-CPAS authorization context:

* existing local grants at currently authorized EIRPs
* peer FAD grants from one durable generation
* protection points from the same coherent state
* existing IAP aggregate / coupling math (no fair-share mutation)

Unsafe proposals are denied (INTERFERENCE). The gate never authorizes a
hidden reduced EIRP and never runs full CPAS.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from models.models import Cbsd, Grant, PeerFadRecord, PeerSas
from services.iap.aggregate import (
    apply_pre_iap_margin_db,
    dbm_to_mw,
    grant_overlaps_channel,
    overlapping_iap_channels,
)
from services.iap.engine import InterferenceCoupling, grants_in_neighborhood
from services.iap.models import GrantRfInfo, ProtectionPoint

logger = logging.getLogger(__name__)

KIND_IAP_ADMISSION_GEN = "iap_admission_generation"
PROPOSED_GRANT_ID = "__proposed__/iap_admission"
_HEADROOM_EPS_MW = 1e-15


class IapAdmissionError(Exception):
    """Fail-closed admission evaluation (indeterminate RF / generation)."""


@dataclass(frozen=True)
class IapAdmissionChannelResult:
    point_id: str
    low_hz: int
    high_hz: int
    managing_mw: float
    peer_mw: float
    aggregate_mw: float
    threshold_mw: float
    residual_mw: float
    proposal_mw: float
    admit: bool


@dataclass(frozen=True)
class IapAdmissionDecision:
    allow: bool
    reason: str
    applicable: bool
    channels: tuple[IapAdmissionChannelResult, ...] = ()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cbsd_registration(cbsd: Cbsd) -> dict[str, Any]:
    try:
        data = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def proposed_grant_rf_info(
    cbsd: Cbsd,
    *,
    low_hz: int,
    high_hz: int,
    max_eirp_dbm_mhz: float,
    grant_id: str = PROPOSED_GRANT_ID,
) -> GrantRfInfo:
    """Build in-memory ``GrantRfInfo`` for a proposed request (no DB row)."""
    reg = _cbsd_registration(cbsd)
    install = reg.get("installationParam")
    if not isinstance(install, dict):
        raise IapAdmissionError("proposed grant missing installationParam")
    try:
        lat = float(install["latitude"])
        lon = float(install["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IapAdmissionError("proposed grant missing latitude/longitude") from exc
    try:
        height = float(install.get("height") or 0.0)
    except (TypeError, ValueError) as exc:
        raise IapAdmissionError("proposed grant invalid height") from exc
    height_type = str(install.get("heightType") or "AGL").upper()
    raw_cat = reg.get("cbsdCategory") or cbsd.cbsd_category
    cat = str(raw_cat).strip().upper() if raw_cat not in (None, "") else None
    if cat not in {"A", "B"}:
        cat = None

    def _opt(key: str) -> float | None:
        if key not in install or install.get(key) is None:
            return None
        try:
            return float(install[key])
        except (TypeError, ValueError):
            return None

    return GrantRfInfo(
        grant_id=grant_id,
        cbsd_id=str(cbsd.cbsd_id),
        latitude=lat,
        longitude=lon,
        height_m=height,
        height_is_agl=height_type != "AMSL",
        indoor=bool(install.get("indoorDeployment", False)),
        low_hz=int(low_hz),
        high_hz=int(high_hz),
        max_eirp_dbm_mhz=float(max_eirp_dbm_mhz),
        is_managing_sas=True,
        grant_pk=None,
        source_sas_id=None,
        cbsd_category=cat,
        antenna_azimuth_deg=_opt("antennaAzimuth"),
        antenna_beamwidth_deg=_opt("antennaBeamwidth"),
        antenna_gain_dbi=_opt("antennaGain"),
    )


def current_generation_fingerprint(db: Session) -> dict[str, Any]:
    """Durable peer + injection generation fingerprint (no network I/O)."""
    from services.data_injection_service import get_injection_generations

    peers = (
        db.query(PeerSas)
        .order_by(PeerSas.id)
        .all()
    )
    peer_generations = {
        str(int(p.id)): p.last_fad_generation for p in peers
    }
    return {
        "peer_generations": peer_generations,
        "injection_generations": dict(get_injection_generations(db) or {}),
    }


def record_iap_admission_generation(
    db: Session,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist coherent-generation marker used by grant-time admission.

 When ``fingerprint`` is provided (CPAS apply path), stamp *that* evaluated
 generation — never a newer live map that drifted after freeze/evaluate.
 """
    from models.models import AdminInjectedData

    base = fingerprint if fingerprint is not None else current_generation_fingerprint(db)
    payload = {
        "peer_generations": dict(base.get("peer_generations") or {}),
        "injection_generations": dict(base.get("injection_generations") or {}),
        "recordedAt": _utc_now_iso(),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    row = (
        db.query(AdminInjectedData)
        .filter_by(kind=KIND_IAP_ADMISSION_GEN)
        .order_by(AdminInjectedData.id.desc())
        .first()
    )
    if row:
        row.data_json = raw
    else:
        db.add(AdminInjectedData(kind=KIND_IAP_ADMISSION_GEN, data_json=raw))
    db.flush()
    return payload


def load_iap_admission_generation(db: Session) -> dict[str, Any] | None:
    from models.models import AdminInjectedData

    row = (
        db.query(AdminInjectedData)
        .filter_by(kind=KIND_IAP_ADMISSION_GEN)
        .order_by(AdminInjectedData.id.desc())
        .first()
    )
    if not row:
        return None
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def coherent_generation_available(db: Session) -> bool:
    """True when a durable marker exists and matches live peer/injection gens.

 Missing marker → not coherent (applicable IAP must fail closed until CPAS
 stamps a generation). Reevaluation-required or mismatch → not coherent.
 """
    from services.cpas_reevaluation import cpas_reevaluation_required

    if cpas_reevaluation_required(db) is not None:
        return False
    marker = load_iap_admission_generation(db)
    if marker is None:
        return False
    live = current_generation_fingerprint(db)
    return bool(
        marker.get("peer_generations") == live["peer_generations"]
        and marker.get("injection_generations") == live["injection_generations"]
    )


def admission_generation_indeterminate(db: Session) -> bool:
    """True when applicable IAP must not authorize (missing/mismatched generation)."""
    from services.cpas_reevaluation import cpas_reevaluation_required

    if cpas_reevaluation_required(db) is not None:
        return True
    marker = load_iap_admission_generation(db)
    if marker is None:
        return True
    live = current_generation_fingerprint(db)
    return not (
        marker.get("peer_generations") == live["peer_generations"]
        and marker.get("injection_generations") == live["injection_generations"]
    )


def collect_local_authorized_grants(db: Session) -> list[GrantRfInfo]:
    """Active local grants at currently authorized (post-CPAS) EIRPs."""
    from services.cpas_service import (
        _frozen_local_grant_from_orm,
        frozen_to_iap_grant_rf,
    )

    out: list[GrantRfInfo] = []
    grants = (
        db.query(Grant)
        .filter_by(terminated=False)
        .order_by(Grant.id)
        .all()
    )
    for grant in grants:
        cbsd = db.query(Cbsd).filter_by(cbsd_id=grant.cbsd_id).first()
        if cbsd is None:
            continue
        frozen = _frozen_local_grant_from_orm(grant, cbsd)
        rf = frozen_to_iap_grant_rf(frozen)
        if rf is not None:
            out.append(rf)
    return out


def collect_peer_grants(db: Session) -> list[GrantRfInfo]:
    """Peer FAD CBSD grants from durable local rows (no network fetch)."""
    from services.iap.peer_fad import grant_rf_infos_from_frozen_peer_cbsds

    rows = (
        db.query(PeerFadRecord)
        .filter_by(record_type="cbsd")
        .order_by(PeerFadRecord.peer_sas_id, PeerFadRecord.id)
        .all()
    )
    parsed: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError as exc:
            raise IapAdmissionError(
                f"malformed peer FAD CBSD record id={row.record_id}"
            ) from exc
        if not isinstance(data, dict):
            raise IapAdmissionError(
                f"peer FAD CBSD record id={row.record_id} is not an object"
            )
        parsed.append((int(row.peer_sas_id), data))
    return grant_rf_infos_from_frozen_peer_cbsds(parsed)


def evaluate_proposal_against_headroom(
    proposed: GrantRfInfo,
    baseline: Sequence[GrantRfInfo],
    points: Sequence[ProtectionPoint],
    coupling: InterferenceCoupling,
) -> IapAdmissionDecision:
    """Admit iff proposal ≤ residual headroom on every applicable channel.

 Residual = threshold_mw(after pre-IAP margin) − baseline aggregate (local +
 peer) at the protection point/channel. The proposal does not enter
 fair-share reallocation.
 """
    channel_results: list[IapAdmissionChannelResult] = []
    applicable = False

    for point in points:
        neighbors = grants_in_neighborhood(point, list(baseline) + [proposed])
        if not any(g.grant_id == proposed.grant_id for g in neighbors):
            continue
        baseline_n = [g for g in neighbors if g.grant_id != proposed.grant_id]
        channels = overlapping_iap_channels(point.low_hz, point.high_hz)
        threshold_dbm = apply_pre_iap_margin_db(
            point.threshold_dbm, point.pre_iap_margin_db
        )
        threshold_mw = dbm_to_mw(threshold_dbm)

        for channel in channels:
            if not grant_overlaps_channel(
                proposed, channel, entity_kind=point.entity_kind
            ):
                continue
            applicable = True
            managing_mw = 0.0
            peer_mw = 0.0
            for g in baseline_n:
                if not grant_overlaps_channel(
                    g, channel, entity_kind=point.entity_kind
                ):
                    continue
                mw = float(coupling(g, point, channel, float(g.max_eirp_dbm_mhz)))
                if mw < 0.0:
                    raise IapAdmissionError("coupling returned negative interference")
                if g.is_managing_sas:
                    managing_mw += mw
                else:
                    peer_mw += mw
            aggregate_mw = managing_mw + peer_mw
            residual_mw = float(threshold_mw) - float(aggregate_mw)
            proposal_mw = float(
                coupling(
                    proposed, point, channel, float(proposed.max_eirp_dbm_mhz)
                )
            )
            if proposal_mw < 0.0:
                raise IapAdmissionError("coupling returned negative interference")
            admit = proposal_mw <= residual_mw + _HEADROOM_EPS_MW
            channel_results.append(
                IapAdmissionChannelResult(
                    point_id=point.point_id,
                    low_hz=channel.low_hz,
                    high_hz=channel.high_hz,
                    managing_mw=float(managing_mw),
                    peer_mw=float(peer_mw),
                    aggregate_mw=float(aggregate_mw),
                    threshold_mw=float(threshold_mw),
                    residual_mw=float(residual_mw),
                    proposal_mw=float(proposal_mw),
                    admit=admit,
                )
            )
            if not admit:
                return IapAdmissionDecision(
                    allow=False,
                    reason="iap_headroom_exceeded",
                    applicable=True,
                    channels=tuple(channel_results),
                )

    if not applicable:
        return IapAdmissionDecision(
            allow=True,
            reason="no_applicable_iap_constraint",
            applicable=False,
            channels=tuple(channel_results),
        )
    return IapAdmissionDecision(
        allow=True,
        reason="within_iap_headroom",
        applicable=True,
        channels=tuple(channel_results),
    )


def filter_points_for_proposed_cbsd(
    db: Session,
    points: Sequence[ProtectionPoint],
    cbsd: Cbsd,
) -> list[ProtectionPoint]:
    """Drop PPA points whose cluster list includes the proposing CBSD.

 WInnForum PPA IAP excludes cluster members from the interferer set; a
 cluster owner's own PPA must not deny their PAL grant at admission.
 """
    from services.data_injection_service import KIND_ZONE, load_injected
    from services.fad_service import fad_cbsd_id
    from services.iap.models import ProtectedEntityKind

    if not any(p.entity_kind is ProtectedEntityKind.PPA for p in points):
        return list(points)

    refs = {str(cbsd.cbsd_id)}
    try:
        refs.add(fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number))
    except Exception:  # noqa: BLE001
        pass

    cluster_point_ids: set[str] = set()
    for payload in load_injected(db, KIND_ZONE):
        record = (
            payload.get("record")
            if isinstance(payload, dict) and isinstance(payload.get("record"), dict)
            else payload
        )
        if not isinstance(record, dict):
            continue
        ppa_info = record.get("ppaInfo") or {}
        cluster = {str(x) for x in (ppa_info.get("cbsdReferenceId") or [])}
        if refs.isdisjoint(cluster):
            continue
        rid = str(record.get("id") or "").strip()
        if rid:
            cluster_point_ids.add(f"ppa:{rid}")

    if not cluster_point_ids:
        return list(points)
    return [
        p
        for p in points
        if not (
            p.entity_kind is ProtectedEntityKind.PPA and p.point_id in cluster_point_ids
        )
    ]


def proposal_has_applicable_iap_constraint(
    proposed: GrantRfInfo,
    points: Sequence[ProtectionPoint],
) -> bool:
    """True when at least one protection point/channel applies to the proposal."""
    for point in points:
        neighbors = grants_in_neighborhood(point, [proposed])
        if not any(g.grant_id == proposed.grant_id for g in neighbors):
            continue
        for channel in overlapping_iap_channels(point.low_hz, point.high_hz):
            if grant_overlaps_channel(
                proposed, channel, entity_kind=point.entity_kind
            ):
                return True
    return False


def proposed_grant_violates_iap(
    db: Session,
    cbsd: Cbsd,
    *,
    low_hz: int,
    high_hz: int,
    max_eirp_dbm_mhz: float,
    coupling: InterferenceCoupling | None = None,
    points: Sequence[ProtectionPoint] | None = None,
    rf_port: object | None = None,
) -> bool:
    """True when the proposal must be denied (unsafe or fail-closed).

 Returns False when no applicable IAP protection exists (ordinary grants).
 """
    from services.iap.coupling import IapCouplingUnavailable, iap_enabled
    from services.iap.protection_points import (
        ProtectionEntityError,
        build_protection_points_from_db,
    )
    from services.mcp_protection import resolve_iap_context

    if not iap_enabled():
        return False

    try:
        if points is None:
            resolved_points = build_protection_points_from_db(db)
        else:
            resolved_points = list(points)
        resolved_points = filter_points_for_proposed_cbsd(db, resolved_points, cbsd)
        from services.fss_provenance import (
            exclude_federal_sync_fss_from_grant_admission,
        )

        # federal database-update FSS is CPAS/heartbeat-owned, not
        # grant-time residual IAP. Admin InjectFss remains eligible.
        resolved_points = exclude_federal_sync_fss_from_grant_admission(
            db, resolved_points
        )
    except ProtectionEntityError as exc:
        logger.warning("IAP admission fail-closed: protection points: %s", exc)
        return True
    except Exception as exc:  # noqa: BLE001 — fail-closed on indeterminate RF state
        logger.warning("IAP admission fail-closed: protection build: %s", exc)
        return True

    if not resolved_points:
        return False

    try:
        proposed = proposed_grant_rf_info(
            cbsd,
            low_hz=low_hz,
            high_hz=high_hz,
            max_eirp_dbm_mhz=max_eirp_dbm_mhz,
        )
    except IapAdmissionError as exc:
        logger.warning("IAP admission fail-closed: proposed RF: %s", exc)
        return True

    # No applicable constraint → ALLOW without requiring the IAP RF backend
    # or a CPAS admission-generation marker.
    if not proposal_has_applicable_iap_constraint(proposed, resolved_points):
        return False

    # Applicable IAP: require a coherent post-CPAS generation marker (fail closed).
    if admission_generation_indeterminate(db):
        logger.warning(
            "IAP admission fail-closed: coherent generation unavailable"
        )
        return True

    try:
        baseline = collect_local_authorized_grants(db) + collect_peer_grants(db)
    except IapAdmissionError as exc:
        logger.warning("IAP admission fail-closed: baseline: %s", exc)
        return True

    # Drop same-CBSD overlapping locals (renewal / replace semantics).
    baseline = [
        g
        for g in baseline
        if not (
            g.is_managing_sas
            and g.cbsd_id == proposed.cbsd_id
            and g.low_hz < high_hz
            and g.high_hz > low_hz
        )
    ]

    if coupling is None:
        try:
            ctx = resolve_iap_context(
                db, iap_points=resolved_points, rf_port=rf_port
            )
            if ctx.coupling is None:
                # Applicable points exist but coupling unavailable → fail closed.
                logger.warning(
                    "IAP admission fail-closed: coupling unavailable"
                )
                return True
            use_coupling = ctx.coupling
            use_points = list(ctx.points) if ctx.points else resolved_points
        except (IapCouplingUnavailable, ProtectionEntityError) as exc:
            logger.warning("IAP admission fail-closed: coupling: %s", exc)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("IAP admission fail-closed: coupling resolve: %s", exc)
            return True
    else:
        use_coupling = coupling
        use_points = resolved_points

    try:
        decision = evaluate_proposal_against_headroom(
            proposed, baseline, use_points, use_coupling
        )
    except IapAdmissionError as exc:
        logger.warning("IAP admission fail-closed: evaluate: %s", exc)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("IAP admission fail-closed: evaluate error: %s", exc)
        return True

    if not decision.allow:
        logger.info(
            "IAP admission DENY cbsd=%s reason=%s channels=%s",
            cbsd.cbsd_id,
            decision.reason,
            len(decision.channels),
        )
    return not decision.allow
