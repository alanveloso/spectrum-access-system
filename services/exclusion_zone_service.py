"""Exclusion zone (EXZ) persistence and grant/SIQ/CPAS interference checks."""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

from providers.contract import GeoJsonRecord, TOKEN_NTIA_PROTECTION_ZONES
from runtime.data_access import require_composed_provider, try_composed_provider
from models.models import AdminInjectedData
from services.geometry import geojson_geometry_usable, within_geojson_buffer_m
from services.meas_report import admin_flag_set, set_admin_flag
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

KIND_EXCLUSION_ZONE = "exclusion_zone"
FLAG_NTIA_15_517 = "ntia_15_517"
KIND_NTIA_ZONES = "ntia_exclusion_zones"


class ExclusionZoneError(ValueError):
    """Invalid EXZ geometry / configuration (domain validation)."""


class ExclusionZoneUnavailable(RuntimeError):
    """Required EXZ dataset missing (fail-closed; ENV/DATASET)."""


def _overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _load_injected(db: Session, kind: str) -> list[dict[str, Any]]:
    rows = db.query(AdminInjectedData).filter_by(kind=kind).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(json.loads(row.data_json))
        except json.JSONDecodeError:
            continue
    return out


# EXZ buffer: WINNF SAS FT Exclusion Zone suite — CBSD inside zone or within
# 50 m of the boundary → interference (responseCode 400). Not a lab margin.
EXZ_BUFFER_M = 50.0

# NTIA TR 15-517 coastal combined contours protect GBS band 3550–3650 MHz.
NTIA_GBS_LOW_HZ = 3_550_000_000
NTIA_GBS_HIGH_HZ = 3_650_000_000
NTIA_COASTAL_NAMES = ("West Combined Contour", "East-Gulf Combined Contour")


def _ntia_coastal_geojson_from_provider() -> dict[str, Any]:
    provider = try_composed_provider("reference_data")
    if provider is None:
        provider = require_composed_provider("reference_data")
    record = provider.fetch(token=TOKEN_NTIA_PROTECTION_ZONES)
    if not isinstance(record, GeoJsonRecord):
        raise ExclusionZoneUnavailable(
            "composed reference_data provider returned unexpected NTIA record"
        )
    return record.document


def _parse_kml_coordinates(text: str | None) -> list[list[float]]:
    ring: list[list[float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                ring.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return ring


def _parse_kml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"type": "FeatureCollection", "features": []}
    root = ET.parse(path).getroot()
    features: list[dict[str, Any]] = []
    wanted = set(NTIA_COASTAL_NAMES)
    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name_el = pm.find("{http://www.opengis.net/kml/2.2}name")
        if name_el is None or name_el.text not in wanted:
            continue
        outer = pm.find(
            ".//{http://www.opengis.net/kml/2.2}outerBoundaryIs"
            "/{http://www.opengis.net/kml/2.2}LinearRing"
            "/{http://www.opengis.net/kml/2.2}coordinates"
        )
        coords_el = outer if outer is not None else pm.find(".//{http://www.opengis.net/kml/2.2}coordinates")
        ring = _parse_kml_coordinates(coords_el.text if coords_el is not None else None)
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        features.append(
            {
                "type": "Feature",
                "properties": {"name": name_el.text},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
        wanted.discard(name_el.text)
        if not wanted:
            break
    return {"type": "FeatureCollection", "features": features}


def load_ntia_coastal_geojson(kml_path: Path | None = None) -> dict[str, Any]:
    """Parse West / East-Gulf Combined Contours from protection_zones.kml.

    Missing file → empty FeatureCollection (caller must treat as ENV/DATASET gap).
    Does not invent coastal polygons.
    """
    if kml_path is not None:
        return _parse_kml_file(kml_path)
    return _ntia_coastal_geojson_from_provider()


def validate_exclusion_zone_record(record: dict[str, Any]) -> None:
    """Raise when an EXZ payload claims protection but geometry is unusable."""
    if not isinstance(record, dict):
        raise ExclusionZoneError("EXZ record must be an object")
    zone = record.get("zone")
    if zone is None:
        raise ExclusionZoneError("EXZ missing zone geometry")
    if not isinstance(zone, dict):
        raise ExclusionZoneError("EXZ zone must be a GeoJSON object")
    if not geojson_geometry_usable(zone):
        raise ExclusionZoneError("EXZ geometry empty or invalid")


def persist_exclusion_zone(db: Session, payload: dict[str, Any]) -> None:
    """Store InjectExclusionZone body: {zone, frequencyRanges}."""
    body = payload if isinstance(payload, dict) else {}
    if body:
        validate_exclusion_zone_record(body)
    db.add(
        AdminInjectedData(
            kind=KIND_EXCLUSION_ZONE,
            data_json=json.dumps(body),
        )
    )
    db.commit()


def enable_ntia_exclusion_zones(db: Session) -> None:
    """Activate NTIA TR 15-517 coastal exclusion zones and cache their geometry.

    Raises ``ExclusionZoneUnavailable`` when the official KML is missing or
    yields no coastal contours (no fake geometry).
    """
    geojson = load_ntia_coastal_geojson()
    features = geojson.get("features") if isinstance(geojson, dict) else None
    if not isinstance(features, list) or not features:
        raise ExclusionZoneUnavailable(
            "NTIA TR 15-517 protection_zones.kml missing or empty "
            "(BLOCKED_BY_DATASET); refusing to enable without contours"
        )
    if not geojson_geometry_usable(geojson):
        raise ExclusionZoneUnavailable(
            "NTIA TR 15-517 coastal geometry invalid (BLOCKED_BY_DATASET)"
        )
    set_admin_flag(db, FLAG_NTIA_15_517)
    existing = db.query(AdminInjectedData).filter_by(kind=KIND_NTIA_ZONES).first()
    payload = json.dumps(
        {
            "zone": geojson,
            "frequencyRanges": [
                {"lowFrequency": NTIA_GBS_LOW_HZ, "highFrequency": NTIA_GBS_HIGH_HZ}
            ],
        }
    )
    if existing:
        existing.data_json = payload
    else:
        db.add(AdminInjectedData(kind=KIND_NTIA_ZONES, data_json=payload))
    db.commit()


def _freq_ranges(record: dict[str, Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for fr in record.get("frequencyRanges") or []:
        if not isinstance(fr, dict):
            continue
        low, high = fr.get("lowFrequency"), fr.get("highFrequency")
        if low is None or high is None:
            continue
        try:
            ranges.append((int(low), int(high)))
        except (TypeError, ValueError):
            continue
    return ranges


def load_active_exclusion_zone_records(db: Session) -> list[dict[str, Any]]:
    """Live EXZ inject + NTIA cache when flag is set (admission / HBT)."""
    records = list(_load_injected(db, KIND_EXCLUSION_ZONE))
    if admin_flag_set(db, FLAG_NTIA_15_517):
        ntia = _load_injected(db, KIND_NTIA_ZONES)
        if not ntia:
            raise ExclusionZoneUnavailable(
                "NTIA 15-517 enabled but ntia_exclusion_zones cache missing"
            )
        for rec in ntia:
            validate_exclusion_zone_record(rec)
            if not geojson_geometry_usable(rec.get("zone")):
                raise ExclusionZoneUnavailable(
                    "NTIA 15-517 enabled but coastal geometry empty"
                )
        records.extend(ntia)
    return records


def _zone_records(db: Session) -> list[dict[str, Any]]:
    return load_active_exclusion_zone_records(db)


def point_hits_exclusion_records(
    records: Sequence[dict[str, Any]],
    lat: float,
    lon: float,
    low_hz: int | None = None,
    high_hz: int | None = None,
    *,
    buffer_m: float = EXZ_BUFFER_M,
    strict_geometry: bool = True,
) -> bool:
    """True if point hits any EXZ record (frozen or live payloads)."""
    for record in records:
        if not isinstance(record, dict):
            continue
        zone = record.get("zone")
        if strict_geometry:
            if zone is None or not isinstance(zone, dict):
                raise ExclusionZoneError("EXZ missing zone geometry")
            if not geojson_geometry_usable(zone):
                raise ExclusionZoneError("EXZ geometry empty or invalid")
        elif not zone or not geojson_geometry_usable(zone):
            continue
        if not within_geojson_buffer_m(lat, lon, zone, buffer_m):
            continue
        freq_ranges = _freq_ranges(record)
        if not freq_ranges:
            return True
        if low_hz is None or high_hz is None:
            return True
        for zlow, zhigh in freq_ranges:
            if _overlaps(low_hz, high_hz, zlow, zhigh):
                return True
    return False


def point_hits_exclusion_zone(
    db: Session,
    lat: float,
    lon: float,
    low_hz: int | None = None,
    high_hz: int | None = None,
) -> bool:
    """True if (lat, lon) is inside/near an active EXZ overlapping [low_hz, high_hz].

    When low/high are None, any overlapping frequency check is skipped (location only).
    """
    return point_hits_exclusion_records(
        load_active_exclusion_zone_records(db),
        lat,
        lon,
        low_hz,
        high_hz,
    )


def exclusion_freq_ranges_at_point(
    db: Session, lat: float, lon: float
) -> list[tuple[int, int]]:
    """Frequency ranges protected by EXZs covering this point (incl. 50 m buffer)."""
    out: list[tuple[int, int]] = []
    for record in load_active_exclusion_zone_records(db):
        zone = record.get("zone")
        if not within_geojson_buffer_m(lat, lon, zone, EXZ_BUFFER_M):
            continue
        out.extend(_freq_ranges(record))
    return out


def capture_exclusion_zone_records_for_freeze(
    db: Session,
) -> list[tuple[str, str, str]]:
    """Snapshot EXZ inject + NTIA cache for CPAS generation N."""
    rows: list[tuple[str, str, str]] = []
    for idx, payload in enumerate(_load_injected(db, KIND_EXCLUSION_ZONE)):
        validate_exclusion_zone_record(payload)
        rows.append(
            (
                KIND_EXCLUSION_ZONE,
                f"exz:{idx}",
                json.dumps(payload, sort_keys=True, default=str),
            )
        )
    if admin_flag_set(db, FLAG_NTIA_15_517):
        ntia = _load_injected(db, KIND_NTIA_ZONES)
        if not ntia:
            raise ExclusionZoneUnavailable(
                "NTIA 15-517 enabled but cache missing at CPAS freeze"
            )
        for idx, payload in enumerate(ntia):
            validate_exclusion_zone_record(payload)
            rows.append(
                (
                    KIND_NTIA_ZONES,
                    f"ntia:{idx}",
                    json.dumps(payload, sort_keys=True, default=str),
                )
            )
    return rows
