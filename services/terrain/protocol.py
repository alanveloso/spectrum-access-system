"""Injectable TerrainProvider / HaatProvider contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TerrainProvider(Protocol):
    """Elevation lookup backed by a versioned terrain dataset."""

    @property
    def dataset_version(self) -> str:
        """Opaque dataset version included in HAAT cache keys."""

    def elevation_m(self, lat: float, lon: float) -> float:
        """Return terrain elevation (meters AMSL) at ``lat``/``lon``."""


@runtime_checkable
class HaatProvider(Protocol):
    """Height Above Average Terrain calculator (WInnForum / Part 96)."""

    @property
    def dataset_version(self) -> str:
        """Opaque dataset version included in HAAT cache keys."""

    def compute_haat_m(
        self,
        lat: float,
        lon: float,
        height_m: float,
        *,
        height_is_agl: bool = True,
    ) -> float:
        """Return CBSD HAAT in meters."""
