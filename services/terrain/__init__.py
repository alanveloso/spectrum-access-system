"""Terrain elevation and HAAT providers for Cat A outdoor Registration."""

from __future__ import annotations

from services.terrain.exceptions import (
    TerrainCoordinateError,
    TerrainDataUnavailable,
    TerrainError,
    TerrainReadError,
)
from services.terrain.fake import CallableTerrainProvider, DeterministicHaatProvider
from services.terrain.haat import (
    CAT_A_OUTDOOR_HAAT_LIMIT_M,
    HAAT_NED_ABS_TOL_M,
    HAAT_REPEATABILITY_ABS_TOL_M,
    HAAT_SYNTHETIC_ABS_TOL_M,
    CachedHaatProvider,
    WinnForumHaatProvider,
    get_haat_provider,
    last_haat_rejection_reason,
    reset_haat_provider,
    set_haat_provider,
)
from services.terrain.ned import NedTerrainProvider
from services.terrain.protocol import HaatProvider, TerrainProvider

__all__ = [
    "CAT_A_OUTDOOR_HAAT_LIMIT_M",
    "CallableTerrainProvider",
    "CachedHaatProvider",
    "DeterministicHaatProvider",
    "HAAT_NED_ABS_TOL_M",
    "HAAT_REPEATABILITY_ABS_TOL_M",
    "HAAT_SYNTHETIC_ABS_TOL_M",
    "HaatProvider",
    "NedTerrainProvider",
    "TerrainCoordinateError",
    "TerrainDataUnavailable",
    "TerrainError",
    "TerrainProvider",
    "TerrainReadError",
    "WinnForumHaatProvider",
    "get_haat_provider",
    "last_haat_rejection_reason",
    "reset_haat_provider",
    "set_haat_provider",
]
