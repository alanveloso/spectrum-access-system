"""Multi-constraint IAP + DPA protection orchestration ( MCP.1 / C2).

CPAS evaluates peer boolean rules first, then IAP (when applicable entities
exist), then DPA movelists with effective EIRPs.

Production path: ProtectionPoints are built from frozen snapshot
protection records; coupling is built via ``make_production_iap_coupling``.
Explicit test kwargs override production builders but share the same IAP engine.

Failure policy:
* No applicable IAP entities → IAP skipped (legitimate).
* Applicable entities + coupling unavailable → ``CpasRfEvaluationError``
 (fail-closed; never silent skip).
* ``sas_iap_enabled=false`` → IAP skipped only when the Profile does not
 require ``aggregate_linear_power``; otherwise fail closed
 (see ``runtime.toggle_reconciliation``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IapResolveResult:
    """Resolved IAP inputs for one CPAS evaluation."""

    points: tuple[Any, ...]
    coupling: Any | None
    source: str  # "none" | "production" | "override" | "disabled"


def resolve_iap_points(
    db: Session,
    iap_points: Sequence[Any] | None,
    *,
    build_from_db: bool = True,
    protection_records: tuple[tuple[str, str, str], ...] | None = None,
    peer_esc_records: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Return explicit points, or build from frozen/live entities when enabled."""
    if iap_points is not None:
        return list(iap_points)
    if not build_from_db:
        return []
    if protection_records is not None:
        from services.iap.protection_points import build_protection_points_from_frozen

        return list(
            build_protection_points_from_frozen(
                protection_records, peer_esc_records=peer_esc_records
            )
        )
    from services.iap.protection_points import build_protection_points_from_db

    return list(build_protection_points_from_db(db))


def resolve_iap_context(
    db: Session,
    *,
    protection_records: tuple[tuple[str, str, str], ...] = (),
    iap_points: Sequence[Any] | None = None,
    iap_coupling: Any | None = None,
    build_from_db: bool = True,
    coupling_factory: Any | None = None,
    peer_esc_records: list[dict[str, Any]] | None = None,
    rf_port: Any | None = None,
) -> IapResolveResult:
    """Resolve ProtectionPoints + coupling for production or test override.

 Precedence:
 1. Explicit ``iap_points`` / ``iap_coupling`` kwargs (test/override).
 2. Production builders from frozen ``protection_records`` (or live DB).
 3. If points exist and coupling still missing → raise
 ``IapCouplingUnavailable`` (unless IAP disabled).

 Coupling RF identity comes from ``rf_port`` / bound RuntimeComposition —
 not from ``SAS_IAP_PATH_LOSS_MODEL``.
 """
    from services.iap.coupling import (
        IapCouplingUnavailable,
        iap_enabled,
        make_production_iap_coupling,
    )
    from services.iap.protection_points import ProtectionEntityError
    from services.propagation.errors import PropagationUnavailableError

    if not iap_enabled():
        return IapResolveResult(points=(), coupling=None, source="disabled")

    try:
        points = resolve_iap_points(
            db,
            iap_points,
            build_from_db=build_from_db,
            protection_records=protection_records if iap_points is None else None,
            peer_esc_records=peer_esc_records if iap_points is None else None,
        )
    except ProtectionEntityError as exc:
        raise IapCouplingUnavailable(
            f"IAP protection entity invalid: {exc}"
        ) from exc
    if not points:
        return IapResolveResult(points=(), coupling=None, source="none")

    if iap_coupling is not None:
        return IapResolveResult(
            points=tuple(points), coupling=iap_coupling, source="override"
        )

    if coupling_factory is not None:
        factory = coupling_factory
    else:

        def factory() -> Any:
            return make_production_iap_coupling(rf_port=rf_port)

    try:
        coupling = factory()
    except PropagationUnavailableError as exc:
        raise IapCouplingUnavailable(
            f"IAP protection entities present but coupling unavailable: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface as domain RF failure
        raise IapCouplingUnavailable(
            f"IAP protection entities present but coupling failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if coupling is None:
        raise IapCouplingUnavailable(
            "IAP protection entities present but coupling provider returned None"
        )
    return IapResolveResult(
        points=tuple(points), coupling=coupling, source="production"
    )


def effective_eirp_by_grant_id(
    decisions: Sequence[Any],
) -> dict[str, float]:
    """Map grant_id → authorized EIRP for IAP reduce_power decisions."""
    out: dict[str, float] = {}
    for d in decisions:
        action = getattr(d, "action", None)
        if action != "reduce_power":
            continue
        eirp = getattr(d, "authorized_eirp_dbm_mhz", None)
        gid = getattr(d, "grant_id", None)
        if gid is None or eirp is None:
            continue
        out[str(gid)] = float(eirp)
    return out


def iap_terminated_grant_ids(decisions: Sequence[Any]) -> set[str]:
    return {
        str(d.grant_id)
        for d in decisions
        if getattr(d, "action", None) == "terminate"
        and getattr(d, "reason", None) == "iap"
        and getattr(d, "grant_id", None) is not None
    }


def merge_constraint_decisions(
    peer: Sequence[Any],
    iap: Sequence[Any],
    dpa: Sequence[Any],
) -> list[Any]:
    """Merge peer / IAP / DPA decisions with terminate taking precedence per grant."""
    by_pk: dict[int, Any] = {}
    order: list[int] = []

    def _consider(decision: Any) -> None:
        pk = getattr(decision, "grant_pk", None)
        if pk is None:
            return
        pk_i = int(pk)
        existing = by_pk.get(pk_i)
        if existing is None:
            by_pk[pk_i] = decision
            order.append(pk_i)
            return
        if getattr(decision, "action", None) == "terminate":
            by_pk[pk_i] = decision
        elif (
            getattr(existing, "action", None) != "terminate"
            and getattr(decision, "action", None) == "reduce_power"
        ):
            by_pk[pk_i] = decision

    for group in (peer, iap, dpa):
        for d in group:
            _consider(d)
    return [by_pk[pk] for pk in order]
