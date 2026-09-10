"""Terrain / HAAT error taxonomy (fail-closed for Registration)."""

from __future__ import annotations


class TerrainError(Exception):
    """Base class for terrain/HAAT failures."""


class TerrainDataUnavailable(TerrainError):
    """NED tiles or dataset directory missing / incomplete."""


class TerrainCoordinateError(TerrainError):
    """Latitude/longitude outside valid geographic ranges."""


class TerrainReadError(TerrainError):
    """I/O or decode failure while reading a terrain tile."""
