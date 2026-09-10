"""Iterative Allocation Process (IAP) engine — general point/channel fairshare.

Faithful structure to harness ``reference_models.iap.iap.iapPointConstraint``
(WINNF-TS-0061): residual threshold after satisfied grants lock in, all
overlapping channels must meet fairshare, and EIRP drops 1 dB only when a
round makes no grant satisfied. Coupling and neighborhood filtering remain
injectable (no fixture IDs).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from services.iap.aggregate import (
    DEFAULT_EIRP_FLOOR_DBM_MHZ,
    EIRP_STEP_DB,
    apply_pre_iap_margin_db,
    dbm_to_mw,
    grant_overlaps_channel,
    overlapping_iap_channels,
    sum_interference_mw,
)
from services.iap.models import (
    ChannelAggregateResult,
    FrequencyChannel,
    GrantChannelContribution,
    GrantRfInfo,
    IapAction,
    IapGrantDecision,
    IapPointResult,
    IapRunResult,
    ProtectedEntityKind,
    ProtectionPoint,
)

# Coupling: (grant, protection_point, channel, eirp_dbm_mhz) -> interference mW
InterferenceCoupling = Callable[
    [GrantRfInfo, ProtectionPoint, FrequencyChannel, float], float
]


@dataclass(frozen=True)
class IapEngineConfig:
    eirp_floor_dbm_mhz: float = DEFAULT_EIRP_FLOOR_DBM_MHZ
    eirp_step_db: float = EIRP_STEP_DB
    max_iterations: int = 10_000


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    r_earth_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * r_earth_km * math.asin(min(1.0, math.sqrt(a)))


# ESC neighborhood (km) — WINNF-TS-0112 / interference.py Cat A/B.
_ESC_NEIGHBORHOOD_KM_A = 40.0
_ESC_NEIGHBORHOOD_KM_B = 80.0


def esc_neighborhood_km_for_category(category: str | None) -> float:
    """ESC IAP neighborhood by frozen category: A→40, B→80; else→80 (conservative)."""
    if category is None:
        return _ESC_NEIGHBORHOOD_KM_B
    cat = str(category).strip().upper()
    if cat == "A":
        return _ESC_NEIGHBORHOOD_KM_A
    if cat == "B":
        return _ESC_NEIGHBORHOOD_KM_B
    return _ESC_NEIGHBORHOOD_KM_B


def grants_in_neighborhood(
    point: ProtectionPoint, grants: list[GrantRfInfo]
) -> list[GrantRfInfo]:
    """Keep grants overlapping the point band and inside neighborhood (if set).

    ESC uses per-grant Cat A/B distances (40/80 km) from frozen ``cbsd_category``.
    Other entity kinds use ``point.neighborhood_km`` when set.
    """
    out: list[GrantRfInfo] = []
    for g in grants:
        if not (g.low_hz < point.high_hz and g.high_hz > point.low_hz):
            continue
        if point.entity_kind is ProtectedEntityKind.ESC:
            radius: float | None = esc_neighborhood_km_for_category(g.cbsd_category)
        else:
            radius = point.neighborhood_km
        if radius is not None:
            dist = _haversine_km(
                g.latitude, g.longitude, point.latitude, point.longitude
            )
            if dist > float(radius):
                continue
        out.append(g)
    return out


def run_iap_for_point(
    point: ProtectionPoint,
    grants: list[GrantRfInfo],
    *,
    coupling: InterferenceCoupling,
    config: IapEngineConfig | None = None,
) -> IapPointResult:
    """Run IAP at one protection point; return aggregates + per-grant decisions."""
    cfg = config or IapEngineConfig()
    channels = overlapping_iap_channels(point.low_hz, point.high_hz)
    neighbors = grants_in_neighborhood(point, grants)
    if not channels or not neighbors:
        early = tuple(
            IapGrantDecision(
                grant_id=g.grant_id,
                cbsd_id=g.cbsd_id,
                grant_pk=g.grant_pk,
                action="keep",
                authorized_eirp_dbm_mhz=g.max_eirp_dbm_mhz,
                initial_eirp_dbm_mhz=g.max_eirp_dbm_mhz,
                explanation=f"no overlapping IAP channels for point {point.point_id}",
            )
            for g in grants
            if g.is_managing_sas
        )
        return IapPointResult(
            point=point, channels=tuple(channels), aggregates=(), decisions=early
        )

    threshold_dbm = apply_pre_iap_margin_db(point.threshold_dbm, point.pre_iap_margin_db)
    threshold_mw = dbm_to_mw(threshold_dbm)

    n_grants = len(neighbors)
    eirp = [float(g.max_eirp_dbm_mhz) for g in neighbors]
    satisfied = [False] * n_grants
    removed = [False] * n_grants
    hit_floor = [False] * n_grants
    locked_interf: dict[tuple[int, int], float] = {}

    iap_threshold_ch = [threshold_mw] * len(channels)
    unsat_count_ch: list[int] = []
    fairshare_ch: list[float] = []
    for channel in channels:
        n_ch = sum(1 for g in neighbors if grant_overlaps_channel(g, channel, entity_kind=point.entity_kind))
        unsat_count_ch.append(n_ch)
        fairshare_ch.append((threshold_mw / float(n_ch)) if n_ch > 0 else 0.0)

    num_unsatisfied = n_grants
    iterations = 0
    while num_unsatisfied > 0 and iterations < cfg.max_iterations:
        iterations += 1

        for g_idx, grant in enumerate(neighbors):
            if removed[g_idx] or satisfied[g_idx]:
                continue
            grant_ok = True
            saw_overlap = False
            for ch_idx, channel in enumerate(channels):
                if not grant_overlaps_channel(grant, channel, entity_kind=point.entity_kind):
                    continue
                saw_overlap = True
                interf = float(coupling(grant, point, channel, eirp[g_idx]))
                if interf < fairshare_ch[ch_idx]:
                    locked_interf[(g_idx, ch_idx)] = interf
                else:
                    grant_ok = False
                    break
            satisfied[g_idx] = bool(grant_ok and saw_overlap)

        newly_locked = 0
        for g_idx, grant in enumerate(neighbors):
            if removed[g_idx] or not satisfied[g_idx]:
                continue
            if num_unsatisfied > 0:
                num_unsatisfied -= 1
            for ch_idx, channel in enumerate(channels):
                if not grant_overlaps_channel(grant, channel, entity_kind=point.entity_kind):
                    continue
                locked = locked_interf.get((g_idx, ch_idx))
                if locked is None:
                    locked = float(coupling(grant, point, channel, eirp[g_idx]))
                    locked_interf[(g_idx, ch_idx)] = locked
                iap_threshold_ch[ch_idx] = iap_threshold_ch[ch_idx] - locked
                unsat_count_ch[ch_idx] = max(0, unsat_count_ch[ch_idx] - 1)
                if unsat_count_ch[ch_idx] > 0:
                    fairshare_ch[ch_idx] = iap_threshold_ch[ch_idx] / float(
                        unsat_count_ch[ch_idx]
                    )
                else:
                    fairshare_ch[ch_idx] = 0.0
            removed[g_idx] = True
            newly_locked += 1

        if newly_locked > 0:
            continue

        for g_idx in range(n_grants):
            if removed[g_idx] or satisfied[g_idx]:
                continue
            eirp[g_idx] -= cfg.eirp_step_db
            if eirp[g_idx] < cfg.eirp_floor_dbm_mhz:
                hit_floor[g_idx] = True
                removed[g_idx] = True
                satisfied[g_idx] = True
                if num_unsatisfied > 0:
                    num_unsatisfied -= 1
                for ch_idx, channel in enumerate(channels):
                    if not grant_overlaps_channel(neighbors[g_idx], channel, entity_kind=point.entity_kind):
                        continue
                    unsat_count_ch[ch_idx] = max(0, unsat_count_ch[ch_idx] - 1)
                    if unsat_count_ch[ch_idx] > 0:
                        fairshare_ch[ch_idx] = iap_threshold_ch[ch_idx] / float(
                            unsat_count_ch[ch_idx]
                        )
                    else:
                        fairshare_ch[ch_idx] = 0.0

    aggregates: list[ChannelAggregateResult] = []
    for ch_idx, channel in enumerate(channels):
        contribs: list[GrantChannelContribution] = []
        managing_mw = 0.0
        for g_idx, g in enumerate(neighbors):
            if not grant_overlaps_channel(g, channel, entity_kind=point.entity_kind):
                continue
            if removed[g_idx] and (g_idx, ch_idx) not in locked_interf:
                continue
            if (g_idx, ch_idx) in locked_interf:
                mw = locked_interf[(g_idx, ch_idx)]
                eirp_val = eirp[g_idx]
            else:
                eirp_val = eirp[g_idx]
                mw = float(coupling(g, point, channel, eirp_val))
            contribs.append(
                GrantChannelContribution(
                    grant_id=g.grant_id,
                    channel=channel,
                    interference_mw=mw,
                    eirp_dbm_mhz=eirp_val,
                )
            )
            if g.is_managing_sas:
                managing_mw += mw
        agg_mw = sum_interference_mw([c.interference_mw for c in contribs])
        n = max(1, len(contribs))
        aggregates.append(
            ChannelAggregateResult(
                channel=channel,
                aggregate_mw=agg_mw,
                managing_sas_mw=managing_mw,
                threshold_mw=threshold_mw,
                fairshare_mw=threshold_mw / float(n),
                within_threshold=agg_mw <= threshold_mw + 1e-15,
            )
        )

    decisions: list[IapGrantDecision] = []
    for g_idx, g in enumerate(neighbors):
        if not g.is_managing_sas:
            continue
        initial = g.max_eirp_dbm_mhz
        final = eirp[g_idx]
        action: IapAction
        if hit_floor[g_idx] or (
            not any((g_idx, c) in locked_interf for c in range(len(channels)))
            and removed[g_idx]
            and final + 1e-9 < initial
        ):
            action = "terminate"
            explanation = (
                f"IAP {point.point_id}/{point.entity_kind.value}: "
                f"EIRP fell below floor {cfg.eirp_floor_dbm_mhz} dBm/MHz"
            )
            auth = cfg.eirp_floor_dbm_mhz
        elif final + 1e-9 < initial:
            action = "reduce_power"
            explanation = (
                f"IAP {point.point_id}/{point.entity_kind.value}: "
                f"EIRP {initial}→{final} dBm/MHz to meet fairshare"
            )
            auth = final
        else:
            action = "keep"
            explanation = (
                f"IAP {point.point_id}/{point.entity_kind.value}: "
                f"within fairshare at {final} dBm/MHz"
            )
            auth = final
        decisions.append(
            IapGrantDecision(
                grant_id=g.grant_id,
                cbsd_id=g.cbsd_id,
                grant_pk=g.grant_pk,
                action=action,
                authorized_eirp_dbm_mhz=auth,
                initial_eirp_dbm_mhz=initial,
                explanation=explanation,
            )
        )
    return IapPointResult(
        point=point,
        channels=tuple(channels),
        aggregates=tuple(aggregates),
        decisions=tuple(decisions),
    )


def merge_iap_decisions(point_results: list[IapPointResult]) -> tuple[IapGrantDecision, ...]:
    """Merge per-point decisions: terminate > suspend > reduce_power > keep; min EIRP."""
    rank = {"keep": 0, "reduce_power": 1, "suspend": 2, "terminate": 3}
    best: dict[str, IapGrantDecision] = {}
    for result in point_results:
        for decision in result.decisions:
            prev = best.get(decision.grant_id)
            if prev is None:
                best[decision.grant_id] = decision
                continue
            if rank[decision.action] > rank[prev.action]:
                best[decision.grant_id] = decision
            elif (
                decision.action == prev.action
                and decision.action == "reduce_power"
                and decision.authorized_eirp_dbm_mhz < prev.authorized_eirp_dbm_mhz
            ):
                best[decision.grant_id] = decision
            elif rank[decision.action] == rank[prev.action]:
                best[decision.grant_id] = IapGrantDecision(
                    grant_id=prev.grant_id,
                    cbsd_id=prev.cbsd_id,
                    grant_pk=prev.grant_pk,
                    action=prev.action,
                    authorized_eirp_dbm_mhz=min(
                        prev.authorized_eirp_dbm_mhz, decision.authorized_eirp_dbm_mhz
                    ),
                    initial_eirp_dbm_mhz=prev.initial_eirp_dbm_mhz,
                    explanation=f"{prev.explanation}; {decision.explanation}",
                )
    return tuple(best[k] for k in sorted(best))


def run_iap(
    points: list[ProtectionPoint],
    grants: list[GrantRfInfo],
    *,
    coupling: InterferenceCoupling,
    config: IapEngineConfig | None = None,
) -> IapRunResult:
    results = [
        run_iap_for_point(p, grants, coupling=coupling, config=config) for p in points
    ]
    return IapRunResult(
        points=tuple(results),
        merged_decisions=merge_iap_decisions(results),
    )
