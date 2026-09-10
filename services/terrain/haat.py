"""WInnForum HAAT (Height Above Average Terrain) for Cat A outdoor Registration.

Algorithm mirrors harness ``TerrainDriver.ComputeNormalizedHaat`` +
``wf_itm.ComputeHaat`` and applies to **any** WGS84 coordinate (no street /
fixture lookup tables):

- 8 radials at bearings 0°, 45°, …, 315°
- 50 sample distances from 3 km to 16 km inclusive
- ``norm_haat = elev_site - mean(radial_elevations)``
- AGL: ``haat = height + norm_haat``
- AMSL: ``haat = height - elev_site + norm_haat``

Part 96 / WINNF REG.7: Category A outdoor HAAT must be ≤ 6 m.

Numerical tolerances (documented for regression / reference comparison):

- ``HAAT_SYNTHETIC_ABS_TOL_M`` — analytic terrain (exact arithmetic)
- ``HAAT_NED_ABS_TOL_M`` — USGS NED float32 GridFloat + bilinear vs recorded refs
- ``HAAT_REPEATABILITY_ABS_TOL_M`` — same inputs must be bit-identical
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import Callable

from services.terrain.exceptions import (
    TerrainCoordinateError,
    TerrainDataUnavailable,
    TerrainError,
)
from services.terrain.protocol import HaatProvider, TerrainProvider
from services.terrain.vincenty import geodesic_points
from runtime.data_access import try_composed_provider

# 47 CFR § 96.43 — Category A outdoor HAAT limit (meters).
CAT_A_OUTDOOR_HAAT_LIMIT_M = 6.0

# Documented absolute tolerances (meters) for sample suites.
HAAT_SYNTHETIC_ABS_TOL_M = 1e-9
HAAT_NED_ABS_TOL_M = 1e-3
HAAT_REPEATABILITY_ABS_TOL_M = 0.0

_RADIAL_BEARINGS_DEG = [i * 45.0 for i in range(8)]
_DISTANCES_KM = [3.0 + (16.0 - 3.0) * i / 49.0 for i in range(50)]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_TERRAIN_DIR = _REPO_ROOT / "data" / "geo" / "ned"

_provider_lock = threading.RLock()
_haat_provider: HaatProvider | None = None


def resolve_terrain_dir(explicit: Path | str | None = None) -> Path:
    """Resolve NED directory: explicit → SAS_TERRAIN_DIR → repo ``data/geo/ned``."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("SAS_TERRAIN_DIR") or os.environ.get("TERRAIN_DIR")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_TERRAIN_DIR


def resolve_ned_dataset_version(terrain_dir: Path | str) -> str:
    """Resolve dataset version label for cache keys / evidence.

 Precedence: non-empty ``SAS_TERRAIN_DATASET_VERSION`` → ``VERSION`` marker
 under ``terrain_dir`` → ``DEFAULT_NED_DATASET_VERSION``.
 """
    env = os.environ.get("SAS_TERRAIN_DATASET_VERSION")
    if env is not None:
        cleaned = env.strip()
        if cleaned:
            return cleaned
    marker = Path(terrain_dir) / "VERSION"
    if marker.is_file():
        lines = marker.read_text(encoding="utf-8").strip().splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()
    from services.terrain.ned import DEFAULT_NED_DATASET_VERSION

    return DEFAULT_NED_DATASET_VERSION


def _validate_coords(lat: float, lon: float) -> None:
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise TerrainCoordinateError(f"non-finite coordinates: lat={lat!r} lon={lon!r}")
    if lat < -90.0 or lat > 90.0:
        raise TerrainCoordinateError(f"latitude out of range: {lat}")
    if lon < -180.0 or lon > 180.0:
        raise TerrainCoordinateError(f"longitude out of range: {lon}")


class WinnForumHaatProvider:
    """HAAT calculator using a real ``TerrainProvider`` (typically NED)."""

    def __init__(self, terrain: TerrainProvider) -> None:
        self._terrain = terrain

    @property
    def dataset_version(self) -> str:
        return self._terrain.dataset_version

    @property
    def terrain(self) -> TerrainProvider:
        return self._terrain

    def compute_normalized_haat_m(self, lat: float, lon: float) -> tuple[float, float]:
        """Return ``(norm_haat_m, ground_elevation_m)`` at the site."""
        _validate_coords(lat, lon)
        all_lats = [lat]
        all_lons = [lon]
        for bearing in _RADIAL_BEARINGS_DEG:
            rlats, rlons = geodesic_points(lat, lon, _DISTANCES_KM, bearing)
            all_lats.extend(rlats)
            all_lons.extend(rlons)

        elevations = [
            self._terrain.elevation_m(la, lo) for la, lo in zip(all_lats, all_lons)
        ]
        site = elevations[0]
        radial_mean = sum(elevations[1:]) / float(len(elevations) - 1)
        return site - radial_mean, site

    def compute_haat_m(
        self,
        lat: float,
        lon: float,
        height_m: float,
        *,
        height_is_agl: bool = True,
    ) -> float:
        if not math.isfinite(height_m):
            raise TerrainCoordinateError(f"non-finite height: {height_m!r}")
        norm_haat, alt_ground = self.compute_normalized_haat_m(lat, lon)
        if height_is_agl:
            return float(height_m) + norm_haat
        return float(height_m) - alt_ground + norm_haat


class CachedHaatProvider:
    """Optional memoization; cache key includes coords, height params, dataset version."""

    def __init__(self, inner: HaatProvider, *, max_entries: int = 256) -> None:
        self._inner = inner
        self._max_entries = max(1, max_entries)
        self._cache: dict[tuple, float] = {}
        self._order: list[tuple] = []
        self._lock = threading.RLock()

    @property
    def dataset_version(self) -> str:
        return self._inner.dataset_version

    @property
    def inner(self) -> HaatProvider:
        return self._inner

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._order.clear()

    def cache_info(self) -> tuple[int, int]:
        with self._lock:
            return len(self._cache), self._max_entries

    def _key(
        self,
        lat: float,
        lon: float,
        height_m: float,
        height_is_agl: bool,
    ) -> tuple:
        return (
            round(float(lat), 8),
            round(float(lon), 8),
            round(float(height_m), 6),
            bool(height_is_agl),
            self._inner.dataset_version,
        )

    def compute_haat_m(
        self,
        lat: float,
        lon: float,
        height_m: float,
        *,
        height_is_agl: bool = True,
    ) -> float:
        key = self._key(lat, lon, height_m, height_is_agl)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                if key in self._order:
                    self._order.remove(key)
                self._order.append(key)
                return hit

        value = self._inner.compute_haat_m(
            lat, lon, height_m, height_is_agl=height_is_agl
        )
        with self._lock:
            self._cache[key] = value
            self._order.append(key)
            while len(self._order) > self._max_entries:
                evict = self._order.pop(0)
                self._cache.pop(evict, None)
        return value


class _UnavailableHaatProvider:
    """Fail-closed stand-in when the NED directory is not provisioned."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._dataset_version = "unavailable"

    @property
    def dataset_version(self) -> str:
        return self._dataset_version

    def compute_haat_m(
        self,
        lat: float,
        lon: float,
        height_m: float,
        *,
        height_is_agl: bool = True,
    ) -> float:
        _ = (lat, lon, height_m, height_is_agl)
        raise TerrainDataUnavailable(self._reason)


def build_default_haat_provider(
    terrain_dir: Path | str | None = None,
    *,
    cached: bool = True,
) -> HaatProvider:
    """Construct the production NED-backed HAAT provider."""
    if terrain_dir is None:
        terrain_provider = try_composed_provider("terrain")
        if terrain_provider is not None:
            backend = getattr(terrain_provider, "elevation_backend", None)
            if backend is not None:
                provider: HaatProvider = WinnForumHaatProvider(backend)
                if cached:
                    provider = CachedHaatProvider(provider)
                return provider
    from services.terrain.ned import NedTerrainProvider

    directory = resolve_terrain_dir(terrain_dir)
    # Dataset version helper lives in this module (not ned.py).
    version = resolve_ned_dataset_version(directory)
    try:
        terrain = NedTerrainProvider(directory, dataset_version=version)
    except TerrainDataUnavailable as exc:
        return _UnavailableHaatProvider(str(exc))
    provider = WinnForumHaatProvider(terrain)
    if cached:
        provider = CachedHaatProvider(provider)
    return provider


def get_haat_provider() -> HaatProvider:
    """Process-wide HAAT provider (lazy NED default)."""
    global _haat_provider
    with _provider_lock:
        if _haat_provider is None:
            _haat_provider = build_default_haat_provider()
        return _haat_provider


def set_haat_provider(provider: HaatProvider | None) -> None:
    """Inject a provider (tests) or clear to rebuild default (``None``)."""
    global _haat_provider
    with _provider_lock:
        _haat_provider = provider


def reset_haat_provider() -> None:
    set_haat_provider(None)


def haat_exceeds_cat_a_outdoor_limit(
    lat: float,
    lon: float,
    height_m: float,
    *,
    height_is_agl: bool = True,
    limit_m: float = CAT_A_OUTDOOR_HAAT_LIMIT_M,
    provider: HaatProvider | None = None,
) -> bool:
    """Return True when computed HAAT is strictly greater than ``limit_m``."""
    active = provider if provider is not None else get_haat_provider()
    haat = active.compute_haat_m(lat, lon, height_m, height_is_agl=height_is_agl)
    return haat > limit_m


# Last Cat-A outdoor HAAT decision detail for diagnostics/tests (not API).
_last_haat_rejection_reason: str | None = None


def last_haat_rejection_reason() -> str | None:
    """Return the most recent Cat-A outdoor HAAT rejection reason, if any."""
    return _last_haat_rejection_reason


def cat_a_outdoor_haat_invalid(
    installation: dict,
    *,
    provider_factory: Callable[[], HaatProvider] | None = None,
) -> bool:
    """True when Cat A outdoor HAAT validation must reject (103).

    Fail-closed: terrain errors and missing elevation become rejection.
    """
    global _last_haat_rejection_reason
    _last_haat_rejection_reason = None
    try:
        lat = float(installation["latitude"])
        lon = float(installation["longitude"])
        height = float(installation["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TerrainCoordinateError("installation coordinates/height invalid") from exc

    height_type = installation.get("heightType") or "AGL"
    height_is_agl = height_type != "AMSL"
    factory = provider_factory or get_haat_provider
    try:
        exceeds = haat_exceeds_cat_a_outdoor_limit(
            lat,
            lon,
            height,
            height_is_agl=height_is_agl,
            provider=factory(),
        )
        if exceeds:
            _last_haat_rejection_reason = (
                f"cat_a_outdoor_haat_exceeds_limit height={height} "
                f"heightType={height_type} lat={lat} lon={lon}"
            )
        return exceeds
    except TerrainError as exc:
        # Missing tiles / read errors / bad coords from provider: reject Registration.
        _last_haat_rejection_reason = (
            f"cat_a_outdoor_haat_terrain_unavailable:{type(exc).__name__}:{exc}"
        )
        return True
