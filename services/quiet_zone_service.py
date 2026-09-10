"""Quiet-zone / FCC field-office / Table Mountain gates (WINNF QPR).

Distance provenance (normative unless noted):
* NRQZ bounds — 47 CFR § 1.924(a) (A)
* FCC Cat A 2.4 km / Cat B 4.8 km — 47 CFR / WINNF-TS-0112 (A)
* FCC office coordinates — 47 CFR § 0.121(b) dataset under ``data/fcc/`` (A)
* Table Mountain reference + distance table — WINNF-TS-0112 (A)
* Configurable protected areas — AdminInjectedData ``quiet_protected_area`` (B)

Missing required FCC office dataset → fail-closed (deny), never silent allow.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

from providers.contract import CoordinateListRecord, TOKEN_FCC_FIELD_OFFICES
from runtime.data_access import require_composed_provider, try_composed_provider
from sqlalchemy.orm import Session

# National Radio Quiet Zone (NRAO / NRRO) — 47 CFR § 1.924(a) NAD-83 bounds.
NRQZ_NORTH = 39.0 + 15.0 / 60.0 + 0.4 / 3600.0  # 39°15′0.4″ N
NRQZ_SOUTH = 37.0 + 30.0 / 60.0 + 0.4 / 3600.0  # 37°30′0.4″ N
NRQZ_EAST = -(78.0 + 29.0 / 60.0 + 59.0 / 3600.0)  # 78°29′59.0″ W
NRQZ_WEST = -(80.0 + 29.0 / 60.0 + 59.2 / 3600.0)  # 80°29′59.2″ W

# FCC protected field offices — WINNF / CFR radii by CBSD category.
FCC_OFFICE_RADIUS_CAT_A_KM = 2.4
FCC_OFFICE_RADIUS_CAT_B_KM = 4.8
# Back-compat alias (Cat A registration radius).
FCC_OFFICE_REG_RADIUS_KM = FCC_OFFICE_RADIUS_CAT_A_KM

# Table Mountain Radio Receiving Zone — WINNF-TS-0112 reference point.
TABLE_MOUNTAIN_LAT = 40.130660
TABLE_MOUNTAIN_LON = -105.244596
TABLE_MOUNTAIN_CAT_A_KM = 3.8
# Cat B distances by total operating bandwidth (MHz).
TABLE_MOUNTAIN_CAT_B_KM = (
    (10.0, 38.0),
    (20.0, 54.0),
    (30.0, 64.0),
    (float("inf"), 80.0),
)

KIND_PROTECTED_AREA = "quiet_protected_area"
KIND_QPR_CONFIG = "quiet_zone_config"

_fcc_offices: list[dict[str, float]] | None = None
_fcc_cache_key: str | None = None


class QuietZoneUnavailable(RuntimeError):
    """Required quiet-zone / FCC dataset missing or unusable."""


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def reset_fcc_office_cache() -> None:
    """Test helper: clear memoized FCC office list."""
    global _fcc_offices, _fcc_cache_key
    _fcc_offices = None
    _fcc_cache_key = None


def _load_fcc_offices_from_csv(csv_path: Path) -> list[dict[str, float]]:
    import csv

    offices: list[dict[str, float]] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                offices.append(
                    {
                        "latitude": float(row["latitude"]),
                        "longitude": float(row["longitude"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    return offices


def _load_fcc_offices(*, require: bool = True, csv_path: Path | None = None) -> list[dict[str, float]]:
    global _fcc_offices, _fcc_cache_key
    if csv_path is not None:
        offices = _load_fcc_offices_from_csv(csv_path)
        if require and not offices:
            raise QuietZoneUnavailable("fcc_field_office_dataset_missing")
        return offices
    if _fcc_offices is not None:
        if require and not _fcc_offices:
            raise QuietZoneUnavailable("fcc_field_office_dataset_empty")
        return _fcc_offices
    try:
        provider = try_composed_provider("reference_data")
        if provider is None:
            provider = require_composed_provider("reference_data")
        record = provider.fetch(token=TOKEN_FCC_FIELD_OFFICES)
    except ValueError as exc:
        raise QuietZoneUnavailable(str(exc)) from exc
    if not isinstance(record, CoordinateListRecord):
        raise QuietZoneUnavailable("composed reference_data provider invalid for FCC offices")
    offices = [
        {"latitude": lat, "longitude": lon} for lat, lon in record.coordinates
    ]
    cache_key = f"{record.provenance.provider_id}:{record.provenance.dataset_id}"
    _fcc_offices = offices
    _fcc_cache_key = cache_key
    if require and not offices:
        raise QuietZoneUnavailable("fcc_field_office_dataset_missing")
    return offices


def in_nrao_nrro_quiet_zone(lat: float, lon: float) -> bool:
    return NRQZ_SOUTH <= lat <= NRQZ_NORTH and NRQZ_WEST <= lon <= NRQZ_EAST


def fcc_office_radius_km(cbsd_category: str | None) -> float:
    cat = (cbsd_category or "A").strip().upper()
    if cat == "B":
        return FCC_OFFICE_RADIUS_CAT_B_KM
    return FCC_OFFICE_RADIUS_CAT_A_KM


def near_fcc_field_office(
    lat: float,
    lon: float,
    *,
    cbsd_category: str | None = "A",
    radius_km: float | None = None,
) -> bool:
    rad = (
        float(radius_km)
        if radius_km is not None
        else fcc_office_radius_km(cbsd_category)
    )
    for office in _load_fcc_offices(require=True):
        if _haversine_km(lat, lon, office["latitude"], office["longitude"]) <= rad:
            return True
    return False


def table_mountain_coordination_km(
    cbsd_category: str | None, bandwidth_mhz: float | None = None
) -> float:
    cat = (cbsd_category or "A").strip().upper()
    if cat != "B":
        return TABLE_MOUNTAIN_CAT_A_KM
    bw = float(bandwidth_mhz) if bandwidth_mhz is not None else float("inf")
    for limit, dist in TABLE_MOUNTAIN_CAT_B_KM:
        if bw <= limit:
            return dist
    return 80.0


def near_table_mountain(
    lat: float,
    lon: float,
    *,
    cbsd_category: str | None = "A",
    bandwidth_mhz: float | None = None,
) -> bool:
    rad = table_mountain_coordination_km(cbsd_category, bandwidth_mhz)
    return (
        _haversine_km(lat, lon, TABLE_MOUNTAIN_LAT, TABLE_MOUNTAIN_LON) <= rad
    )


def _default_qpr_config() -> dict[str, Any]:
    return {
        "tableMountainEnabled": True,
        "fccOfficesEnabled": True,
        "configurableAreasEnabled": True,
    }


def _qpr_config(db: Session | None) -> dict[str, Any]:
    if db is None:
        return _default_qpr_config()
    from models.models import AdminInjectedData

    row = db.query(AdminInjectedData).filter_by(kind=KIND_QPR_CONFIG).first()
    if not row:
        return _default_qpr_config()
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return _default_qpr_config()
    if not isinstance(data, dict):
        return _default_qpr_config()
    return {
        "tableMountainEnabled": bool(data.get("tableMountainEnabled", True)),
        "fccOfficesEnabled": bool(data.get("fccOfficesEnabled", True)),
        "configurableAreasEnabled": bool(data.get("configurableAreasEnabled", True)),
    }


def _configurable_areas(db: Session | None) -> list[dict[str, Any]]:
    if db is None:
        return []
    from models.models import AdminInjectedData

    out: list[dict[str, Any]] = []
    for row in db.query(AdminInjectedData).filter_by(kind=KIND_PROTECTED_AREA).all():
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _in_configurable_area(
    lat: float, lon: float, areas: Sequence[dict[str, Any]]
) -> bool:
    from services.geometry import point_in_geojson

    for area in areas:
        zone = area.get("zone") or area.get("geometry") or area
        if isinstance(zone, dict) and point_in_geojson(lat, lon, zone):
            return True
        # Point+radius form: {latitude, longitude, radiusKm}
        try:
            a_lat = float(area["latitude"])
            a_lon = float(area["longitude"])
            rad = float(area.get("radiusKm") or area.get("radius_km") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if rad > 0 and _haversine_km(lat, lon, a_lat, a_lon) <= rad:
            return True
    return False


def _category_from_payload(
    installation_or_reg: dict[str, Any], *, cbsd_category: str | None
) -> str:
    if cbsd_category:
        return str(cbsd_category).upper()
    cat = installation_or_reg.get("cbsdCategory")
    if cat:
        return str(cat).upper()
    return "A"


def quiet_zone_blocks_location(
    lat: float,
    lon: float,
    *,
    cbsd_category: str | None = "A",
    bandwidth_mhz: float | None = None,
    db: Session | None = None,
    require_fcc_dataset: bool = True,
) -> str | None:
    """Return a reason code when the location is blocked, else None.

    Raises QuietZoneUnavailable when the FCC office dataset is required and missing.
    """
    if in_nrao_nrro_quiet_zone(lat, lon):
        return "nrqz"
    cfg = _qpr_config(db)
    if cfg.get("fccOfficesEnabled", True):
        if require_fcc_dataset:
            _load_fcc_offices(require=True)
        if near_fcc_field_office(lat, lon, cbsd_category=cbsd_category):
            return "fcc_field_office"
    if cfg.get("tableMountainEnabled", True) and near_table_mountain(
        lat, lon, cbsd_category=cbsd_category, bandwidth_mhz=bandwidth_mhz
    ):
        return "table_mountain"
    if cfg.get("configurableAreasEnabled", True) and _in_configurable_area(
        lat, lon, _configurable_areas(db)
    ):
        return "configurable_protected_area"
    return None


def registration_blocked_by_quiet_zone(
    installation: dict[str, Any],
    *,
    cbsd_category: str | None = None,
    db: Session | None = None,
) -> bool:
    """True when Registration must be rejected (QPR.2 / QPR.5–8 applicable rules)."""
    try:
        lat = float(installation["latitude"])
        lon = float(installation["longitude"])
    except (KeyError, TypeError, ValueError):
        return False
    cat = _category_from_payload(installation, cbsd_category=cbsd_category)
    try:
        return quiet_zone_blocks_location(lat, lon, cbsd_category=cat, db=db) is not None
    except QuietZoneUnavailable:
        return True  # fail-closed


def grant_blocked_by_quiet_zone(
    lat: float,
    lon: float,
    *,
    cbsd_category: str | None = "A",
    low_hz: int | None = None,
    high_hz: int | None = None,
    db: Session | None = None,
) -> bool:
    """True when Grant must be denied for quiet-zone / coordination rules."""
    bw_mhz = None
    if low_hz is not None and high_hz is not None and high_hz > low_hz:
        bw_mhz = (high_hz - low_hz) / 1_000_000.0
    try:
        return (
            quiet_zone_blocks_location(
                lat,
                lon,
                cbsd_category=cbsd_category,
                bandwidth_mhz=bw_mhz,
                db=db,
            )
            is not None
        )
    except QuietZoneUnavailable:
        return True
