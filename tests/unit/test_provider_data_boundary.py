"""Provider/data boundary: composed providers are authoritative for regulatory data."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from primitives.geography import GeoPoint
from profiles import load_profile
from providers.contract import (
    CAPABILITY_BOUNDARIES,
    CAPABILITY_REFERENCE_DATA,
    DataKind,
    DatasetProvenance,
    LonLatVerticesRecord,
    PROVIDER_API_VERSION,
    TOKEN_US_CANADA_BORDER,
)
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.composition import RuntimeComposition
from runtime.context import runtime_composition_scope
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH
from runtime.errors import MissingCapabilityError
from services import border_geometry

_REPO = Path(__file__).resolve().parents[2]

_MIGRATED_MODULES = (
    "services/border_geometry.py",
    "services/quiet_zone_service.py",
    "services/dpa_service.py",
    "services/exclusion_zone_service.py",
    "services/county_geometry.py",
    "services/terrain/haat.py",
)

_FORBIDDEN_IN_MIGRATED = (
    "get_data_root",
    "set_data_root",
    "SAS_PROTECTION_DATA",
    "protection_data.loader",
    "DataProviderDiscovery",
    "PluginRegistry.from_discovery",
)


@dataclass(frozen=True, slots=True)
class _SpyBorderProvider:
    api_version = PROVIDER_API_VERSION
    kind = DataKind.BOUNDARIES

    vertices: tuple[tuple[float, float], ...]

    def advertised_capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_BOUNDARIES})

    def provenance(self) -> DatasetProvenance:
        return DatasetProvenance("spy_border", "test", "mapping_border")

    def fetch(
        self, *, point: GeoPoint | None = None, token: str | None = None
    ) -> LonLatVerticesRecord:
        if token != TOKEN_US_CANADA_BORDER:
            raise ValueError(f"unexpected token {token!r}")
        return LonLatVerticesRecord(vertices=self.vertices, provenance=self.provenance())


def test_spy_border_provider_wins_over_legacy_filesystem(tmp_path: Path):
    """Consumer must follow composed provider even when legacy KMZ path is corrupt."""
    spy_vertices = (
        (-100.0, 49.0),
        (-100.0, 49.1),
        (-99.9, 49.1),
    )
    composition = RuntimeComposition(
        profile=object(),  # type: ignore[arg-type]
        profile_context=object(),
        requirements=object(),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        protocol_adapter=None,
        device_adapter=None,
        network_adapter=None,
        providers=(_SpyBorderProvider(vertices=spy_vertices),),
        rf=None,
        provenance=object(),  # type: ignore[arg-type]
    )

    corrupt = tmp_path / "uscabdry_sampled.kmz"
    corrupt.write_bytes(b"not-a-valid-kmz")

    border_geometry.reset_border_geometry_cache()
    with runtime_composition_scope(composition):
        key, vertices = border_geometry._vertices_from_composed_provider()  # noqa: SLF001
        assert key.startswith("mapping_border:")
        assert vertices == list(spy_vertices)
    border_geometry.reset_border_geometry_cache()


def test_reference_deployment_resolves_protection_providers():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    boundaries = composition.provider_for("boundaries")
    assert boundaries.kind is DataKind.BOUNDARIES
    reference = composition.provider_for("reference_data")
    assert reference.kind is DataKind.REFERENCE_DATA
    terrain = composition.provider_for("terrain")
    assert terrain.kind is DataKind.TERRAIN
    assert "protection_boundaries" in composition.provenance.selected_plugins.values()


def test_missing_reference_data_capability_fails_closed():
    from runtime.deployment import DeploymentConfig, PluginSelection

    partial = DeploymentConfig(
        id="partial",
        version="1.0.0",
        description="terrain only",
        device=PluginSelection(plugin="cbsd"),
        rf=PluginSelection(plugin="free_space"),
        protocol=PluginSelection(plugin="winnforum_rest"),
        providers=(PluginSelection(plugin="protection_terrain"),),
    )
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        partial,
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    with pytest.raises(MissingCapabilityError):
        composition.provider_for("reference_data")


def test_migrated_modules_do_not_bypass_composed_providers():
    for rel in _MIGRATED_MODULES:
        path = _REPO / rel
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for forbidden in _FORBIDDEN_IN_MIGRATED:
                    if forbidden in node.module:
                        pytest.fail(f"{rel} imports forbidden module {node.module!r}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_IN_MIGRATED:
                    pytest.fail(f"{rel} calls forbidden {node.func.id}()")


def test_county_geometry_uses_provider_when_dir_unset(monkeypatch):
    monkeypatch.delenv("SAS_COUNTY_DIR", raising=False)
    from providers.contract import GeoJsonRecord
    from services.county_geometry import load_county_geometry

    ring = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]

    @dataclass(frozen=True, slots=True)
    class _CountyProvider:
        api_version = PROVIDER_API_VERSION
        kind = DataKind.REFERENCE_DATA

        def advertised_capabilities(self):
            return frozenset({CAPABILITY_REFERENCE_DATA})

        def provenance(self):
            return DatasetProvenance("spy", "1", "county")

        def fetch(self, *, point=None, token=None):
            return GeoJsonRecord(
                document={"type": "Polygon", "coordinates": [ring]},
                provenance=self.provenance(),
            )

    composition = RuntimeComposition(
        profile=object(),  # type: ignore[arg-type]
        profile_context=object(),
        requirements=object(),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        protocol_adapter=None,
        device_adapter=None,
        network_adapter=None,
        providers=(_CountyProvider(),),
        rf=None,
        provenance=object(),  # type: ignore[arg-type]
    )

    with runtime_composition_scope(composition):
        geom = load_county_geometry("12345")
    assert geom["type"] == "Polygon"


def test_county_geometry_maps_provider_file_not_found(monkeypatch):
    monkeypatch.delenv("SAS_COUNTY_DIR", raising=False)
    from services.county_geometry import CountyGeometryError, load_county_geometry

    @dataclass(frozen=True, slots=True)
    class _MissingCountyProvider:
        api_version = PROVIDER_API_VERSION
        kind = DataKind.REFERENCE_DATA

        def advertised_capabilities(self):
            return frozenset({CAPABILITY_REFERENCE_DATA})

        def provenance(self):
            return DatasetProvenance("spy", "1", "county")

        def fetch(self, *, point=None, token=None):
            raise FileNotFoundError("county geojson missing: 20063.json")

    composition = RuntimeComposition(
        profile=object(),  # type: ignore[arg-type]
        profile_context=object(),
        requirements=object(),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        protocol_adapter=None,
        device_adapter=None,
        network_adapter=None,
        providers=(_MissingCountyProvider(),),
        rf=None,
        provenance=object(),  # type: ignore[arg-type]
    )

    with runtime_composition_scope(composition):
        with pytest.raises(CountyGeometryError, match="county_file_missing"):
            load_county_geometry("20063")
