"""Maximum / Largest Allowable PPA Contour (WINNF PCR RF).

Implements the reference-model semantics from
``reference_models.ppa.ppa`` (harness pin documented in evidence):

* per-CBSD RF coverage contour at −96 dBm / 10 MHz;
* hybrid path loss (median, reliability=0.5);
* standard antenna gains;
* maximum allowable EIRP (Cat A 30 / Cat B 47 dBm/10 MHz, or
 ``installationParam.eirpCapability`` when present);
* Hamming-smoothed radial distances 0.2…40 km;
* true geometric union of CBSD contours → one Feature
 (Polygon or MultiPolygon) via Shapely ``unary_union``.

Does **not** create a parallel propagation engine: callers inject
``PropagationEngines`` from ``load_reference_engines`` (or test doubles).

Fail-closed: missing engines / hybrid / Shapely / required install params →
``PpaRfContourError`` (never silent hull / first-polygon fallback).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from services.grant_service import CAT_A_MAX_EIRP_10MHZ, CAT_B_MAX_EIRP_10MHZ
from services.propagation.errors import PropagationUnavailableError
from services.terrain.vincenty import geodesic_point, geodesic_points

THRESHOLD_PER_10MHZ_DBM = -96.0
RX_HEIGHT_M = 1.5
RADIAL_STEP_KM = 0.2
RADIAL_MAX_KM = 40.0
HAMMING_WINDOW = 15


class PpaRfContourError(RuntimeError):
    """RF maximum PPA contour cannot be computed (fail-closed)."""


class _PathLoss(Protocol):
    db_loss: float


@dataclass(frozen=True)
class PpaRfEngines:
    """Minimal injectable backends for PPA RF contour (subset of PropagationEngines)."""

    calc_hybrid: Callable[..., _PathLoss]
    antenna_standard_gains: Callable[..., Any]
    region_type: Callable[[float, float], str] | None = None


def _distances_km() -> list[float]:
    n = int(RADIAL_MAX_KM / RADIAL_STEP_KM)
    return [RADIAL_STEP_KM * (i + 1) for i in range(n)]


def _hamming_filter(values: Sequence[float], window_len: int = HAMMING_WINDOW) -> list[float]:
    """Circular Hamming smoother matching reference_models.ppa.ppa._HammingFilter."""
    if window_len < 3 or len(values) == 0:
        return list(values)
    half = window_len // 2
    extended = list(values[-half:]) + list(values) + list(values[:half])
    # Hamming weights
    weights = [
        0.54 - 0.46 * math.cos(2.0 * math.pi * i / (window_len - 1))
        for i in range(window_len)
    ]
    wsum = sum(weights)
    weights = [w / wsum for w in weights]
    out: list[float] = []
    for i in range(len(values)):
        acc = 0.0
        for j, w in enumerate(weights):
            acc += extended[i + j] * w
        out.append(acc)
    return out


def _max_allowable_eirp_dbm_10mhz(device: dict[str, Any]) -> float:
    install = device.get("installationParam") or {}
    if "eirpCapability" in install and install["eirpCapability"] is not None:
        try:
            return float(install["eirpCapability"])
        except (TypeError, ValueError) as exc:
            raise PpaRfContourError("invalid_eirpCapability") from exc
    cat = str(device.get("cbsdCategory") or "A").upper()
    return CAT_A_MAX_EIRP_10MHZ if cat == "A" else CAT_B_MAX_EIRP_10MHZ


def _require_install(device: dict[str, Any]) -> dict[str, Any]:
    install = device.get("installationParam")
    if not isinstance(install, dict):
        raise PpaRfContourError("missing_installationParam")
    for key in ("latitude", "longitude", "height", "heightType", "antennaGain"):
        if key not in install:
            raise PpaRfContourError(f"missing_{key}")
    if "indoorDeployment" not in install:
        raise PpaRfContourError("missing_indoorDeployment")
    try:
        float(install["latitude"])
        float(install["longitude"])
        float(install["height"])
        float(install["antennaGain"])
    except (TypeError, ValueError) as exc:
        raise PpaRfContourError("invalid_installationParam") from exc
    if str(install["heightType"]).upper() not in {"AGL", "AMSL"}:
        raise PpaRfContourError("invalid_heightType")
    return install


def _radial_coverage_km(
    *,
    install: dict[str, Any],
    eirp_capability: float,
    antenna_gain_dir: float,
    region: str,
    radial_lats: Sequence[float],
    radial_lons: Sequence[float],
    engines: PpaRfEngines,
) -> float:
    """Return coverage distance (km) along one azimuth (count of steps ≥ threshold)."""
    lat_cbsd = float(install["latitude"])
    lon_cbsd = float(install["longitude"])
    height_cbsd = float(install["height"])
    peak_gain = float(install["antennaGain"])
    indoor = bool(install["indoorDeployment"])
    is_amsl = str(install["heightType"]).upper() == "AMSL"
    count = 0
    for lat, lon in zip(radial_lats, radial_lons):
        try:
            result = engines.calc_hybrid(
                lat_cbsd,
                lon_cbsd,
                height_cbsd,
                float(lat),
                float(lon),
                RX_HEIGHT_M,
                cbsd_indoor=indoor,
                reliability=0.5,
                region=region,
                is_height_cbsd_amsl=is_amsl,
            )
            db_loss = float(result.db_loss)
        except PropagationUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PpaRfContourError(f"hybrid_path_loss_failed:{type(exc).__name__}") from exc
        received = (eirp_capability - peak_gain + float(antenna_gain_dir)) - db_loss
        if received >= THRESHOLD_PER_10MHZ_DBM:
            count += 1
    return count * RADIAL_STEP_KM


def cbsd_rf_contour_ring(
    device: dict[str, Any], *, engines: PpaRfEngines
) -> list[list[float]]:
    """Return a closed GeoJSON ring ``[[lon, lat], ...]`` for one CBSD."""
    install = _require_install(device)
    eirp = _max_allowable_eirp_dbm_10mhz(device)
    lat0 = float(install["latitude"])
    lon0 = float(install["longitude"])
    distances = _distances_km()
    azimuths = list(range(360))

    try:
        gains = engines.antenna_standard_gains(
            azimuths,
            install.get("antennaAzimuth"),
            install.get("antennaBeamwidth"),
            float(install["antennaGain"]),
        )
    except Exception as exc:  # noqa: BLE001
        raise PpaRfContourError(f"antenna_gain_failed:{type(exc).__name__}") from exc

    # Normalize gains to a sequence of 360 floats.
    if isinstance(gains, (int, float)):
        gain_list = [float(gains)] * 360
    else:
        gain_list = [float(g) for g in gains]
        if len(gain_list) != 360:
            raise PpaRfContourError("antenna_gain_length")

    if engines.region_type is not None:
        try:
            region = str(engines.region_type(lat0, lon0) or "SUBURBAN")
        except Exception as exc:  # noqa: BLE001
            raise PpaRfContourError(f"region_type_failed:{type(exc).__name__}") from exc
    else:
        region = "SUBURBAN"

    contour_dists: list[float] = []
    for az, g_dir in zip(azimuths, gain_list):
        rlats, rlons = geodesic_points(lat0, lon0, distances, float(az))
        contour_dists.append(
            _radial_coverage_km(
                install=install,
                eirp_capability=eirp,
                antenna_gain_dir=g_dir,
                region=region,
                radial_lats=rlats,
                radial_lons=rlons,
                engines=engines,
            )
        )

    smoothed = _hamming_filter(contour_dists)
    ring: list[list[float]] = []
    for dist_km, az in zip(smoothed, azimuths):
        d = max(float(dist_km), RADIAL_STEP_KM)
        plat, plon = geodesic_point(lat0, lon0, d, float(az))
        ring.append([plon, plat])
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    if len(ring) < 4:
        raise PpaRfContourError("degenerate_rf_contour")
    return ring


def union_geojson_polygons(geometries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """True topological union of Polygon/MultiPolygon GeoJSON dicts.

 Returns a GeoJSON geometry dict of type ``Polygon`` or ``MultiPolygon``.
 Fail-closed: never convex-hull, never pick-first, never drop components.
 Uses the product runtime Shapely dependency (not the WInnForum harness).
 """
    if not geometries:
        raise PpaRfContourError("empty_union_input")
    try:
        from shapely.geometry import MultiPolygon, mapping, shape
        from shapely.ops import unary_union
    except ImportError as exc:
        raise PpaRfContourError("shapely_unavailable") from exc

    polys: list[Any] = []
    for geom in geometries:
        if not isinstance(geom, dict):
            raise PpaRfContourError("invalid_union_geometry")
        gtype = geom.get("type")
        if gtype not in {"Polygon", "MultiPolygon"}:
            raise PpaRfContourError("invalid_union_geometry")
        try:
            sh = shape(geom)
        except (TypeError, ValueError) as exc:
            raise PpaRfContourError("invalid_union_geometry") from exc
        # Match reference contour hygiene (buffer(0)); not a hull substitute.
        if not sh.is_valid:
            sh = sh.buffer(0)
        if sh.is_empty:
            raise PpaRfContourError("empty_union_geometry")
        if sh.geom_type == "Polygon":
            if len(sh.exterior.coords) < 4:
                raise PpaRfContourError("degenerate_union_geometry")
            polys.append(sh)
        elif sh.geom_type == "MultiPolygon":
            for part in sh.geoms:
                if part.is_empty or len(part.exterior.coords) < 4:
                    raise PpaRfContourError("degenerate_union_geometry")
                polys.append(part)
        else:
            raise PpaRfContourError(f"non_polygonal_geometry:{sh.geom_type}")

    try:
        united = unary_union(polys)
    except Exception as exc:  # noqa: BLE001
        raise PpaRfContourError(f"union_failed:{type(exc).__name__}") from exc

    if united is None or united.is_empty:
        raise PpaRfContourError("empty_union_result")

    if united.geom_type == "GeometryCollection":
        flat: list[Any] = []
        for part in united.geoms:
            if part.geom_type == "Polygon" and not part.is_empty:
                flat.append(part)
            elif part.geom_type == "MultiPolygon":
                flat.extend([p for p in part.geoms if not p.is_empty])
        if not flat:
            raise PpaRfContourError("non_polygonal_union_result")
        united = flat[0] if len(flat) == 1 else MultiPolygon(flat)

    if united.geom_type not in {"Polygon", "MultiPolygon"}:
        raise PpaRfContourError(f"non_polygonal_union_result:{united.geom_type}")

    mapped = mapping(united)
    if not isinstance(mapped, dict):
        raise PpaRfContourError("non_dict_geojson_mapping")
    return mapped


def _union_feature(geometry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"source": "ppa_rf_contour"},
        "geometry": geometry,
    }


def maximum_rf_ppa_contour(
    devices: Sequence[dict[str, Any]], *, engines: PpaRfEngines
) -> dict[str, Any]:
    """Union of per-CBSD RF contours as a one-feature GeoJSON FeatureCollection.

 Each CBSD produces an independent RF ring; rings are then topologically
 unioned into a single ``Polygon`` or ``MultiPolygon`` feature. Never returns
 one Feature per CBSD.
 """
    if not devices:
        raise PpaRfContourError("empty_cluster")
    polygons: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict):
            raise PpaRfContourError("invalid_device")
        ring = cbsd_rf_contour_ring(device, engines=engines)
        polygons.append({"type": "Polygon", "coordinates": [ring]})
    geometry = union_geojson_polygons(polygons)
    return {
        "type": "FeatureCollection",
        "features": [_union_feature(geometry)],
    }


def load_default_ppa_rf_engines() -> PpaRfEngines:
    """Load hybrid/antenna backends from the harness reference models.

 Raises ``PpaRfContourError`` when the reference stack is unavailable
 (no silent Free-Space / hull substitute).
 """
    try:
        from services.propagation.engines import load_reference_engines

        engines = load_reference_engines()
    except PropagationUnavailableError as exc:
        raise PpaRfContourError(f"rf_engines_unavailable:{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PpaRfContourError(f"rf_engines_unavailable:{type(exc).__name__}") from exc

    region_fn: Callable[[float, float], str] | None = None
    if engines.region_nlcd_vote is not None:
        def _region(lat: float, lon: float) -> str:
            # RegionNlcdVote returns a region string in harness usage paths.
            try:
                return str(engines.region_nlcd_vote([(lat, lon)], 1) or "SUBURBAN")
            except TypeError:
                return str(engines.region_nlcd_vote(lat, lon) or "SUBURBAN")
            except Exception as exc:  # noqa: BLE001
                raise PpaRfContourError(f"nlcd_region_failed:{type(exc).__name__}") from exc

        region_fn = _region

    return PpaRfEngines(
        calc_hybrid=engines.calc_hybrid,
        antenna_standard_gains=engines.antenna_standard_gains,
        region_type=region_fn,
    )


def cbsd_orm_to_ppa_device(cbsd: Any, *, registration: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a reference-model-like CBSD device dict from ORM + registration JSON."""
    import json

    if registration is None:
        try:
            registration = json.loads(cbsd.registration_json or "{}")
        except json.JSONDecodeError as exc:
            raise PpaRfContourError("invalid_registration_json") from exc
    if not isinstance(registration, dict):
        raise PpaRfContourError("invalid_registration_json")
    install = registration.get("installationParam")
    if not isinstance(install, dict):
        raise PpaRfContourError("missing_installationParam")
    cat = str(
        registration.get("cbsdCategory") or getattr(cbsd, "cbsd_category", None) or "A"
    ).upper()
    return {
        "cbsdCategory": cat,
        "installationParam": dict(install),
    }
