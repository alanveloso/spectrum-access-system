"""Operator-supplied feature bundles.

Loads protected_entities / boundaries from a path the operator provides.
Does **not** ship regulatory geometries. Missing or empty bundles fail closed on fetch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from primitives.geography import GeoPoint, LinearRing
from providers.contract import (
    CAPABILITY_BOUNDARIES,
    CAPABILITY_PROTECTED_ENTITIES,
    PROVIDER_API_VERSION,
    DataKind,
    DatasetProvenance,
    FeatureIdsRecord,
)

BUNDLE_API_VERSION = "spectrum-access-data/v1"
BUNDLE_ENV = "SPECTRUM_ACCESS_FEATURE_BUNDLE"
DEFAULT_BUNDLE_REL = Path("data/geo/anatel/slp_3700_operator_bundle.yaml")


class DataBundleUnavailableError(ValueError):
    """Required operator feature bundle is missing or empty (fail closed)."""


def resolve_bundle_path(
    explicit: Path | None = None, *, cwd: Path | None = None
) -> Path | None:
    """Return configured bundle path, or None if unset."""
    if explicit is not None:
        return explicit.expanduser()
    env = os.environ.get(BUNDLE_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    root = cwd or Path.cwd()
    candidate = root / DEFAULT_BUNDLE_REL
    if candidate.is_file():
        return candidate
    return None


def _parse_ring(raw: Any, *, feature_id: str) -> LinearRing:
    if not isinstance(raw, list) or len(raw) < 3:
        raise ValueError(f"feature {feature_id!r} ring must have at least 3 positions")
    coords: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"feature {feature_id!r} ring vertices must be [lon, lat]")
        coords.append((float(item[0]), float(item[1])))
    return LinearRing.from_lon_lat(coords)


@dataclass(frozen=True, slots=True)
class FeatureBundleSection:
    features: tuple[tuple[str, LinearRing], ...]


@dataclass(frozen=True, slots=True)
class FeatureBundleDocument:
    provenance: DatasetProvenance
    protected_entities: FeatureBundleSection
    boundaries: FeatureBundleSection
    source_path: str


def load_feature_bundle(path: Path) -> FeatureBundleDocument:
    """Parse an operator bundle. Empty feature lists are allowed at load time."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise DataBundleUnavailableError(f"feature bundle not found: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"feature bundle YAML error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("feature bundle must be a mapping")
    if raw.get("api_version") != BUNDLE_API_VERSION:
        raise ValueError(
            f"feature bundle api_version must be {BUNDLE_API_VERSION!r}, "
            f"got {raw.get('api_version')!r}"
        )
    prov_raw = raw.get("provenance")
    if not isinstance(prov_raw, dict):
        raise ValueError("feature bundle requires provenance mapping")
    provenance = DatasetProvenance(
        dataset_id=str(prov_raw.get("dataset_id", "")),
        dataset_version=str(prov_raw.get("dataset_version", "")),
        provider_id=str(prov_raw.get("provider_id", "operator_feature_bundle")),
    )

    def _section(key: str) -> FeatureBundleSection:
        block = raw.get(key) or {}
        if not isinstance(block, dict):
            raise ValueError(f"{key} must be a mapping")
        features_raw = block.get("features") or []
        if not isinstance(features_raw, list):
            raise ValueError(f"{key}.features must be a list")
        parsed: list[tuple[str, LinearRing]] = []
        seen: set[str] = set()
        for item in features_raw:
            if not isinstance(item, dict):
                raise ValueError(f"{key}.features entries must be mappings")
            fid = str(item.get("id", "")).strip()
            if not fid:
                raise ValueError(f"{key}.features entry missing id")
            if fid in seen:
                raise ValueError(f"duplicate feature id {fid!r} under {key}")
            seen.add(fid)
            ring = _parse_ring(item.get("ring"), feature_id=fid)
            parsed.append((fid, ring))
        return FeatureBundleSection(features=tuple(parsed))

    return FeatureBundleDocument(
        provenance=provenance,
        protected_entities=_section("protected_entities"),
        boundaries=_section("boundaries"),
        source_path=str(resolved),
    )


class OperatorFeatureBundleProvider:
    """Capability-scoped view of an operator feature bundle.

    Construction succeeds even when the bundle is absent so discovery can advertise
    capabilities; ``fetch`` fails closed until a non-empty section is available.
    """

    api_version = PROVIDER_API_VERSION

    def __init__(
        self,
        kind: DataKind,
        *,
        bundle_path: Path | None = None,
        document: FeatureBundleDocument | None = None,
    ) -> None:
        if kind not in (DataKind.PROTECTED_ENTITIES, DataKind.BOUNDARIES):
            raise ValueError("bundle provider kind must be protected_entities or boundaries")
        self._kind = kind
        self._path = bundle_path
        self._document = document
        self._unavailable_reason: str | None = None
        if document is None:
            path = resolve_bundle_path(bundle_path)
            if path is None:
                self._unavailable_reason = (
                    f"no feature bundle configured "
                    f"(set {BUNDLE_ENV} or create {DEFAULT_BUNDLE_REL.as_posix()})"
                )
            else:
                try:
                    self._document = load_feature_bundle(path)
                    self._path = path
                except DataBundleUnavailableError as exc:
                    self._unavailable_reason = str(exc)
                except ValueError as exc:
                    self._unavailable_reason = str(exc)

    @property
    def kind(self) -> DataKind:
        return self._kind

    def advertised_capabilities(self) -> frozenset[str]:
        if self._kind is DataKind.PROTECTED_ENTITIES:
            return frozenset({CAPABILITY_PROTECTED_ENTITIES})
        return frozenset({CAPABILITY_BOUNDARIES})

    def provenance(self) -> DatasetProvenance:
        if self._document is None:
            return DatasetProvenance(
                dataset_id="operator_feature_bundle",
                dataset_version="unavailable",
                provider_id="operator_feature_bundle",
            )
        return self._document.provenance

    def bundle_ready(self) -> bool:
        if self._document is None:
            return False
        section = (
            self._document.protected_entities
            if self._kind is DataKind.PROTECTED_ENTITIES
            else self._document.boundaries
        )
        return bool(section.features)

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> FeatureIdsRecord:
        if point is None:
            raise ValueError("point is required")
        if self._document is None:
            raise DataBundleUnavailableError(
                self._unavailable_reason or "feature bundle unavailable"
            )
        section = (
            self._document.protected_entities
            if self._kind is DataKind.PROTECTED_ENTITIES
            else self._document.boundaries
        )
        if not section.features:
            raise DataBundleUnavailableError(
                f"{self._kind.value} section is empty in bundle "
                f"{self._document.source_path}; supply operator data from BDTA/"
                f"official sources — do not invent geometries"
            )
        ids = tuple(fid for fid, ring in section.features if ring.contains(point))
        return FeatureIdsRecord(feature_ids=ids, provenance=self._document.provenance)


def protected_entities_provider() -> OperatorFeatureBundleProvider:
    """Entry-point factory: protected_entities view of the operator bundle."""
    return OperatorFeatureBundleProvider(DataKind.PROTECTED_ENTITIES)


def boundaries_provider() -> OperatorFeatureBundleProvider:
    """Entry-point factory: boundaries view of the operator bundle."""
    return OperatorFeatureBundleProvider(DataKind.BOUNDARIES)
