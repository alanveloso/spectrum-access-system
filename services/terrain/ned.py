"""USGS NED 1″ GridFloat terrain provider (WInnForum Common-Data layout).

Tile layout matches ``reference_models.geo.terrain.TerrainDriver``:
- 1×1° float32 GridFloat with 6-pixel overlap (3612×3612)
- filenames ``usgs_ned_1_nXXwYYY_gridfloat_std.flt`` or ``floatnXXwYYY_1_std.flt``
- bilinear interpolation with half-pixel center offset
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

from services.terrain.exceptions import (
    TerrainCoordinateError,
    TerrainDataUnavailable,
    TerrainReadError,
)

_NUM_PIXEL_OVERLAP = 6
_TILE_BASE_DIM = 3600
_TILE_DIM = _TILE_BASE_DIM + 2 * _NUM_PIXEL_OVERLAP
_TILE_BYTES = _TILE_DIM * _TILE_DIM * 4
_NODATA_THRESHOLD = -900.0

# Default dataset label for cache keys / evidence.
DEFAULT_NED_DATASET_VERSION = "usgs_ned_1_arcsec_gridfloat_std"


def _tile_encoding(ilat: int, ilon: int) -> str:
    return "%c%02d%c%03d" % (
        "sn"[int(ilat >= 0)],
        abs(ilat),
        "we"[int(ilon >= 0)],
        abs(ilon),
    )


def _validate_coords(lat: float, lon: float) -> None:
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise TerrainCoordinateError(f"non-finite coordinates: lat={lat!r} lon={lon!r}")
    if lat < -90.0 or lat > 90.0:
        raise TerrainCoordinateError(f"latitude out of range: {lat}")
    if lon < -180.0 or lon > 180.0:
        raise TerrainCoordinateError(f"longitude out of range: {lon}")


class NedTerrainProvider:
    """Read USGS NED 1″ elevations from a local GridFloat directory."""

    def __init__(
        self,
        terrain_dir: Path | str,
        *,
        dataset_version: str = DEFAULT_NED_DATASET_VERSION,
        cache_size: int = 8,
    ) -> None:
        self._terrain_dir = Path(terrain_dir)
        self._dataset_version = dataset_version
        self._cache_size = max(1, cache_size)
        self._tile_cache: dict[tuple[int, int], list[float]] = {}
        self._tile_order: list[tuple[int, int]] = []

        if not self._terrain_dir.is_dir():
            raise TerrainDataUnavailable(
                f"terrain directory missing: {self._terrain_dir}"
            )

    @property
    def dataset_version(self) -> str:
        return self._dataset_version

    @property
    def terrain_dir(self) -> Path:
        return self._terrain_dir

    def elevation_m(self, lat: float, lon: float) -> float:
        _validate_coords(lat, lon)
        return self._elevation_bilinear(lat, lon)

    def elevations_m(self, lats: list[float], lons: list[float]) -> list[float]:
        if len(lats) != len(lons):
            raise TerrainCoordinateError("lat/lon list length mismatch")
        return [self.elevation_m(la, lo) for la, lo in zip(lats, lons)]

    def _tile_path(self, ilat: int, ilon: int) -> Path:
        encoding = _tile_encoding(ilat, ilon)
        candidates = (
            self._terrain_dir / f"usgs_ned_1_{encoding}_gridfloat_std.flt",
            self._terrain_dir / f"float{encoding}_1_std.flt",
        )
        for path in candidates:
            if path.is_file():
                return path
        raise TerrainDataUnavailable(
            f"NED tile missing for NW=({ilat},{ilon}): expected one of "
            f"{[p.name for p in candidates]} under {self._terrain_dir}"
        )

    def _load_tile(self, ilat: int, ilon: int) -> list[float]:
        key = (ilat, ilon)
        cached = self._tile_cache.get(key)
        if cached is not None:
            if key in self._tile_order:
                self._tile_order.remove(key)
            self._tile_order.append(key)
            return cached

        path = self._tile_path(ilat, ilon)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise TerrainReadError(f"cannot read NED tile {path}: {exc}") from exc
        if len(raw) != _TILE_BYTES:
            raise TerrainReadError(
                f"unexpected NED tile size for {path}: {len(raw)} != {_TILE_BYTES}"
            )
        try:
            values = list(struct.unpack(f"<{_TILE_DIM * _TILE_DIM}f", raw))
        except struct.error as exc:
            raise TerrainReadError(f"cannot decode NED tile {path}: {exc}") from exc

        self._tile_cache[key] = values
        self._tile_order.append(key)
        while len(self._tile_order) > self._cache_size:
            evict = self._tile_order.pop(0)
            self._tile_cache.pop(evict, None)
        return values

    def _sample(self, tile: list[float], row: int, col: int) -> float:
        if row < 0 or col < 0 or row >= _TILE_DIM or col >= _TILE_DIM:
            raise TerrainDataUnavailable(
                f"NED sample indices out of tile bounds: row={row} col={col}"
            )
        value = tile[row * _TILE_DIM + col]
        if value < _NODATA_THRESHOLD:
            return 0.0
        return float(value)

    def _elevation_bilinear(self, lat: float, lon: float) -> float:
        ilat = int(math.ceil(lat))
        ilon = int(math.floor(lon))
        tile = self._load_tile(ilat, ilon)

        float_x = _NUM_PIXEL_OVERLAP + _TILE_BASE_DIM * (lon - ilon) - 0.5
        float_y = _NUM_PIXEL_OVERLAP + _TILE_BASE_DIM * (ilat - lat) - 0.5
        xm = int(math.floor(float_x))
        ym = int(math.floor(float_y))
        xp = xm + 1
        yp = ym + 1
        alpha_x = float_x - xm
        alpha_y = float_y - ym

        ymxm = self._sample(tile, ym, xm)
        ymxp = self._sample(tile, ym, xp)
        ypxm = self._sample(tile, yp, xm)
        ypxp = self._sample(tile, yp, xp)

        return (
            alpha_x * alpha_y * ypxp
            + alpha_x * (1.0 - alpha_y) * ymxp
            + (1.0 - alpha_x) * (1.0 - alpha_y) * ymxm
            + (1.0 - alpha_x) * alpha_y * ypxm
        )
