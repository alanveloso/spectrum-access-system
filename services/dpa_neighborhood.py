"""DPA neighborhood membership and Rel1Ext transmitExpireTime caps.

WINNF-TS-4010 V1.1.0 §6.4.4.13 (REL1Ext-R1-IPM-02 / IPM-03):

* Grants used by CBSDs inside any DPA neighborhood whose frequency overlaps
 3550–3650 MHz must advertise ``transmitExpireTime`` ≤ 240 s ahead (and ≤
 ``grantExpireTime``).
* All other grants may use a longer window, still ≤ 24 h − 1 min.

Neighborhood radii come from the loaded DPA catalogue KML extended data
(category / indoor / outdoor / ≤6 m AGL), not from fixture IDs.

``heightType=AMSL`` is converted to AGL via terrain elevation
(``AGL = AMSL − ground_elevation``). Terrain unavailability does not silently
treat AMSL as AGL (fail-closed for the 240 s cap when DPA data exists).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from models.models import Cbsd
from services.geometry import within_geojson_buffer_m
from services.spectrum_inquiry_service import _overlaps
from services.terrain.exceptions import TerrainDataUnavailable, TerrainError
from services.terrain.haat import resolve_ned_dataset_version, resolve_terrain_dir
from services.terrain.ned import NedTerrainProvider
from services.terrain.protocol import TerrainProvider

# Rel1Ext temporal windows (seconds).
TRANSMIT_EXPIRE_SEC = 60  # default short window (≤ 24h−1min; keeps HBT.5 fast)
DPA_NEIGHBORHOOD_TRANSMIT_EXPIRE_SEC = 240
MAX_TRANSMIT_EXPIRE_OUTSIDE_SEC = 24 * 3600 - 60  # 24h − 1 minute

DPA_TX_BAND_LOW_HZ = 3_550_000_000
DPA_TX_BAND_HIGH_HZ = 3_650_000_000
HEIGHT_SPLIT_M = 6.0

_terrain_lock = threading.Lock()
_terrain_provider: TerrainProvider | None = None


class DpaNeighborhoodStatus(str, Enum):
    """Result of evaluating CBSD location against the DPA catalogue."""

    OUTSIDE = "outside"
    INSIDE = "inside"
    # Missing height/geometry/radius data required to decide — never treat as OUTSIDE
    # when DPA catalogue/activation implies an applicable protection context.
    INDETERMINATE = "indeterminate"


def get_terrain_provider() -> TerrainProvider:
    """Process-wide terrain provider used for AMSL→AGL (lazy NED default)."""
    global _terrain_provider
    with _terrain_lock:
        if _terrain_provider is None:
            directory = resolve_terrain_dir(None)
            version = resolve_ned_dataset_version(directory)
            _terrain_provider = NedTerrainProvider(directory, dataset_version=version)
        return _terrain_provider


def set_terrain_provider(provider: TerrainProvider | None) -> None:
    """Inject terrain (tests) or clear to rebuild default (``None``)."""
    global _terrain_provider
    with _terrain_lock:
        _terrain_provider = provider


def reset_terrain_provider() -> None:
    set_terrain_provider(None)


def grant_overlaps_dpa_tx_expire_band(low_hz: int, high_hz: int) -> bool:
    """True when the grant overlaps the Rel1Ext 3550–3650 MHz TxExpire band."""
    return bool(_overlaps(low_hz, high_hz, DPA_TX_BAND_LOW_HZ, DPA_TX_BAND_HIGH_HZ))


def _installation(cbsd: Cbsd) -> dict[str, Any]:
    try:
        reg = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(reg, dict):
        return {}
    inst = reg.get("installationParam") or {}
    return inst if isinstance(inst, dict) else {}


def resolve_height_agl_m(
    lat: float,
    lon: float,
    height_m: float,
    height_type: str,
    *,
    terrain: TerrainProvider | None = None,
) -> float:
    """Return antenna height AGL (meters).

 * ``AGL`` — use ``height_m`` directly.
 * ``AMSL`` — ``AGL = AMSL − ground_elevation`` via terrain/NED.
 """
    normalized = str(height_type or "AGL").strip().upper()
    if normalized == "AGL":
        return float(height_m)
    if normalized != "AMSL":
        raise TerrainDataUnavailable(f"unsupported heightType={height_type!r}")
    active = terrain if terrain is not None else get_terrain_provider()
    ground = float(active.elevation_m(lat, lon))
    return float(height_m) - ground


def _cbsd_lat_lon_height_agl(
    cbsd: Cbsd, *, terrain: TerrainProvider | None = None
) -> tuple[float, float, float, bool, str]:
    """Return (lat, lon, height_agl_m, indoor, category).

    Raises ``TerrainDataUnavailable`` / ``TerrainError`` when AMSL cannot be converted.
    Raises ``ValueError`` when coordinates/height are unusable.
    """
    inst = _installation(cbsd)
    try:
        lat = float(inst["latitude"])
        lon = float(inst["longitude"])
        height = float(inst["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("installationParam latitude/longitude/height required") from exc
    height_type = str(inst.get("heightType") or "AGL")
    height_agl = resolve_height_agl_m(
        lat, lon, height, height_type, terrain=terrain
    )
    indoor = bool(inst.get("indoorDeployment", False))
    try:
        reg = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        reg = {}
    cat = str(
        (reg.get("cbsdCategory") if isinstance(reg, dict) else None)
        or cbsd.cbsd_category
        or "A"
    ).upper()
    if cat not in {"A", "B"}:
        cat = "A"
    return lat, lon, height_agl, indoor, cat


def neighborhood_radius_km_for_cbsd(
    neighborhood_km: dict[str, float],
    *,
    category: str,
    indoor: bool,
    height_agl_m: float,
) -> float | None:
    """Pick the catalogue radius matching CBSD class / indoor / ≤6 m AGL."""
    if not neighborhood_km:
        return None
    le6 = height_agl_m <= HEIGHT_SPLIT_M
    cat = category.upper()
    preferred: list[str]
    if cat == "B":
        preferred = (
            ["catB_6m_NeighborhoodDistanceKm", "catBNeighborhoodDistanceKm"]
            if le6
            else ["catBNeighborhoodDistanceKm", "catB_6m_NeighborhoodDistanceKm"]
        )
    elif indoor:
        preferred = (
            [
                "catA_Indoor_6m_NeighborhoodDistanceKm",
                "catA_Indoor_NeighborhoodDistanceKm",
            ]
            if le6
            else [
                "catA_Indoor_NeighborhoodDistanceKm",
                "catA_Indoor_6m_NeighborhoodDistanceKm",
            ]
        )
    else:
        preferred = (
            [
                "catA_Outdoor_6m_NeighborhoodDistanceKm",
                "catA_Outdoor_NeighborhoodDistanceKm",
            ]
            if le6
            else [
                "catA_Outdoor_NeighborhoodDistanceKm",
                "catA_Outdoor_6m_NeighborhoodDistanceKm",
            ]
        )
    for key in preferred:
        if key in neighborhood_km:
            try:
                return float(neighborhood_km[key])
            except (TypeError, ValueError):
                continue
    # No preferred key — cannot pick a deterministic class radius.
    return None


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _active_dpa_ids(db: Session) -> set[str]:
    from services.dpa_service import list_active_activations

    out: set[str] = set()
    for data in list_active_activations(db):
        dpa_id = data.get("dpaId")
        if isinstance(dpa_id, str) and dpa_id.strip():
            out.add(dpa_id.strip())
    return out


def evaluate_dpa_neighborhood(
    db: Session,
    cbsd: Cbsd,
    *,
    terrain: TerrainProvider | None = None,
) -> DpaNeighborhoodStatus:
    """Classify CBSD against loaded DPA contours / neighborhoods."""
    from services.dpa_service import list_catalogue

    catalogue = [item for item in list_catalogue(db) if isinstance(item, dict)]
    if not catalogue:
        return DpaNeighborhoodStatus.OUTSIDE

    try:
        lat, lon, height_agl, indoor, category = _cbsd_lat_lon_height_agl(
            cbsd, terrain=terrain
        )
    except (TerrainError, ValueError):
        # Cannot resolve AGL (e.g. AMSL without terrain) while DPAs exist.
        return DpaNeighborhoodStatus.INDETERMINATE

    active_ids = _active_dpa_ids(db)
    saw_usable = False
    for item in catalogue:
        dpa_id = str(item.get("dpaId") or "").strip()
        geometry = item.get("geometry")
        if not isinstance(geometry, dict):
            # Active DPA without geometry must not collapse to OUTSIDE.
            if dpa_id and dpa_id in active_ids:
                return DpaNeighborhoodStatus.INDETERMINATE
            # Inactive catalogue row with no geometry: skip (not evaluable).
            continue

        nb = item.get("neighborhoodKm") or {}
        if not isinstance(nb, dict):
            nb = {}
        nb_clean: dict[str, float] = {}
        for key, raw in nb.items():
            if _is_number(raw):
                nb_clean[str(key)] = float(raw)

        radius_km = neighborhood_radius_km_for_cbsd(
            nb_clean,
            category=category,
            indoor=indoor,
            height_agl_m=height_agl,
        )
        if radius_km is None:
            if nb_clean:
                # Keys present but none matched this CBSD class — data incomplete.
                if dpa_id and dpa_id in active_ids:
                    return DpaNeighborhoodStatus.INDETERMINATE
                # Contour-only fallback for inactive rows.
                saw_usable = True
                if within_geojson_buffer_m(lat, lon, geometry, 0.0):
                    return DpaNeighborhoodStatus.INSIDE
                continue
            # No neighborhood metadata: contour interior only.
            saw_usable = True
            if within_geojson_buffer_m(lat, lon, geometry, 0.0):
                return DpaNeighborhoodStatus.INSIDE
            continue

        saw_usable = True
        if within_geojson_buffer_m(lat, lon, geometry, radius_km * 1000.0):
            return DpaNeighborhoodStatus.INSIDE

    if not saw_usable and active_ids:
        return DpaNeighborhoodStatus.INDETERMINATE
    return DpaNeighborhoodStatus.OUTSIDE


def cbsd_in_any_dpa_neighborhood(db: Session, cbsd: Cbsd) -> bool:
    """True only for a definitive INSIDE classification."""
    return evaluate_dpa_neighborhood(db, cbsd) is DpaNeighborhoodStatus.INSIDE


def requires_dpa_neighborhood_tx_cap(
    db: Session, cbsd: Cbsd, *, low_hz: int, high_hz: int
) -> bool:
    """HBT.13 short TxExpire: neighborhood INSIDE or INDETERMINATE on 3550–3650 MHz."""
    if not grant_overlaps_dpa_tx_expire_band(low_hz, high_hz):
        return False
    status = evaluate_dpa_neighborhood(db, cbsd)
    return status in {
        DpaNeighborhoodStatus.INSIDE,
        DpaNeighborhoodStatus.INDETERMINATE,
    }


def transmit_expire_horizon_sec(
    db: Session,
    cbsd: Cbsd,
    *,
    low_hz: int,
    high_hz: int,
) -> int:
    """Seconds ahead for transmitExpireTime under Rel1Ext rules."""
    if requires_dpa_neighborhood_tx_cap(db, cbsd, low_hz=low_hz, high_hz=high_hz):
        return min(DPA_NEIGHBORHOOD_TRANSMIT_EXPIRE_SEC, MAX_TRANSMIT_EXPIRE_OUTSIDE_SEC)
    return min(TRANSMIT_EXPIRE_SEC, MAX_TRANSMIT_EXPIRE_OUTSIDE_SEC)


def compute_transmit_expire_time(
    db: Session,
    cbsd: Cbsd,
    grant_expire: datetime,
    *,
    low_hz: int,
    high_hz: int,
    now: datetime | None = None,
) -> datetime:
    """Timezone-aware UTC wall time for transmitExpireTime."""
    from services.clock import ensure_utc, utc_now

    wall = ensure_utc(now or utc_now()).replace(microsecond=0)
    horizon = transmit_expire_horizon_sec(db, cbsd, low_hz=low_hz, high_hz=high_hz)
    tx = wall + timedelta(seconds=horizon)
    ge = ensure_utc(grant_expire).replace(microsecond=0)
    if tx > ge:
        tx = ge
    return tx


def fmt_transmit_expire(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
