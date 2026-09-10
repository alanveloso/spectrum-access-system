"""Reference protection-data bundle providers.

Implementation-owned dataset layout lives here. Regulatory services consume
capabilities through ``DataProvider.fetch`` and must not resolve bundle paths.
"""

from __future__ import annotations

import csv
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import get_settings
from primitives.geography import GeoPoint
from protection_data.loader import assert_protection_data_ready, get_data_root
from providers.bundle_context import BundleContext
from providers.contract import (
    CAPABILITY_BOUNDARIES,
    CAPABILITY_LAND_COVER,
    CAPABILITY_REFERENCE_DATA,
    CAPABILITY_RIGHTS,
    CAPABILITY_TERRAIN,
    COUNTY_TOKEN_PREFIX,
    CoordinateListRecord,
    DataKind,
    DatasetProvenance,
    FeatureIdsRecord,
    GeoJsonRecord,
    LandCoverRecord,
    LonLatVerticesRecord,
    PROVIDER_API_VERSION,
    ResourcePathsRecord,
    TOKEN_DPA_KML_CATALOGUE,
    TOKEN_FCC_FIELD_OFFICES,
    TOKEN_NTIA_PROTECTION_ZONES,
    TOKEN_US_CANADA_BORDER,
    TerrainRecord,
)

_DPA_KML_NAMES = ("E-DPAs.kml", "P-DPAs.kml")
_KML_NS = "{http://www.opengis.net/kml/2.2}"


def _settings_bundle_id() -> str:
    return get_settings().sas_protection_data_bundle


def _default_context() -> BundleContext:
    return BundleContext.load(_settings_bundle_id(), data_root=get_data_root())


def _provenance(ctx: BundleContext, provider_id: str) -> DatasetProvenance:
    bundle_id, version, pid = ctx.provenance_base(provider_id=provider_id)
    return DatasetProvenance(
        dataset_id=bundle_id, dataset_version=version, provider_id=pid
    )


def _parse_kml_coord_text(text: str | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            out.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return out


def _load_kmz_vertices(kmz_path: Path) -> tuple[tuple[float, float], ...]:
    if not kmz_path.is_file():
        raise ValueError(f"border KMZ missing: {kmz_path}")
    with zipfile.ZipFile(kmz_path) as kmz:
        kml_names = [
            info.filename
            for info in kmz.infolist()
            if info.filename.lower().endswith(".kml")
        ]
        if not kml_names:
            raise ValueError(f"border KMZ has no KML entry: {kmz_path}")
        with kmz.open(kml_names[0]) as kml_fh:
            root = ET.parse(kml_fh).getroot()
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    vertices: list[tuple[float, float]] = []
    for coords_el in root.iter(f"{ns}coordinates"):
        vertices.extend(_parse_kml_coord_text(coords_el.text))
    if len(vertices) < 2:
        raise ValueError(f"border KMZ empty geometry: {kmz_path}")
    return tuple(vertices)


def _load_fcc_offices_csv(csv_path: Path) -> tuple[tuple[float, float], ...]:
    if not csv_path.is_file():
        raise ValueError(f"FCC office CSV missing: {csv_path}")
    offices: list[tuple[float, float]] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                offices.append((float(row["latitude"]), float(row["longitude"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not offices:
        raise ValueError("FCC office CSV empty or unreadable")
    return tuple(offices)


def _load_county_geojson(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"county geojson missing: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"county geojson unreadable: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError("county geojson root must be an object")
    return payload


def _ntia_coastal_geojson(kml_path: Path) -> dict[str, Any]:
    if not kml_path.is_file():
        return {"type": "FeatureCollection", "features": []}
    root = ET.parse(kml_path).getroot()
    wanted = {"West Combined Contour", "East-Gulf Combined Contour"}
    features: list[dict[str, Any]] = []
    for pm in root.iter(f"{_KML_NS}Placemark"):
        name_el = pm.find(f"{_KML_NS}name")
        if name_el is None or name_el.text not in wanted:
            continue
        outer = pm.find(
            f".//{_KML_NS}outerBoundaryIs/{_KML_NS}LinearRing/{_KML_NS}coordinates"
        )
        coords_el = outer if outer is not None else pm.find(f".//{_KML_NS}coordinates")
        ring = _parse_kml_coord_text(
            coords_el.text if coords_el is not None else None
        )
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        features.append(
            {
                "type": "Feature",
                "properties": {"name": name_el.text},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
        wanted.discard(name_el.text)
        if not wanted:
            break
    return {"type": "FeatureCollection", "features": features}


@dataclass(frozen=True, slots=True)
class ProtectionBundleTerrainProvider:
    """Terrain elevation from the bundle ``terrain_ned`` slot."""

    api_version = PROVIDER_API_VERSION
    kind = DataKind.TERRAIN

    _ctx: BundleContext
    _ned: Any

    def __init__(
        self,
        *,
        bundle_id: str | None = None,
        data_root: Path | str | None = None,
        ctx: BundleContext | None = None,
    ) -> None:
        context = ctx or BundleContext.load(
            bundle_id or _settings_bundle_id(), data_root=data_root
        )
        from services.terrain.haat import resolve_ned_dataset_version
        from services.terrain.ned import NedTerrainProvider

        terrain_dir = context.slot_dir("terrain_ned")
        version = resolve_ned_dataset_version(terrain_dir)
        object.__setattr__(self, "_ctx", context)
        object.__setattr__(
            self, "_ned", NedTerrainProvider(terrain_dir, dataset_version=version)
        )

    @property
    def protection_bundle_id(self) -> str:
        return self._ctx.bundle_id

    @property
    def elevation_backend(self) -> Any:
        """NedTerrainProvider for HAAT bridge in services/terrain."""
        return self._ned

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_TERRAIN})

    def provenance(self) -> DatasetProvenance:
        return _provenance(self._ctx, "protection_terrain")

    def validate_ready(self, *, strict: bool = False) -> None:
        assert_protection_data_ready(
            self._ctx.bundle_id,
            data_root=self._ctx.data_root,
            strict=strict,
        )

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> TerrainRecord:
        if point is None:
            raise ValueError("point is required for terrain fetch")
        elev = self._ned.elevation_m(point.latitude_deg, point.longitude_deg)
        return TerrainRecord(elevation_m=elev, provenance=self.provenance())


@dataclass(frozen=True, slots=True)
class ProtectionBundleBoundariesProvider:
    """Boundary resources from the protection bundle."""

    api_version = PROVIDER_API_VERSION
    kind = DataKind.BOUNDARIES

    _ctx: BundleContext

    def __init__(
        self,
        *,
        bundle_id: str | None = None,
        data_root: Path | str | None = None,
        ctx: BundleContext | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "_ctx",
            ctx or BundleContext.load(
                bundle_id or _settings_bundle_id(), data_root=data_root
            ),
        )

    @property
    def protection_bundle_id(self) -> str:
        return self._ctx.bundle_id

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_BOUNDARIES})

    def provenance(self) -> DatasetProvenance:
        return _provenance(self._ctx, "protection_boundaries")

    def validate_ready(self, *, strict: bool = False) -> None:
        assert_protection_data_ready(
            self._ctx.bundle_id,
            data_root=self._ctx.data_root,
            strict=strict,
        )

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> LonLatVerticesRecord:
        if token != TOKEN_US_CANADA_BORDER:
            raise ValueError(f"unsupported boundaries token {token!r}")
        kmz = self._ctx.slot_file("us_canada_border", pattern="uscabdry_sampled.kmz")
        if kmz is None:
            raise ValueError("us_canada_border KMZ unavailable in composed bundle")
        vertices = _load_kmz_vertices(kmz)
        return LonLatVerticesRecord(vertices=vertices, provenance=self.provenance())


@dataclass(frozen=True, slots=True)
class ProtectionBundleReferenceProvider:
    """Reference datasets: FCC offices, county geometry, DPA KML catalogue, NTIA EXZ."""

    api_version = PROVIDER_API_VERSION
    kind = DataKind.REFERENCE_DATA

    _ctx: BundleContext

    def __init__(
        self,
        *,
        bundle_id: str | None = None,
        data_root: Path | str | None = None,
        ctx: BundleContext | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "_ctx",
            ctx or BundleContext.load(
                bundle_id or _settings_bundle_id(), data_root=data_root
            ),
        )

    @property
    def protection_bundle_id(self) -> str:
        return self._ctx.bundle_id

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_REFERENCE_DATA})

    def provenance(self) -> DatasetProvenance:
        return _provenance(self._ctx, "protection_reference")

    def validate_ready(self, *, strict: bool = False) -> None:
        assert_protection_data_ready(
            self._ctx.bundle_id,
            data_root=self._ctx.data_root,
            strict=strict,
        )

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> (
        CoordinateListRecord
        | GeoJsonRecord
        | ResourcePathsRecord
    ):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required for reference_data fetch")
        if token == TOKEN_FCC_FIELD_OFFICES:
            csv_path = self._ctx.slot_dir("zones_reference") / "fcc_field_office_locations.csv"
            if not csv_path.is_file():
                # Bundle slot is geo/zones marker; FCC CSV ships under fcc/ in reference tree.
                csv_path = self._ctx.data_root / "fcc" / "fcc_field_office_locations.csv"
            coords = _load_fcc_offices_csv(csv_path)
            return CoordinateListRecord(
                coordinates=coords, provenance=self.provenance()
            )
        if token == TOKEN_DPA_KML_CATALOGUE:
            ntia_dir = self._ctx.slot_dir("dpa_definitions")
            paths: list[str] = []
            env = os.environ.get("SAS_DPA_KML_PATHS", "").strip()
            if env:
                paths.extend(
                    str(Path(part).expanduser().resolve())
                    for part in env.split(os.pathsep)
                    if part and Path(part).is_file()
                )
            else:
                for name in _DPA_KML_NAMES:
                    candidate = ntia_dir / name
                    if candidate.is_file():
                        paths.append(str(candidate.resolve()))
            if not paths:
                raise ValueError("DPA KML catalogue unavailable from composed provider")
            return ResourcePathsRecord(paths=tuple(paths), provenance=self.provenance())
        if token == TOKEN_NTIA_PROTECTION_ZONES:
            kml = self._ctx.slot_dir("dpa_definitions") / "protection_zones.kml"
            if not kml.is_file():
                kml = self._ctx.data_root / "ntia" / "protection_zones.kml"
            document = _ntia_coastal_geojson(kml)
            return GeoJsonRecord(document=document, provenance=self.provenance())
        if token.startswith(COUNTY_TOKEN_PREFIX):
            fips = token.removeprefix(COUNTY_TOKEN_PREFIX).strip()
            if not fips.isdigit():
                raise ValueError(f"invalid county token {token!r}")
            county_dir = self._ctx.slot_dir("census_tracts")
            if county_dir.name == "census":
                county_root = county_dir.parent / "county"
            else:
                county_root = self._ctx.data_root / "geo" / "county"
            path = (county_root / f"{fips}.json").resolve()
            try:
                path.relative_to(county_root.resolve())
            except ValueError as exc:
                raise ValueError(f"invalid county token {token!r}") from exc
            document = _load_county_geojson(path)
            return GeoJsonRecord(document=document, provenance=self.provenance())
        raise ValueError(f"unsupported reference_data token {token!r}")


def protection_terrain_provider() -> ProtectionBundleTerrainProvider:
    return ProtectionBundleTerrainProvider()


def protection_boundaries_provider() -> ProtectionBundleBoundariesProvider:
    return ProtectionBundleBoundariesProvider()


def protection_reference_provider() -> ProtectionBundleReferenceProvider:
    return ProtectionBundleReferenceProvider()


@dataclass(frozen=True, slots=True)
class ProtectionBundleLandCoverProvider:
    """NLCD / clutter land-cover authority from the protection bundle.

    Composition proves the land_cover capability is selected. Missing tiles for a
    specific coordinate remain a fetch-time coverage error, not a startup gap.
    """

    api_version = PROVIDER_API_VERSION
    kind = DataKind.LAND_COVER

    _ctx: BundleContext

    def __init__(
        self,
        *,
        bundle_id: str | None = None,
        data_root: Path | str | None = None,
        ctx: BundleContext | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "_ctx",
            ctx
            or BundleContext.load(
                bundle_id or _settings_bundle_id(), data_root=data_root
            ),
        )

    @property
    def protection_bundle_id(self) -> str:
        return self._ctx.bundle_id

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_LAND_COVER})

    def provenance(self) -> DatasetProvenance:
        return _provenance(self._ctx, "protection_land_cover")

    def validate_ready(self, *, strict: bool = False) -> None:
        assert_protection_data_ready(
            self._ctx.bundle_id,
            data_root=self._ctx.data_root,
            strict=strict,
        )
        # Slot presence is part of capability readiness; tiles may still be sparse.
        _ = self._ctx.slot_dir("nlcd_clutter")

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> LandCoverRecord:
        del token
        if point is None:
            raise ValueError("point is required for land_cover fetch")
        nlcd_dir = self._ctx.slot_dir("nlcd_clutter")
        try:
            from services.propagation.engines import load_reference_engines

            engines = load_reference_engines(nlcd_dir=str(nlcd_dir))
            vote = engines.region_nlcd_vote
            if vote is None:
                raise ValueError("land_cover coverage missing for point")
            # RegionNlcdVote historically accepts [[lat, lon]] or (lat, lon).
            try:
                raw = vote([[point.latitude_deg, point.longitude_deg]], 1)
            except TypeError:
                raw = vote(point.latitude_deg, point.longitude_deg)
            if raw is None:
                raise ValueError("land_cover coverage missing for point")
            # Stable integer code for region labels / numeric NLCD classes.
            if isinstance(raw, (int, float)):
                class_code = int(raw)
            else:
                class_code = abs(hash(str(raw))) % 100_000
            return LandCoverRecord(
                class_code=class_code, provenance=self.provenance()
            )
        except Exception as exc:  # noqa: BLE001 — map backend gaps to coverage miss
            raise ValueError("land_cover coverage missing for point") from exc


@dataclass(frozen=True, slots=True)
class ProtectionBundleRightsProvider:
    """Spectrum-rights capability authority backed by the protection bundle.

    CBRS PAL/license geometries remain specialized service concerns. This
    provider makes the required ``rights`` capability explicitly selected at
    composition time; point queries report no covering right unless extended.
    """

    api_version = PROVIDER_API_VERSION
    kind = DataKind.RIGHTS

    _ctx: BundleContext

    def __init__(
        self,
        *,
        bundle_id: str | None = None,
        data_root: Path | str | None = None,
        ctx: BundleContext | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "_ctx",
            ctx
            or BundleContext.load(
                bundle_id or _settings_bundle_id(), data_root=data_root
            ),
        )

    @property
    def protection_bundle_id(self) -> str:
        return self._ctx.bundle_id

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_RIGHTS})

    def provenance(self) -> DatasetProvenance:
        return _provenance(self._ctx, "protection_rights")

    def validate_ready(self, *, strict: bool = False) -> None:
        assert_protection_data_ready(
            self._ctx.bundle_id,
            data_root=self._ctx.data_root,
            strict=strict,
        )

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> FeatureIdsRecord:
        del token
        if point is None:
            raise ValueError("point is required for rights fetch")
        # Authority exists; no composed PAL polygon catalogue yet → empty cover.
        return FeatureIdsRecord(feature_ids=(), provenance=self.provenance())


def protection_land_cover_provider() -> ProtectionBundleLandCoverProvider:
    return ProtectionBundleLandCoverProvider()


def protection_rights_provider() -> ProtectionBundleRightsProvider:
    return ProtectionBundleRightsProvider()
