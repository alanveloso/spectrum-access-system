"""Data provider contract.

Generic terrain / land cover / entities / rights / boundaries / reference data.
Existing raster loaders stay in services; this package does not wrap them.
RF models are out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing

PROVIDER_API_VERSION = "1.0.0"

CAPABILITY_TERRAIN = "terrain"
CAPABILITY_LAND_COVER = "land_cover"
CAPABILITY_PROTECTED_ENTITIES = "protected_entities"
CAPABILITY_RIGHTS = "rights"
CAPABILITY_BOUNDARIES = "boundaries"
CAPABILITY_REFERENCE_DATA = "reference_data"

DATA_CAPABILITIES = frozenset(
    {
        CAPABILITY_TERRAIN,
        CAPABILITY_LAND_COVER,
        CAPABILITY_PROTECTED_ENTITIES,
        CAPABILITY_RIGHTS,
        CAPABILITY_BOUNDARIES,
        CAPABILITY_REFERENCE_DATA,
    }
)


class DataKind(StrEnum):
    TERRAIN = CAPABILITY_TERRAIN
    LAND_COVER = CAPABILITY_LAND_COVER
    PROTECTED_ENTITIES = CAPABILITY_PROTECTED_ENTITIES
    RIGHTS = CAPABILITY_RIGHTS
    BOUNDARIES = CAPABILITY_BOUNDARIES
    REFERENCE_DATA = CAPABILITY_REFERENCE_DATA


@dataclass(frozen=True, slots=True)
class DatasetProvenance:
    """Dataset identity recorded on decisions (D4 dataset_versions)."""

    dataset_id: str
    dataset_version: str
    provider_id: str

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or not self.dataset_version.strip():
            raise ValueError("dataset_id and dataset_version are required")
        if not self.provider_id.strip():
            raise ValueError("provider_id is required")

    def as_pair(self) -> tuple[str, str]:
        return (self.dataset_id, self.dataset_version)


@dataclass(frozen=True, slots=True)
class TerrainRecord:
    elevation_m: float
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class LandCoverRecord:
    class_code: int
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class FeatureIdsRecord:
    feature_ids: tuple[str, ...]
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class ReferenceBandRecord:
    band_id: str
    frequency: FrequencyRange
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class GeoJsonRecord:
    document: dict
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class CoordinateListRecord:
    """Geographic points as ``(latitude_deg, longitude_deg)`` pairs."""

    coordinates: tuple[tuple[float, float], ...]
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class LonLatVerticesRecord:
    """Polyline vertices as ``(longitude_deg, latitude_deg)`` pairs."""

    vertices: tuple[tuple[float, float], ...]
    provenance: DatasetProvenance


@dataclass(frozen=True, slots=True)
class ResourcePathsRecord:
    """Resolved absolute paths supplied by the provider (consumer may parse files)."""

    paths: tuple[str, ...]
    provenance: DatasetProvenance


# Stable resource tokens for token-based fetch (not profile/deployment ids).
TOKEN_US_CANADA_BORDER = "us_canada_border"
TOKEN_FCC_FIELD_OFFICES = "fcc_field_offices"
TOKEN_DPA_KML_CATALOGUE = "dpa_kml_catalogue"
TOKEN_NTIA_PROTECTION_ZONES = "ntia_protection_zones"
COUNTY_TOKEN_PREFIX = "county:"


ProviderRecord = (
    TerrainRecord
    | LandCoverRecord
    | FeatureIdsRecord
    | ReferenceBandRecord
    | GeoJsonRecord
    | CoordinateListRecord
    | LonLatVerticesRecord
    | ResourcePathsRecord
)


def providers_meet_requirements(
    providers: tuple["DataProvider", ...], required: tuple[str, ...]
) -> None:
    have = set()
    for provider in providers:
        have |= set(provider.advertised_capabilities())
    missing = [cap for cap in required if cap not in have]
    if missing:
        raise ValueError(f"data providers missing required capabilities: {missing}")


@runtime_checkable
class DataProvider(Protocol):
    """Trusted operator plugin. Missing coverage must fail closed."""

    @property
    def api_version(self) -> str: ...

    @property
    def kind(self) -> DataKind: ...

    def advertised_capabilities(self) -> frozenset[str]: ...

    def provenance(self) -> DatasetProvenance: ...

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> ProviderRecord: ...


def _require_point(point: GeoPoint | None) -> GeoPoint:
    if point is None:
        raise ValueError("point is required")
    return point


class MappingTerrainProvider:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.TERRAIN

    def __init__(
        self,
        samples: MappingGeo,
        provenance: DatasetProvenance,
    ) -> None:
        if not samples:
            raise ValueError("terrain samples are required")
        self._samples = samples
        self._provenance = provenance

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_TERRAIN})

    def provenance(self) -> DatasetProvenance:
        return self._provenance

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> TerrainRecord:
        loc = _require_point(point)
        key = (loc.latitude_deg, loc.longitude_deg)
        if key not in self._samples:
            raise ValueError("terrain coverage missing for point")
        return TerrainRecord(elevation_m=self._samples[key], provenance=self._provenance)


class MappingLandCoverProvider:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.LAND_COVER

    def __init__(
        self, samples: MappingGeoInt, provenance: DatasetProvenance
    ) -> None:
        if not samples:
            raise ValueError("land_cover samples are required")
        self._samples = samples
        self._provenance = provenance

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_LAND_COVER})

    def provenance(self) -> DatasetProvenance:
        return self._provenance

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> LandCoverRecord:
        loc = _require_point(point)
        key = (loc.latitude_deg, loc.longitude_deg)
        if key not in self._samples:
            raise ValueError("land_cover coverage missing for point")
        return LandCoverRecord(class_code=self._samples[key], provenance=self._provenance)


class MappingFeatureProvider:
    """Point-in-ring features for protected_entities, rights, or boundaries."""

    api_version = PROVIDER_API_VERSION

    def __init__(
        self,
        kind: DataKind,
        features: tuple[tuple[str, LinearRing], ...],
        provenance: DatasetProvenance,
    ) -> None:
        if kind not in (
            DataKind.PROTECTED_ENTITIES,
            DataKind.RIGHTS,
            DataKind.BOUNDARIES,
        ):
            raise ValueError("feature provider kind must be entities, rights, or boundaries")
        if not features:
            raise ValueError("features are required")
        self._kind = kind
        self._features = features
        self._provenance = provenance

    @property
    def kind(self) -> DataKind:
        return self._kind

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({self._kind.value})

    def provenance(self) -> DatasetProvenance:
        return self._provenance

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> FeatureIdsRecord:
        loc = _require_point(point)
        ids = tuple(fid for fid, ring in self._features if ring.contains(loc))
        return FeatureIdsRecord(feature_ids=ids, provenance=self._provenance)


class MappingReferenceProvider:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.REFERENCE_DATA

    def __init__(
        self,
        bands: dict[str, FrequencyRange],
        provenance: DatasetProvenance,
    ) -> None:
        if not bands:
            raise ValueError("reference bands are required")
        self._bands = bands
        self._provenance = provenance

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_REFERENCE_DATA})

    def provenance(self) -> DatasetProvenance:
        return self._provenance

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> ReferenceBandRecord:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required")
        if token not in self._bands:
            raise ValueError(f"reference band missing: {token}")
        return ReferenceBandRecord(
            band_id=token,
            frequency=self._bands[token],
            provenance=self._provenance,
        )


# Local aliases so MappingTerrainProvider annotations work without importing Mapping.
MappingGeo = dict[tuple[float, float], float]
MappingGeoInt = dict[tuple[float, float], int]
