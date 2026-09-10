"""Admin PPA creation lifecycle.

Implements TriggerPpaCreation / GetPpaCreationStatus semantics:

- validate PALs (known + VALID + shared holder);
- validate cluster CBSDs (registered, holder match);
- build maximum contour: claimedBoundary / providedContour clipped to
 PAL service-area (and census tracts when provisioned);
- reject when a required clip dataset is configured but missing;
- reject overlap with an existing PPA on the same PAL channel;
- persist ZoneData for FAD export;
- status progresses to completed with withError only after a real failure.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData, Cbsd
from services.county_geometry import CountyGeometryError, load_county_geometry
from services.fad_service import rewrite_zone_id
from services.geometry import (
    geojson_areas_overlap,
    geojson_geometry_usable,
    iter_geojson_rings,
)
from services.meas_report import set_admin_flag
from services.pal_service import load_pal_records
from services.ppa_geometry import (
    as_feature_collection,
    intersect_geojson,
    point_covered_by_area,
    polygon_covered_by,
    polygon_within_service_area,
    union_geojson,
)
from services.ppa_rf_contour import (
    PpaRfContourError,
    PpaRfEngines,
    cbsd_orm_to_ppa_device,
    load_default_ppa_rf_engines,
    maximum_rf_ppa_contour,
)

KIND_STATUS = "ppa_creation_status"
KIND_ZONE = "zone"
KIND_AUDIT = "ppa_audit"
KIND_CLUSTER_LIST = "cluster_list"
KIND_PCR_CONFIG = "ppa_creation_config"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CENSUS_DIR = _REPO_ROOT / "data" / "geo" / "census"


class PpaCreationError(Exception):
    """Domain failure that must surface as withError=true after completion."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _append_audit(db: Session, event: str, detail: dict[str, Any]) -> None:
    db.add(
        AdminInjectedData(
            kind=KIND_AUDIT,
            data_json=json.dumps(
                {"event": event, "at": _utc_now_iso(), **detail},
                default=str,
            ),
        )
    )


def set_ppa_creation_status(
    db: Session,
    *,
    completed: bool,
    with_error: bool,
    reason: str | None = None,
    ppa_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "completed": completed,
        "withError": with_error,
        "updatedAt": _utc_now_iso(),
    }
    if reason is not None:
        payload["reason"] = reason
    if ppa_id is not None:
        payload["ppaId"] = ppa_id
    set_admin_flag(db, KIND_STATUS, payload)


def get_ppa_creation_status(db: Session) -> dict[str, bool]:
    """Return harness status object; unfinished/absent → completed=false."""
    row = db.query(AdminInjectedData).filter_by(kind=KIND_STATUS).first()
    if not row:
        return {"completed": False, "withError": False}
    try:
        status = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return {"completed": False, "withError": False}
    if not isinstance(status, dict):
        return {"completed": False, "withError": False}
    return {
        "completed": bool(status.get("completed", False)),
        "withError": bool(status.get("withError", False)),
    }


def _cbsd_location(cbsd: Cbsd) -> tuple[float, float] | None:
    try:
        reg = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return None
    inst = reg.get("installationParam") or {}
    lat, lon = inst.get("latitude"), inst.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _is_valid_feature_collection(contour: Any) -> bool:
    if not isinstance(contour, dict) or contour.get("type") != "FeatureCollection":
        return False
    features = contour.get("features")
    if not isinstance(features, list) or not features:
        return False
    return bool(iter_geojson_rings(contour))


def _convex_hull_lonlat(points: list[tuple[float, float]]) -> list[list[float]]:
    """Andrew's monotone chain; points are (lat, lon); ring is GeoJSON [lon, lat]."""
    unique = sorted({(lon, lat) for lat, lon in points})
    if len(unique) == 1:
        lon, lat = unique[0]
        d = 0.001
        return [
            [lon - d, lat - d],
            [lon - d, lat + d],
            [lon + d, lat + d],
            [lon + d, lat - d],
            [lon - d, lat - d],
        ]
    if len(unique) == 2:
        (lon1, lat1), (lon2, lat2) = unique
        pad = 0.0005
        return [
            [lon1 - pad, lat1 - pad],
            [lon1 - pad, lat1 + pad],
            [lon2 + pad, lat2 + pad],
            [lon2 + pad, lat2 - pad],
            [lon1 - pad, lat1 - pad],
        ]

    def cross(
        o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    ring = [[lon, lat] for lon, lat in hull]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def _geometry_from_cluster(locations: list[tuple[float, float]]) -> dict[str, Any]:
    ring = _convex_hull_lonlat(locations)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }


def _inline_pal_service_area(pal: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy inline GeoJSON on the PAL record (unit-test / admin inject)."""
    raw_license = pal.get("license")
    license_obj: dict[str, Any] = raw_license if isinstance(raw_license, dict) else {}
    for key in ("licenseArea", "serviceArea", "licenseAreaGeometry"):
        geo = pal.get(key) or license_obj.get(key)
        if isinstance(geo, dict) and iter_geojson_rings(geo):
            return geo
    return None


def _pal_license_area_identifier(pal: dict[str, Any]) -> str | None:
    raw_license = pal.get("license")
    license_obj: dict[str, Any] = raw_license if isinstance(raw_license, dict) else {}
    ident = license_obj.get("licenseAreaIdentifier") or pal.get("licenseAreaIdentifier")
    if ident is None or str(ident).strip() == "":
        return None
    return str(ident).strip()


def _pal_service_area(pal: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one PAL licensed area.

 Precedence (reference / WInnForum PAL): ``license.licenseAreaIdentifier``
 (county FIPS GeoJSON) over inline geometry fields. Identifier present but
 unloadable raises ``PpaCreationError`` (fail closed, never silent None).
 """
    ident = _pal_license_area_identifier(pal)
    if ident is not None:
        try:
            return load_county_geometry(ident)
        except CountyGeometryError as exc:
            raise PpaCreationError("service_area_unavailable") from exc
    return _inline_pal_service_area(pal)


def _union_service_areas(pals: list[dict[str, Any]]) -> dict[str, Any] | None:
    parts: list[dict[str, Any]] = []
    for pal in pals:
        area = _pal_service_area(pal)
        if area is not None:
            parts.append(area)
    if not parts:
        return None
    return union_geojson(*parts)


def _as_feature_collection(geom: dict[str, Any]) -> dict[str, Any]:
    if geom.get("type") == "FeatureCollection":
        return geom
    if geom.get("type") == "Feature":
        return {"type": "FeatureCollection", "features": [geom]}
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": geom}],
    }


def _pcr_config(db: Session) -> dict[str, Any]:
    row = db.query(AdminInjectedData).filter_by(kind=KIND_PCR_CONFIG).first()
    defaults = {"requireCensusClip": False, "requireServiceArea": False}
    if not row:
        return defaults
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return defaults
    if not isinstance(data, dict):
        return defaults
    return {
        "requireCensusClip": bool(data.get("requireCensusClip", False)),
        "requireServiceArea": bool(data.get("requireServiceArea", False)),
    }


def _load_census_geometry() -> dict[str, Any] | None:
    if not _CENSUS_DIR.is_dir():
        return None
    features: list[dict[str, Any]] = []
    for path in sorted(_CENSUS_DIR.glob("*.geojson")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "FeatureCollection":
            features.extend(
                f for f in (data.get("features") or []) if isinstance(f, dict)
            )
        elif data.get("type") == "Feature":
            features.append(data)
        elif geojson_geometry_usable(data):
            features.append(
                {"type": "Feature", "properties": {}, "geometry": data}
            )
    if not features:
        return None
    return {"type": "FeatureCollection", "features": features}


def _clip_contour_to_max(
    contour: dict[str, Any], max_area: dict[str, Any]
) -> dict[str, Any]:
    """Return contour ∩ max_area as a FeatureCollection (areal parts only)."""
    clipped = intersect_geojson(contour, max_area)
    if clipped is None:
        raise PpaCreationError("contour_empty_after_clip")
    fc = as_feature_collection(clipped)
    if fc is None:
        raise PpaCreationError("contour_empty_after_clip")
    return fc


def _cluster_ids_from_injection(db: Session, body: dict[str, Any]) -> list[str] | None:
    cbsd_ids = body.get("cbsdIds")
    if isinstance(cbsd_ids, list) and cbsd_ids:
        return None
    user_id = body.get("userId")
    pal_ids = body.get("palIds") or []
    for row in db.query(AdminInjectedData).filter_by(kind=KIND_CLUSTER_LIST).all():
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if user_id and data.get("userId") == user_id:
            ids = data.get("cbsdIds")
            if isinstance(ids, list) and ids:
                return [str(x) for x in ids]
        if pal_ids and data.get("palId") in pal_ids:
            ids = data.get("cbsdIds")
            if isinstance(ids, list) and ids:
                return [str(x) for x in ids]
    return None


def _points_within(zone: dict[str, Any], locations: list[tuple[float, float]]) -> bool:
    return all(point_covered_by_area(lat, lon, zone) for lat, lon in locations)


def _contour_within_service_area(
    contour: dict[str, Any], service_area: dict[str, Any]
) -> bool:
    """PPA/claimed vs licensed SA: reference buffer-then-within."""
    return polygon_within_service_area(contour, service_area)


def _existing_ppa_records(db: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in db.query(AdminInjectedData).filter_by(kind=KIND_ZONE).all():
        try:
            payload = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        record = payload.get("record") or payload
        if not isinstance(record, dict):
            continue
        if record.get("usage") != "PPA" and "ppaInfo" not in record:
            continue
        out.append(record)
    return out


def _geometries_overlap(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Admin PPA conflict: True when polygon interiors intersect.

 Delegates to :func:`services.geometry.geojson_areas_overlap` (edge crossings
 and containment; boundary-only touch is not a conflict).
 """
    return geojson_areas_overlap(a, b)


def _pal_freq(pal: dict[str, Any]) -> tuple[int, int] | None:
    assignment = (pal.get("channelAssignment") or {}).get("primaryAssignment") or {}
    low, high = assignment.get("lowFrequency"), assignment.get("highFrequency")
    if low is None or high is None:
        block = pal.get("palBlock") or {}
        low, high = block.get("lowFrequency"), block.get("highFrequency")
    if low is None or high is None:
        return None
    try:
        return int(low), int(high)
    except (TypeError, ValueError):
        return None


def _freqs_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and a[1] > b[0]


def _validate_and_build(
    db: Session, body: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    pal_ids = body.get("palIds")
    cbsd_ids = body.get("cbsdIds")
    provided = body.get("providedContour") or body.get("claimedBoundary")
    cfg = _pcr_config(db)

    if not isinstance(pal_ids, list) or not pal_ids:
        raise PpaCreationError("missing_palIds")
    if not all(isinstance(p, str) and p.strip() for p in pal_ids):
        raise PpaCreationError("invalid_palIds")
    pal_ids = [p.strip() for p in pal_ids]
    if len(set(pal_ids)) != len(pal_ids):
        raise PpaCreationError("duplicate_palIds")

    injected_cluster = _cluster_ids_from_injection(db, body)
    if (not isinstance(cbsd_ids, list) or not cbsd_ids) and injected_cluster:
        cbsd_ids = injected_cluster

    if not isinstance(cbsd_ids, list) or not cbsd_ids:
        raise PpaCreationError("missing_cbsdIds")
    if not all(isinstance(c, str) and c.strip() for c in cbsd_ids):
        raise PpaCreationError("invalid_cbsdIds")
    cbsd_ids = [c.strip() for c in cbsd_ids]
    if len(set(cbsd_ids)) != len(cbsd_ids):
        raise PpaCreationError("duplicate_cbsdIds")

    pals_by_id = {
        p["palId"]: p
        for p in load_pal_records(db, active_only=False)
        if p.get("palId")
    }
    selected: list[dict[str, Any]] = []
    for pal_id in pal_ids:
        pal = pals_by_id.get(pal_id)
        if pal is None:
            raise PpaCreationError("unknown_palId")
        status = str(pal.get("licenseStatus") or "").upper()
        if status != "VALID":
            raise PpaCreationError("inactive_pal")
        selected.append(pal)

    holders = {str(p.get("userId") or "") for p in selected}
    if len(holders) != 1 or not next(iter(holders)):
        raise PpaCreationError("pal_holder_mismatch")
    holder = next(iter(holders))

    cbsds: list[Cbsd] = []
    for cid in cbsd_ids:
        row = db.query(Cbsd).filter_by(cbsd_id=cid).first()
        if row is None:
            raise PpaCreationError("unknown_cbsdId")
        if str(row.user_id or "") != holder:
            raise PpaCreationError("cbsd_not_pal_holder")
        cbsds.append(row)

    locations: list[tuple[float, float]] = []
    for cbsd in cbsds:
        loc = _cbsd_location(cbsd)
        if loc is None:
            raise PpaCreationError("cbsd_missing_location")
        locations.append(loc)

    service_area = _union_service_areas(selected)
    if cfg.get("requireServiceArea") and service_area is None:
        raise PpaCreationError("service_area_required")
    if service_area is not None and not _points_within(service_area, locations):
        raise PpaCreationError("cbsd_outside_service_area")

    census = _load_census_geometry()
    if cfg.get("requireCensusClip") and census is None:
        raise PpaCreationError("census_dataset_missing")
    if census is not None and not _points_within(census, locations):
        raise PpaCreationError("cbsd_outside_census")

    # --- Maximum / Largest Allowable PPA Contour (RF) ---
    # Order (reference_models.ppa.ppa semantics + local clip):
    #   per-CBSD RF (−96 dBm/10 MHz) → union → clip census → clip PAL service area
    #   → optional claimedBoundary ⊆ result.
    # Hull is never used as an RF substitute.
    rf_engines = body.get("_rfEngines")
    try:
        engines: PpaRfEngines
        if isinstance(rf_engines, PpaRfEngines):
            engines = rf_engines
        else:
            engines = load_default_ppa_rf_engines()
        devices = [cbsd_orm_to_ppa_device(c) for c in cbsds]
        rf_max = maximum_rf_ppa_contour(devices, engines=engines)
    except PpaRfContourError as exc:
        raise PpaCreationError(f"rf_contour_unavailable:{exc}") from exc

    contour = rf_max
    if census is not None:
        contour = _clip_contour_to_max(contour, census)
        if not _points_within(contour, locations):
            raise PpaCreationError("contour_outside_census")
    if service_area is not None:
        contour = _clip_contour_to_max(contour, service_area)
        if not _points_within(contour, locations):
            raise PpaCreationError("cbsd_outside_max_contour")

    if provided is not None:
        if not (
            _is_valid_feature_collection(provided)
            or geojson_geometry_usable(provided)
        ):
            raise PpaCreationError("invalid_providedContour")
        claimed = (
            provided
            if isinstance(provided, dict)
            and provided.get("type") == "FeatureCollection"
            else _as_feature_collection(provided)
        )
        # Cluster CBSDs are not required to lie inside a valid claimed contour
        # (holder may shrink the maximum PPA). Holder + PAL SA still apply above.
        if service_area is not None and not _contour_within_service_area(
            claimed, service_area
        ):
            raise PpaCreationError("claimedBoundary_outside_service_area")
        if not polygon_covered_by(claimed, contour):
            raise PpaCreationError("claimedBoundary_exceeds_rf_maximum")
        contour = claimed

    selected_freqs = [_pal_freq(p) for p in selected]
    selected_freqs_ok = [f for f in selected_freqs if f is not None]
    for existing in _existing_ppa_records(db):
        info = existing.get("ppaInfo") or {}
        existing_pal_ids = list(info.get("palId") or [])
        existing_pals = [pals_by_id[p] for p in existing_pal_ids if p in pals_by_id]
        existing_freqs = [f for f in (_pal_freq(p) for p in existing_pals) if f]
        freq_clash = any(
            _freqs_overlap(a, b) for a in selected_freqs_ok for b in existing_freqs
        )
        if not freq_clash and not set(existing_pal_ids).intersection(pal_ids):
            continue
        if _geometries_overlap(contour, existing.get("zone")):
            raise PpaCreationError("overlaps_existing_ppa")

    token = uuid.uuid4().hex
    raw_id = f"zone/ppa/pending/{pal_ids[0]}/{token}"
    ppa_id = rewrite_zone_id(raw_id, fallback_suffix=f"{pal_ids[0]}/{token}")

    # Official PCR / FAD: zone is FeatureCollection with exactly one Feature
    # whose geometry is Polygon or MultiPolygon (never N per-CBSD features).
    zone_fc = _as_feature_collection(contour)
    feats = zone_fc.get("features") if isinstance(zone_fc, dict) else None
    if not isinstance(feats, list) or len(feats) != 1:
        raise PpaCreationError("ppa_zone_must_be_single_feature")
    geom = feats[0].get("geometry") if isinstance(feats[0], dict) else None
    if not isinstance(geom, dict) or geom.get("type") not in {"Polygon", "MultiPolygon"}:
        raise PpaCreationError("ppa_zone_invalid_geometry")
    if not iter_geojson_rings({"type": "FeatureCollection", "features": feats}):
        raise PpaCreationError("ppa_zone_empty_geometry")
    zone_geom: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": dict(feats[0].get("properties") or {}),
                "geometry": geom,
            }
        ],
    }

    begin = _utc_now_iso()[:10]
    expiration = None
    for pal in selected:
        raw_license = pal.get("license")
        lic: dict[str, Any] = raw_license if isinstance(raw_license, dict) else {}
        expiration = lic.get("licenseExpiration") or pal.get("licenseExpiration")
        if expiration:
            break
    if not expiration:
        expiration = begin

    record = {
        "id": ppa_id,
        "name": f"PPA-{pal_ids[0]}",
        "creator": ppa_id.split("/")[2] if len(ppa_id.split("/")) > 2 else "sas",
        "usage": "PPA",
        "terminated": False,
        "zone": zone_geom,
        "ppaInfo": {
            "palId": pal_ids,
            "cbsdReferenceId": list(cbsd_ids),
            "ppaBeginDate": begin,
            "ppaExpirationDate": expiration,
        },
    }
    return ppa_id, record


def _persist_zone(db: Session, record: dict[str, Any]) -> None:
    db.add(
        AdminInjectedData(
            kind=KIND_ZONE,
            data_json=json.dumps({"record": record}),
        )
    )


def create_ppa(db: Session, body: dict[str, Any]) -> str:
    """Run PPA creation; return ppa id string or empty string on failure.

 Always leaves ``ppa_creation_status`` with completed=true when finished.
 ``withError`` is true only after a caught domain failure (never silent success).
 """
    if not isinstance(body, dict):
        body = {}

    set_ppa_creation_status(db, completed=False, with_error=False)

    from services.concurrency import (
        acquire_iap_admission_xact_lock,
        exclusive_iap_admission,
    )
    from services.data_injection_service import bump_injection_generation

    try:
        ppa_id, record = _validate_and_build(db, body)
        # IAP admission ↔ PPA zone: bump generation so old markers become incoherent.
        with exclusive_iap_admission():
            acquire_iap_admission_xact_lock(db)
            _persist_zone(db, record)
            bump_injection_generation(db, KIND_ZONE)
            set_ppa_creation_status(
                db, completed=True, with_error=False, ppa_id=ppa_id
            )
            _append_audit(db, "create_ppa_ok", {"ppaId": ppa_id})
            db.commit()
        return ppa_id
    except PpaCreationError as exc:
        set_ppa_creation_status(
            db, completed=True, with_error=True, reason=exc.reason
        )
        _append_audit(db, "create_ppa_error", {"reason": exc.reason})
        db.commit()
        return ""
    except Exception as exc:  # noqa: BLE001 — must not leave status=incomplete forever
        # Protocol failure path (empty id + withError), not success catch-all.
        set_ppa_creation_status(
            db,
            completed=True,
            with_error=True,
            reason=f"internal_error:{type(exc).__name__}",
        )
        _append_audit(
            db,
            "create_ppa_internal_error",
            {
                "errorType": type(exc).__name__,
                "errorMessage": str(exc)[:300],
            },
        )
        db.commit()
        return ""
