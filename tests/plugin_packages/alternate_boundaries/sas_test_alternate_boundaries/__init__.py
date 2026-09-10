"""Deterministic in-memory boundaries provider for provider substitutability tests."""

from __future__ import annotations

from dataclasses import dataclass

from primitives.geography import GeoPoint
from providers.contract import (
    CAPABILITY_BOUNDARIES,
    DataKind,
    DatasetProvenance,
    LonLatVerticesRecord,
    PROVIDER_API_VERSION,
    TOKEN_US_CANADA_BORDER,
)

LEGO_BOUNDARY_VERTICES: tuple[tuple[float, float], ...] = (
    (-95.0, 48.0),
    (-95.0, 48.5),
    (-94.5, 48.5),
    (-94.5, 48.0),
)

_LEGO_PROVENANCE = DatasetProvenance(
    dataset_id="mapping_boundaries_lego",
    dataset_version="1.0.0",
    provider_id="mapping_boundaries",
)


@dataclass(frozen=True, slots=True)
class MappingBoundariesProvider:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.BOUNDARIES

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_BOUNDARIES})

    def provenance(self) -> DatasetProvenance:
        return _LEGO_PROVENANCE

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> LonLatVerticesRecord:
        if token != TOKEN_US_CANADA_BORDER:
            raise ValueError(f"unsupported boundaries token {token!r}")
        return LonLatVerticesRecord(
            vertices=LEGO_BOUNDARY_VERTICES,
            provenance=self.provenance(),
        )


def mapping_boundaries_provider() -> MappingBoundariesProvider:
    return MappingBoundariesProvider()
