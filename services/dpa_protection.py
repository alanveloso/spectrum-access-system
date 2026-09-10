"""Rel1Ext DPA interference protection.

Determines which DPA channels require protection, evaluates aggregate
interference at protection points using Rel1Ext Type-3 path loss
(ITM median + P.2108 clutter for Tx AGL ≤ 6 m + 8 dB network loading),
and builds move-lists consumed by grant/heartbeat enforcement.

Path-loss policy:
* Default / Rel1Ext IPR requires an explicit ITM median (or injectable ITM
 backend). Free Space is **never** substituted silently for ITM.
* Free Space is allowed only when ``DpaPathLossModel.FREE_SPACE`` is selected
 (local/test profile).

Move-list policy:
* ``build_movelist`` is a local deterministic greedy algorithm. It is **not**
 evidence of the official harness Monte-Carlo / keep-move procedure.

No harness fixture DPA IDs or coordinates are hard-coded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy.orm import Session

from models.models import Cbsd, Grant
from primitives.power import mw_to_dbm
from primitives.rf_arithmetic import (
    received_power_dbm,
    received_power_mw,
    sum_linear_mw,
    within_threshold_dbm,
)
from services.geometry import within_geojson_buffer_m
from services.propagation.errors import PropagationUnavailableError
from services.propagation.rel1ext_dpa import (
    ACTIVITY_LOSS_FACTOR_DB,
    calc_p2108_clutter_db,
    compose_dpa_pathloss_db,
)

# Default margin Δ (dB) applied as I_agg ≤ TH + Δ (IPR.8 wording uses TH+Δ).
DEFAULT_DPA_MARGIN_DB = 0.0
DEFAULT_REF_HEIGHT_M = 50.0
DEFAULT_THRESHOLD_DBM_PER_10MHZ = -144.0
FREQ_MHZ_DEFAULT = 3625.0


class ProtectionReason(str, Enum):
    ACTIVE = "active"
    ESC_DISCONNECTED = "esc_disconnected"
    ESC_ABSENT = "esc_absent"
    ALWAYS_ON = "always_on"


class DpaPathLossModel(str, Enum):
    """Explicit path-loss profile for DPA aggregate / movelist."""

    ITM_REL1EXT = "itm_rel1ext"  # required for Rel1Ext IPR (default)
    FREE_SPACE = "free_space"  # local/test only — never a silent ITM substitute


class DpaPathLossUnavailable(PropagationUnavailableError):
    """ITM/NED/required RF backend missing for a Rel1Ext DPA evaluation."""


@dataclass(frozen=True)
class ProtectedDpaChannel:
    dpa_id: str
    low_hz: int
    high_hz: int
    reason: ProtectionReason
    geometry: dict[str, Any] | None
    neighborhood_km: dict[str, float]
    protection_params: dict[str, Any]
    threshold_dbm_per_10mhz: float
    ref_height_m: float


@dataclass(frozen=True)
class DpaGrantRf:
    grant_id: str
    cbsd_id: str
    latitude: float
    longitude: float
    height_m: float
    height_is_agl: bool
    indoor: bool
    low_hz: int
    high_hz: int
    max_eirp_dbm_mhz: float
    # False for peer FAD contributions (aggregate only; never local mutate).
    is_managing_sas: bool = True
    cbsd_category: str = "A"

@dataclass(frozen=True)
class PointInterference:
    grant_id: str
    interference_dbm: float
    interference_mw: float
    path_loss_db: float


PathLossFn = Callable[
    [DpaGrantRf, float, float, float],
    float,
]
ItmMedianFn = Callable[
    [DpaGrantRf, float, float, float],
    float,
]


def polygon_representative_point(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    """Return a representative (lat, lon) from a GeoJSON Point / Polygon / MultiPolygon."""
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            return None
        return (lat, lon)
    ring: Sequence[Any] | None = None
    if gtype == "Polygon" and isinstance(coords, list) and coords:
        ring = coords[0]
    elif gtype == "MultiPolygon" and isinstance(coords, list) and coords:
        first = coords[0]
        if isinstance(first, list) and first:
            ring = first[0]
    if not isinstance(ring, list) or len(ring) < 3:
        return None
    lons: list[float] = []
    lats: list[float] = []
    for pt in ring[:-1] if ring[0] == ring[-1] else ring:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        try:
            lons.append(float(pt[0]))
            lats.append(float(pt[1]))
        except (TypeError, ValueError):
            continue
    if not lons:
        return None
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def free_space_path_loss_db(
    lat_tx: float,
    lon_tx: float,
    height_tx_m: float,
    lat_rx: float,
    lon_rx: float,
    height_rx_m: float,
    *,
    freq_mhz: float = FREQ_MHZ_DEFAULT,
) -> float:
    """Free-space path loss (dB). Only for ``DpaPathLossModel.FREE_SPACE`` profiles."""
    from services.terrain.vincenty import geodesic_distance_km

    dist_km = max(geodesic_distance_km(lat_tx, lon_tx, lat_rx, lon_rx), 1e-6)
    _ = height_tx_m, height_rx_m
    return float(20.0 * math.log10(dist_km) + 20.0 * math.log10(freq_mhz) + 32.44)


def rel1ext_dpa_path_loss_db(
    grant: DpaGrantRf,
    lat_rx: float,
    lon_rx: float,
    height_rx_m: float,
    *,
    median_path_loss_db: float | None = None,
    model: DpaPathLossModel = DpaPathLossModel.ITM_REL1EXT,
    terrain_elevation_m: Callable[[float, float], float] | None = None,
) -> float:
    """Compose Rel1Ext Type-3 path loss toward a DPA point.

 For ``ITM_REL1EXT``, ``median_path_loss_db`` **must** be the ITM median (or an
 injectable ITM result). Free Space is never used as a silent substitute.
 For ``FREE_SPACE``, the median is Free Space (explicit lab/test profile).
 """
    if not grant.height_is_agl and terrain_elevation_m is None:
        raise DpaPathLossUnavailable(
            "terrain elevation backend required for AMSL Rel1Ext DPA path loss"
        )

    if model is DpaPathLossModel.FREE_SPACE:
        if median_path_loss_db is None:
            height_agl = grant.height_m
            if not grant.height_is_agl:
                assert terrain_elevation_m is not None
                height_agl = grant.height_m - float(
                    terrain_elevation_m(grant.latitude, grant.longitude)
                )
            median_path_loss_db = free_space_path_loss_db(
                grant.latitude,
                grant.longitude,
                height_agl,
                lat_rx,
                lon_rx,
                height_rx_m,
            )
    elif median_path_loss_db is None:
        raise DpaPathLossUnavailable(
            "Rel1Ext DPA path loss requires ITM median; Free Space is not a "
            "silent substitute (select DpaPathLossModel.FREE_SPACE explicitly "
            "for local/test profiles only)"
        )

    clutter = calc_p2108_clutter_db(
        grant.latitude,
        grant.longitude,
        grant.height_m,
        lat_rx,
        lon_rx,
        is_height_cbsd_amsl=not grant.height_is_agl,
        terrain_elevation_m=terrain_elevation_m,
    )
    return float(
        compose_dpa_pathloss_db(
            float(median_path_loss_db),
            float(clutter),
            activity_loss_db=ACTIVITY_LOSS_FACTOR_DB,
        )
    )


def _wrap_terrain_elevation_backend(
    terrain_elevation_m: Callable[[float, float], float] | None,
) -> Callable[[float, float], float] | None:
    """Convert terrain/NED backend failures into DpaPathLossUnavailable."""
    if terrain_elevation_m is None:
        return None

    def _wrapped(lat: float, lon: float) -> float:
        try:
            return float(terrain_elevation_m(lat, lon))
        except DpaPathLossUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — numerical / IO terrain backends
            raise DpaPathLossUnavailable(
                f"terrain elevation backend failed: {exc}"
            ) from exc

    return _wrapped


def make_path_loss_fn(
    *,
    model: DpaPathLossModel = DpaPathLossModel.ITM_REL1EXT,
    itm_median_fn: ItmMedianFn | None = None,
    terrain_elevation_m: Callable[[float, float], float] | None = None,
) -> PathLossFn:
    """Build a path-loss callable for the selected model."""
    terrain_elevation_m = _wrap_terrain_elevation_backend(terrain_elevation_m)

    def _fn(
        grant: DpaGrantRf, lat_rx: float, lon_rx: float, height_rx_m: float
    ) -> float:
        if model is DpaPathLossModel.FREE_SPACE:
            return rel1ext_dpa_path_loss_db(
                grant,
                lat_rx,
                lon_rx,
                height_rx_m,
                model=DpaPathLossModel.FREE_SPACE,
                terrain_elevation_m=terrain_elevation_m,
            )
        if itm_median_fn is None:
            raise DpaPathLossUnavailable(
                "ITM median backend unavailable for Rel1Ext DPA path loss"
            )
        median = float(itm_median_fn(grant, lat_rx, lon_rx, height_rx_m))
        return rel1ext_dpa_path_loss_db(
            grant,
            lat_rx,
            lon_rx,
            height_rx_m,
            median_path_loss_db=median,
            model=DpaPathLossModel.ITM_REL1EXT,
            terrain_elevation_m=terrain_elevation_m,
        )

    return _fn


def load_rel1ext_backends() -> tuple[
    ItmMedianFn, Callable[[float, float], float]
]:
    """Load production Rel1Ext ITM median and terrain elevation backends."""
    from services.propagation import load_reference_engines

    engines = load_reference_engines()

    def _itm(
        grant: DpaGrantRf, lat_rx: float, lon_rx: float, height_rx_m: float
    ) -> float:
        try:
            result = engines.calc_itm(
                grant.latitude,
                grant.longitude,
                grant.height_m,
                lat_rx,
                lon_rx,
                height_rx_m,
                cbsd_indoor=grant.indoor,
                reliability=0.5,
                freq_mhz=FREQ_MHZ_DEFAULT,
                is_height_cbsd_amsl=not grant.height_is_agl,
            )
        except Exception as exc:  # noqa: BLE001 — numerical / IO backends
            raise DpaPathLossUnavailable(f"ITM median failed: {exc}") from exc
        return float(result.db_loss)

    raw_terrain = engines.terrain_elevation_m

    def _terrain(lat: float, lon: float) -> float:
        try:
            return float(raw_terrain(lat, lon))
        except Exception as exc:  # noqa: BLE001 — numerical / IO terrain backends
            raise DpaPathLossUnavailable(
                f"terrain elevation backend failed: {exc}"
            ) from exc

    return _itm, _terrain


def load_itm_median_fn() -> ItmMedianFn:
    """Load ITM median from reference engines; raise if ITM/NED unavailable."""
    itm, _ = load_rel1ext_backends()
    return itm


def default_rel1ext_path_loss_fn(
    *,
    itm_median_fn: ItmMedianFn | None = None,
    terrain_elevation_m: Callable[[float, float], float] | None = None,
) -> PathLossFn:
    """Production default: ITM Rel1Ext plus terrain elevation for AMSL."""
    if itm_median_fn is not None:
        return make_path_loss_fn(
            model=DpaPathLossModel.ITM_REL1EXT,
            itm_median_fn=itm_median_fn,
            terrain_elevation_m=terrain_elevation_m,
        )
    try:
        loaded_itm, loaded_terrain = load_rel1ext_backends()
    except PropagationUnavailableError as exc:
        message = str(exc)

        def _missing(
            grant: DpaGrantRf, lat_rx: float, lon_rx: float, height_rx_m: float
        ) -> float:
            raise DpaPathLossUnavailable(message)

        return _missing
    terrain = (
        terrain_elevation_m if terrain_elevation_m is not None else loaded_terrain
    )
    return make_path_loss_fn(
        model=DpaPathLossModel.ITM_REL1EXT,
        itm_median_fn=loaded_itm,
        terrain_elevation_m=terrain,
    )


def cochannel_eirp_dbm(grant: DpaGrantRf, low_hz: int, high_hz: int) -> float:
    """Conducted+antenna-simplified EIRP in the overlapping co-channel bandwidth."""
    overlap_hz = max(0, min(grant.high_hz, high_hz) - max(grant.low_hz, low_hz))
    if overlap_hz <= 0:
        return float("-inf")
    return float(grant.max_eirp_dbm_mhz + 10.0 * math.log10(overlap_hz / 1.0e6))


def interference_dbm_at_point(
    grant: DpaGrantRf,
    *,
    lat_rx: float,
    lon_rx: float,
    height_rx_m: float,
    low_hz: int,
    high_hz: int,
    path_loss_db: float | None = None,
    path_loss_fn: PathLossFn | None = None,
) -> PointInterference | None:
    if grant.low_hz >= high_hz or grant.high_hz <= low_hz:
        return None
    if path_loss_db is None:
        fn = path_loss_fn if path_loss_fn is not None else default_rel1ext_path_loss_fn()
        path_loss_db = float(fn(grant, lat_rx, lon_rx, height_rx_m))
    eirp = cochannel_eirp_dbm(grant, low_hz, high_hz)
    if eirp == float("-inf"):
        return None
    inter_dbm = received_power_dbm(eirp, path_loss_db)
    return PointInterference(
        grant_id=grant.grant_id,
        interference_dbm=inter_dbm,
        interference_mw=received_power_mw(eirp, path_loss_db),
        path_loss_db=float(path_loss_db),
    )


def aggregate_within_threshold(
    contributions: Sequence[PointInterference],
    *,
    threshold_dbm: float,
    margin_db: float = DEFAULT_DPA_MARGIN_DB,
) -> tuple[float, bool]:
    """Return (I_agg_dbm, ok) for I_agg ≤ TH + Δ."""
    total_mw = sum_linear_mw(c.interference_mw for c in contributions)
    agg_dbm = mw_to_dbm(total_mw)
    return agg_dbm, within_threshold_dbm(
        agg_dbm, threshold_dbm, margin_db, abs_tol=1e-12
    )


def build_movelist(
    contributions: Sequence[PointInterference],
    *,
    threshold_dbm: float,
    margin_db: float = DEFAULT_DPA_MARGIN_DB,
) -> list[str]:
    """Local deterministic greedy move-list (not official harness Monte-Carlo).

 Drops highest interferers until ``I_agg ≤ TH + Δ``. This is a local
 implementation aid for IPR wiring — not proof of official move-list
 algorithm or pass-rate tolerances.
 """
    remaining = sorted(contributions, key=lambda c: c.interference_mw, reverse=True)
    moved: list[str] = []
    while remaining:
        _, ok = aggregate_within_threshold(
            remaining, threshold_dbm=threshold_dbm, margin_db=margin_db
        )
        if ok:
            break
        victim = remaining.pop(0)
        moved.append(victim.grant_id)
    return moved


def _threshold_from_params(params: dict[str, Any]) -> float:
    raw = params.get("protectionCritDbmPer10MHz", DEFAULT_THRESHOLD_DBM_PER_10MHZ)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_DBM_PER_10MHZ


def _ref_height_from_params(params: dict[str, Any]) -> float:
    raw = params.get("refHeightMeters", DEFAULT_REF_HEIGHT_M)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REF_HEIGHT_M


def list_protected_dpa_channels(db: Session) -> list[ProtectedDpaChannel]:
    """DPA channels that must be protected under Rel1Ext IPR rules."""
    from services.dpa_service import list_active_activations, list_catalogue
    from services.esc_admin_service import is_esc_absent, is_esc_disconnected

    catalogue = [c for c in list_catalogue(db) if isinstance(c, dict)]
    by_id = {
        str(c.get("dpaId")): c for c in catalogue if isinstance(c.get("dpaId"), str)
    }
    out: dict[tuple[str, int, int], ProtectedDpaChannel] = {}

    def _add(
        item: dict[str, Any],
        *,
        low_hz: int,
        high_hz: int,
        reason: ProtectionReason,
    ) -> None:
        dpa_id = str(item.get("dpaId") or "").strip()
        if not dpa_id:
            return
        key = (dpa_id, low_hz, high_hz)
        if key in out:
            return
        params = item.get("protectionParams") or {}
        if not isinstance(params, dict):
            params = {}
        nb = item.get("neighborhoodKm") or {}
        if not isinstance(nb, dict):
            nb = {}
        nb_clean: dict[str, float] = {}
        for k, v in nb.items():
            try:
                nb_clean[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        geom = item.get("geometry") if isinstance(item.get("geometry"), dict) else None
        out[key] = ProtectedDpaChannel(
            dpa_id=dpa_id,
            low_hz=low_hz,
            high_hz=high_hz,
            reason=reason,
            geometry=geom,
            neighborhood_km=nb_clean,
            protection_params=dict(params),
            threshold_dbm_per_10mhz=_threshold_from_params(params),
            ref_height_m=_ref_height_from_params(params),
        )

    # Active activations (IPR.2/3/4/5/7/8).
    for act in list_active_activations(db):
        dpa_id = str(act.get("dpaId") or "").strip()
        item = by_id.get(dpa_id)
        if item is None:
            continue
        fr = act.get("frequencyRange") or {}
        try:
            low = int(fr["lowFrequency"])
            high = int(fr["highFrequency"])
        except (KeyError, TypeError, ValueError):
            continue
        _add(item, low_hz=low, high_hz=high, reason=ProtectionReason.ACTIVE)

    esc_fail_closed = is_esc_disconnected(db) or is_esc_absent(db)
    for item in catalogue:
        esc_monitored = bool(item.get("escMonitored", True))
        channels = item.get("channels") or []
        if not isinstance(channels, list):
            continue
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            try:
                low = int(ch["lowFrequency"])
                high = int(ch["highFrequency"])
            except (KeyError, TypeError, ValueError):
                continue
            if not esc_monitored:
                # Inland / always-on (IPR.4 class).
                _add(item, low_hz=low, high_hz=high, reason=ProtectionReason.ALWAYS_ON)
            elif esc_fail_closed:
                reason = (
                    ProtectionReason.ESC_ABSENT
                    if is_esc_absent(db)
                    else ProtectionReason.ESC_DISCONNECTED
                )
                _add(item, low_hz=low, high_hz=high, reason=reason)

    return sorted(out.values(), key=lambda p: (p.dpa_id, p.low_hz, p.high_hz))


def grant_frequency_overlaps_protected(
    protected: Sequence[ProtectedDpaChannel], low_hz: int, high_hz: int
) -> list[ProtectedDpaChannel]:
    return [p for p in protected if low_hz < p.high_hz and high_hz > p.low_hz]


def cbsd_inside_protected_neighborhood(
    cbsd: Cbsd,
    protected: ProtectedDpaChannel,
    *,
    terrain: Any | None = None,
) -> bool | None:
    """True/False when evaluable; None when height/geometry is indeterminate."""
    from services.dpa_neighborhood import (
        neighborhood_radius_km_for_cbsd,
        resolve_height_agl_m,
        _installation,
    )

    if protected.geometry is None:
        return None
    inst = _installation(cbsd)
    try:
        lat = float(inst["latitude"])
        lon = float(inst["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        height_agl = resolve_height_agl_m(
            lat,
            lon,
            float(inst.get("height", 0.0)),
            str(inst.get("heightType") or "AGL"),
            terrain=terrain,
        )
    except Exception:  # noqa: BLE001 — terrain / validation → indeterminate
        return None
    indoor = bool(inst.get("indoorDeployment", False))
    category = str(inst.get("cbsdCategory") or "A").upper()
    # Prefer registration category when present on the CBSD row.
    try:
        import json

        reg = json.loads(cbsd.registration_json or "{}")
        if isinstance(reg, dict) and reg.get("cbsdCategory"):
            category = str(reg["cbsdCategory"]).upper()
        elif cbsd.cbsd_category:
            category = str(cbsd.cbsd_category).upper()
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    if category not in {"A", "B"}:
        category = "A"
    radius_km = neighborhood_radius_km_for_cbsd(
        protected.neighborhood_km,
        category=category,
        indoor=indoor,
        height_agl_m=height_agl,
    )
    buffer_m = 0.0 if radius_km is None else radius_km * 1000.0
    return bool(within_geojson_buffer_m(lat, lon, protected.geometry, buffer_m))


def dpa_grant_rf_from_cbsd_grant(cbsd: Cbsd, grant: Grant) -> DpaGrantRf | None:
    from services.dpa_neighborhood import _installation

    inst = _installation(cbsd)
    try:
        lat = float(inst["latitude"])
        lon = float(inst["longitude"])
        height = float(inst.get("height", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    height_type = str(inst.get("heightType") or "AGL").upper()
    category = "A"
    try:
        import json

        reg = json.loads(cbsd.registration_json or "{}")
        if isinstance(reg, dict) and reg.get("cbsdCategory"):
            category = str(reg["cbsdCategory"]).upper()
        elif cbsd.cbsd_category:
            category = str(cbsd.cbsd_category).upper()
    except (json.JSONDecodeError, TypeError, ValueError):
        if cbsd.cbsd_category:
            category = str(cbsd.cbsd_category).upper()
    if category not in {"A", "B"}:
        category = "A"
    return DpaGrantRf(
        grant_id=grant.grant_id,
        cbsd_id=cbsd.cbsd_id,
        latitude=lat,
        longitude=lon,
        height_m=height,
        height_is_agl=(height_type != "AMSL"),
        indoor=bool(inst.get("indoorDeployment", False)),
        low_hz=int(grant.low_frequency),
        high_hz=int(grant.high_frequency),
        max_eirp_dbm_mhz=float(grant.max_eirp if grant.max_eirp is not None else 0.0),
        cbsd_category=category,
    )


def evaluate_protected_channel(
    protected: ProtectedDpaChannel,
    grants: Sequence[DpaGrantRf],
    *,
    path_loss_fn: PathLossFn | None = None,
    margin_db: float = DEFAULT_DPA_MARGIN_DB,
) -> tuple[list[PointInterference], list[str], float, bool]:
    """Return contributions, movelist, I_agg_dbm, within_threshold.

    When ITM/required datasets are unavailable under the default Rel1Ext model,
    returns fail-closed (over threshold; all overlapping grants on movelist).
    """
    overlapping = [
        g
        for g in grants
        if g.low_hz < protected.high_hz and g.high_hz > protected.low_hz
    ]
    point = polygon_representative_point(protected.geometry)
    if point is None:
        # Fail-closed: cannot evaluate → treat as over threshold; move all overlap.
        return [], [g.grant_id for g in overlapping], float("inf"), False
    lat_rx, lon_rx = point
    fn = path_loss_fn if path_loss_fn is not None else default_rel1ext_path_loss_fn()
    contribs: list[PointInterference] = []
    try:
        for grant in overlapping:
            hit = interference_dbm_at_point(
                grant,
                lat_rx=lat_rx,
                lon_rx=lon_rx,
                height_rx_m=protected.ref_height_m,
                low_hz=protected.low_hz,
                high_hz=protected.high_hz,
                path_loss_fn=fn,
            )
            if hit is not None:
                contribs.append(hit)
    except (DpaPathLossUnavailable, PropagationUnavailableError):
        return [], [g.grant_id for g in overlapping], float("inf"), False
    agg_dbm, ok = aggregate_within_threshold(
        contribs,
        threshold_dbm=protected.threshold_dbm_per_10mhz,
        margin_db=margin_db,
    )
    moved = build_movelist(
        contribs,
        threshold_dbm=protected.threshold_dbm_per_10mhz,
        margin_db=margin_db,
    )
    return contribs, moved, agg_dbm, ok


def collect_active_dpa_grants(
    db: Session,
    *,
    grant_pks: Sequence[int] | None = None,
    eirp_by_grant_id: Mapping[str, float] | None = None,
    exclude_grant_ids: set[str] | None = None,
) -> list[DpaGrantRf]:
    """Collect local managing-SAS grants for DPA evaluation.

 When ``grant_pks`` is provided (CPAS/MCP frozen snapshot), only those PKs
 are loaded — mid-run inserts are invisible. When ``grant_pks`` is ``None``
 (activate/grant path), scan live active grants.
 """
    from models.models import Cbsd as CbsdModel

    exclude = exclude_grant_ids or set()
    frozen_pks: Sequence[int] | None = grant_pks
    if frozen_pks is not None and not frozen_pks:
        return []
    query = db.query(Grant)
    if frozen_pks is not None:
        query = query.filter(Grant.id.in_(list(frozen_pks)))
    out: list[DpaGrantRf] = []
    for grant in query.order_by(Grant.id).all():
        if frozen_pks is None:
            if bool(getattr(grant, "terminated", False)):
                continue
            life = str(getattr(grant, "lifecycle_state", "") or "").upper()
            if life in {"RELINQUISHED", "EXPIRED"}:
                continue
        if grant.grant_id in exclude:
            continue
        cbsd = db.query(CbsdModel).filter_by(cbsd_id=grant.cbsd_id).first()
        if not cbsd:
            continue
        rf = dpa_grant_rf_from_cbsd_grant(cbsd, grant)
        if rf is None:
            continue
        if eirp_by_grant_id and grant.grant_id in eirp_by_grant_id:
            from dataclasses import replace

            rf = replace(
                rf, max_eirp_dbm_mhz=float(eirp_by_grant_id[grant.grant_id])
            )
        out.append(rf)
    return out


def dpa_grants_from_frozen_peer_cbsds(
    peer_cbsd_rows: Sequence[tuple[int, dict[str, Any]]],
) -> list[DpaGrantRf]:
    """Peer FAD grants as DPA RF contributors (frozen generation; not mutable)."""
    from services.iap.peer_fad import grant_rf_infos_from_frozen_peer_cbsds

    out: list[DpaGrantRf] = []
    for info in grant_rf_infos_from_frozen_peer_cbsds(list(peer_cbsd_rows)):
        out.append(
            DpaGrantRf(
                grant_id=info.grant_id,
                cbsd_id=info.cbsd_id,
                latitude=info.latitude,
                longitude=info.longitude,
                height_m=info.height_m,
                height_is_agl=info.height_is_agl,
                indoor=info.indoor,
                low_hz=info.low_hz,
                high_hz=info.high_hz,
                max_eirp_dbm_mhz=info.max_eirp_dbm_mhz,
                is_managing_sas=False,
                cbsd_category="A",
            )
        )
    return out


def rf_inside_protected_neighborhood(
    grant: DpaGrantRf,
    protected: ProtectedDpaChannel,
    *,
    category: str | None = None,
) -> bool | None:
    """Neighborhood test from RF coordinates (peers / frozen RF without CBSD row)."""
    from services.dpa_neighborhood import neighborhood_radius_km_for_cbsd

    if protected.geometry is None:
        return None
    raw_cat = category if category is not None else grant.cbsd_category
    cat = str(raw_cat or "A").upper()
    if cat not in {"A", "B"}:
        cat = "A"
    height_agl = float(grant.height_m)
    radius_km = neighborhood_radius_km_for_cbsd(
        protected.neighborhood_km,
        category=cat,
        indoor=bool(grant.indoor),
        height_agl_m=height_agl,
    )
    buffer_m = 0.0 if radius_km is None else radius_km * 1000.0
    return bool(
        within_geojson_buffer_m(
            grant.latitude, grant.longitude, protected.geometry, buffer_m
        )
    )


def filter_grants_in_neighborhood(
    db: Session,
    protected: ProtectedDpaChannel,
    grants: Sequence[DpaGrantRf],
) -> list[DpaGrantRf]:
    """Keep grants whose RF is INSIDE or INDETERMINATE for this DPA neighborhood.

 Uses frozen/collected ``DpaGrantRf`` fields only — does not re-read live
 ``registration_json`` (required for CPAS generation-N consistency).
 """
    del db  # RF path does not need live CBSD rows.
    out: list[DpaGrantRf] = []
    for grant in grants:
        inside = rf_inside_protected_neighborhood(grant, protected)
        if inside is False:
            continue
        # True or None (indeterminate / missing geometry) → include (fail-closed).
        out.append(grant)
    return out


def refresh_activation_movelists(
    db: Session,
    *,
    path_loss_fn: PathLossFn | None = None,
    grant_pks: Sequence[int] | None = None,
    local_grants: Sequence[DpaGrantRf] | None = None,
    peer_grants: Sequence[DpaGrantRf] | None = None,
    eirp_by_grant_id: Mapping[str, float] | None = None,
    exclude_grant_ids: set[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Recompute movelist on each active DPA activation row (neighborhood-scoped).

 CPAS/MCP should pass frozen ``local_grants`` (RF generation N). When
 ``local_grants`` is omitted, ``grant_pks`` / live collect is used.

 All channels are evaluated before any activation row is updated so a mid-loop
 RF failure does not leave a partial movelist generation committed as success.
 """
    from services.dpa_service import FrequencyRange, list_active_activations, _upsert_activation

    if local_grants is not None:
        grants = list(local_grants)
        if eirp_by_grant_id:
            from dataclasses import replace

            adjusted: list[DpaGrantRf] = []
            for g in grants:
                if g.grant_id in eirp_by_grant_id:
                    adjusted.append(
                        replace(
                            g,
                            max_eirp_dbm_mhz=float(eirp_by_grant_id[g.grant_id]),
                        )
                    )
                else:
                    adjusted.append(g)
            grants = adjusted
        if exclude_grant_ids:
            grants = [g for g in grants if g.grant_id not in exclude_grant_ids]
    else:
        grants = list(
            collect_active_dpa_grants(
                db,
                grant_pks=grant_pks,
                eirp_by_grant_id=eirp_by_grant_id,
                exclude_grant_ids=exclude_grant_ids,
            )
        )
    if peer_grants:
        grants.extend(peer_grants)
    protected = [
        p for p in list_protected_dpa_channels(db) if p.reason == ProtectionReason.ACTIVE
    ]
    by_key = {(p.dpa_id, p.low_hz, p.high_hz): p for p in protected}
    pending: list[tuple[str, Any, list[str]]] = []
    for act in list_active_activations(db):
        dpa_id = str(act.get("dpaId") or "").strip()
        fr = act.get("frequencyRange") or {}
        try:
            low = int(fr["lowFrequency"])
            high = int(fr["highFrequency"])
        except (KeyError, TypeError, ValueError):
            # Skip corrupt activation rows; do not treat as successful empty movelist.
            continue
        channel = by_key.get((dpa_id, low, high))
        if channel is None:
            continue
        neighborhood_grants = filter_grants_in_neighborhood(db, channel, grants)
        _, moved, _, _ = evaluate_protected_channel(
            channel, neighborhood_grants, path_loss_fn=path_loss_fn
        )
        managing_ids = {g.grant_id for g in neighborhood_grants if g.is_managing_sas}
        moved_local = [gid for gid in moved if gid in managing_ids]
        pending.append((dpa_id, FrequencyRange(low, high), moved_local))

    for dpa_id, freq, moved_local in pending:
        _upsert_activation(
            db,
            dpa_id=dpa_id,
            freq=freq,
            movelist=moved_local,
        )
    if commit:
        db.commit()
    return {"updated": len(pending)}


def grant_on_any_movelist(db: Session, grant_id: str) -> bool:
    from services.dpa_service import list_active_activations

    for act in list_active_activations(db):
        moved = act.get("movelist") or []
        if isinstance(moved, list) and grant_id in moved:
            return True
    return False


def proposed_grant_violates_dpa(
    db: Session,
    cbsd: Cbsd,
    *,
    low_hz: int,
    high_hz: int,
    max_eirp_dbm_mhz: float,
    path_loss_fn: PathLossFn | None = None,
) -> bool:
    """True when a proposed grant would leave Rel1Ext DPA aggregate over TH+Δ.

 Rejects whenever the neighborhood-scoped aggregate *including* the proposed
 grant is over threshold (even if existing grants already violate). Fail-closed
 on indeterminate neighborhood membership or ITM/backend unavailability.
 """
    from services.dpa_neighborhood import _installation

    protected = grant_frequency_overlaps_protected(
        list_protected_dpa_channels(db), low_hz, high_hz
    )
    if not protected:
        return False

    inst = _installation(cbsd)
    try:
        lat = float(inst["latitude"])
        lon = float(inst["longitude"])
        height = float(inst.get("height", 0.0))
    except (KeyError, TypeError, ValueError):
        return True
    height_type = str(inst.get("heightType") or "AGL").upper()
    proposed = DpaGrantRf(
        grant_id="__proposed__",
        cbsd_id=cbsd.cbsd_id,
        latitude=lat,
        longitude=lon,
        height_m=height,
        height_is_agl=(height_type != "AMSL"),
        indoor=bool(inst.get("indoorDeployment", False)),
        low_hz=low_hz,
        high_hz=high_hz,
        max_eirp_dbm_mhz=max_eirp_dbm_mhz,
    )
    existing = collect_active_dpa_grants(db)
    fn = path_loss_fn if path_loss_fn is not None else default_rel1ext_path_loss_fn()
    for channel in protected:
        inside = cbsd_inside_protected_neighborhood(cbsd, channel)
        if inside is False:
            continue
        if inside is None:
            return True
        neighborhood = filter_grants_in_neighborhood(db, channel, existing)
        # Drop any prior grant from same CBSD on overlapping freq (renewal-like).
        neighborhood = [
            g
            for g in neighborhood
            if not (
                g.cbsd_id == proposed.cbsd_id
                and g.low_hz < high_hz
                and g.high_hz > low_hz
            )
        ]
        try:
            _, _, _, ok = evaluate_protected_channel(
                channel,
                list(neighborhood) + [proposed],
                path_loss_fn=fn,
            )
        except (DpaPathLossUnavailable, PropagationUnavailableError):
            return True
        if not ok:
            return True
    return False
