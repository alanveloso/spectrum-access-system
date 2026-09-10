"""US/Canada border geometry for Arrangement R sharing-zone membership.

Mirrors WINNF ``CheckCbsdInBorderSharingZone`` / ``GetClosestCanadianBorderPoint``
without requiring numpy/shapely at import time. Membership uses the official
sampled border KMZ under ``data/fcc/uscabdry_sampled.kmz``.

Fail-closed callers must treat a missing/unusable KMZ as unavailable when
Arrangement R frequency overlap applies.
"""

from __future__ import annotations

import math
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from providers.contract import LonLatVerticesRecord, TOKEN_US_CANADA_BORDER
from runtime.data_access import require_composed_provider, try_composed_provider

# Sharing-zone radii (km) — WINNF Arrangement R / BPR harness.
SHARING_ZONE_MAX_KM = 56.0
SHARING_ZONE_INNER_KM = 8.0

# Cached (lon, lat) vertices keyed by provider provenance.
_border_vertices: list[tuple[float, float]] | None = None
_border_cache_key: str | None = None


class BorderGeometryUnavailable(RuntimeError):
    """Required US/Canada border KMZ missing or unusable."""


def reset_border_geometry_cache() -> None:
    """Test helper: clear memoized border vertices."""
    global _border_vertices, _border_cache_key
    _border_vertices = None
    _border_cache_key = None


def _vertices_from_composed_provider() -> tuple[str, list[tuple[float, float]]]:
    provider = try_composed_provider("boundaries")
    if provider is None:
        provider = require_composed_provider("boundaries")
    record = provider.fetch(token=TOKEN_US_CANADA_BORDER)
    if not isinstance(record, LonLatVerticesRecord):
        raise BorderGeometryUnavailable(
            "composed boundaries provider returned unexpected record type"
        )
    key = f"{record.provenance.provider_id}:{record.provenance.dataset_id}"
    return key, list(record.vertices)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        dlon
    )
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _angle_between(angle: float, min_angle: float, max_angle: float) -> bool:
    angle %= 360.0
    min_angle %= 360.0
    max_angle %= 360.0
    if min_angle < max_angle:
        return min_angle <= angle <= max_angle
    return angle >= min_angle or angle <= max_angle


def _parse_coord_text(text: str | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        out.append((lon, lat))
    return out


def _load_kmz_vertices(kmz_path: Path) -> list[tuple[float, float]]:
    if not kmz_path.is_file():
        raise BorderGeometryUnavailable(
            f"US/Canada border KMZ missing: {kmz_path} (BLOCKED_BY_DATASET)"
        )
    try:
        with zipfile.ZipFile(kmz_path) as kmz:
            kml_names = [
                info.filename
                for info in kmz.infolist()
                if info.filename.lower().endswith(".kml")
            ]
            if not kml_names:
                raise BorderGeometryUnavailable(
                    f"US/Canada border KMZ has no KML entry: {kmz_path}"
                )
            with kmz.open(kml_names[0]) as kml_fh:
                root = ET.parse(kml_fh).getroot()
    except BorderGeometryUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BorderGeometryUnavailable(
            f"US/Canada border KMZ unreadable: {kmz_path}: {exc}"
        ) from exc

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    vertices: list[tuple[float, float]] = []
    for coords_el in root.iter(f"{ns}coordinates"):
        vertices.extend(_parse_coord_text(coords_el.text))
    if len(vertices) < 2:
        raise BorderGeometryUnavailable(
            f"US/Canada border KMZ empty geometry: {kmz_path}"
        )
    return vertices


def border_vertices(*, kmz_path: Path | None = None) -> list[tuple[float, float]]:
    """Return cached (lon, lat) vertices from the composed boundaries provider."""
    global _border_vertices, _border_cache_key
    if kmz_path is not None:
        return _load_kmz_vertices(kmz_path)
    cache_key, vertices = _vertices_from_composed_provider()
    if _border_vertices is not None and _border_cache_key == cache_key:
        return _border_vertices
    _border_vertices = vertices
    _border_cache_key = cache_key
    return _border_vertices


def closest_canadian_border_point(
    latitude: float,
    longitude: float,
    max_dist_km: float,
    *,
    kmz_path: Path | None = None,
) -> tuple[float, float, float, float] | None:
    """Closest border vertex within ``max_dist_km``, or None.

    Returns ``(border_lat, border_lon, distance_km, bearing_deg)``.
    """
    # Coarse degree prefilter (~same idea as harness buffer).
    cos_lat = max(0.2, abs(math.cos(math.radians(latitude))))
    max_delta = (max_dist_km / (111.32 * cos_lat)) * 1.1
    best: tuple[float, float, float, float] | None = None  # dist, bearing, lat, lon
    for lon, lat in border_vertices(kmz_path=kmz_path):
        if abs(lat - latitude) > max_delta or abs(lon - longitude) > max_delta:
            continue
        dist = _haversine_km(latitude, longitude, lat, lon)
        if dist > max_dist_km:
            continue
        bearing = _initial_bearing_deg(latitude, longitude, lat, lon)
        if best is None or dist < best[0]:
            best = (dist, bearing, lat, lon)
    if best is None:
        return None
    dist, bearing, lat, lon = best
    return lat, lon, dist, bearing


def check_cbsd_in_border_sharing_zone(
    latitude: float,
    longitude: float,
    ant_azimuth: Any,
    ant_beamwidth: Any,
    *,
    kmz_path: Path | None = None,
) -> tuple[bool, float | None, float | None]:
    """Return ``(in_zone, border_lat, border_lon)`` — WINNF BPR sharing-zone rules."""
    closest = closest_canadian_border_point(
        latitude, longitude, SHARING_ZONE_MAX_KM, kmz_path=kmz_path
    )
    if closest is None:
        return False, None, None
    border_lat, border_lon, border_dist, border_bearing = closest
    if border_dist > SHARING_ZONE_MAX_KM:
        return False, None, None
    if border_dist <= SHARING_ZONE_INNER_KM:
        return True, border_lat, border_lon
    # 8–56 km: omni (missing / 0 / 360 beamwidth) always in zone; otherwise cone.
    if (
        ant_beamwidth is None
        or ant_azimuth is None
        or ant_beamwidth == 0
        or ant_beamwidth == 360
    ):
        return True, border_lat, border_lon
    try:
        azi = float(ant_azimuth)
        bw = float(ant_beamwidth)
    except (TypeError, ValueError):
        return True, border_lat, border_lon
    min_cone = border_bearing - 100.0
    max_cone = border_bearing + 100.0
    min_beam = azi - bw / 2.0
    max_beam = azi + bw / 2.0
    if (
        _angle_between(min_beam, min_cone, max_cone)
        or _angle_between(max_beam, min_cone, max_cone)
        or _angle_between(azi, min_cone, max_cone)
    ):
        return True, border_lat, border_lon
    return False, None, None
