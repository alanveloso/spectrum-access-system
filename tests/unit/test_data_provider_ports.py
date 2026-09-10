"""data provider contract — terrain, cover, entities, rights, boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing
from providers.contract import (
    CAPABILITY_TERRAIN,
    DataKind,
    DatasetProvenance,
    MappingFeatureProvider,
    MappingLandCoverProvider,
    MappingReferenceProvider,
    MappingTerrainProvider,
    TerrainRecord,
    providers_meet_requirements,
)
from providers.discovery import DataProviderDiscovery

_BANNED = (
    "cbsd",
    "cbrs",
    "pal",
    "gaa",
    "grant",
    "heartbeat",
    "winnforum",
    "fcc",
    "ned",
    "nlcd",
)

_PROV = DatasetProvenance(dataset_id="mem", dataset_version="1", provider_id="map")


def test_terrain_and_land_cover_fail_closed_outside_coverage():
    point = GeoPoint(0.0, 0.0)
    terrain = MappingTerrainProvider({(0.0, 0.0): 12.5}, _PROV)
    cover = MappingLandCoverProvider({(0.0, 0.0): 41}, _PROV)
    rec = terrain.fetch(point=point)
    assert isinstance(rec, TerrainRecord)
    assert rec.elevation_m == pytest.approx(12.5)
    assert rec.provenance.as_pair() == ("mem", "1")
    assert cover.fetch(point=point).class_code == 41
    with pytest.raises(ValueError, match="coverage missing"):
        terrain.fetch(point=GeoPoint(1.0, 1.0))
    with pytest.raises(ValueError, match="point is required"):
        terrain.fetch(token="x")


def test_feature_and_reference_providers():
    ring = LinearRing.from_lon_lat([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])
    entities = MappingFeatureProvider(
        DataKind.PROTECTED_ENTITIES, (("e1", ring),), _PROV
    )
    rights = MappingFeatureProvider(DataKind.RIGHTS, (("r1", ring),), _PROV)
    bounds = MappingFeatureProvider(DataKind.BOUNDARIES, (("b1", ring),), _PROV)
    inside = GeoPoint(1.0, 1.0)
    outside = GeoPoint(9.0, 9.0)
    assert entities.fetch(point=inside).feature_ids == ("e1",)
    assert rights.fetch(point=outside).feature_ids == ()
    assert bounds.kind is DataKind.BOUNDARIES
    ref = MappingReferenceProvider(
        {"band-a": FrequencyRange(low_hz=1000, high_hz=2000)}, _PROV
    )
    assert ref.fetch(token="band-a").frequency.low_hz == 1000
    with pytest.raises(ValueError, match="reference band missing"):
        ref.fetch(token="nope")
    providers_meet_requirements(
        (entities, rights, bounds, ref),
        ("protected_entities", "rights", "boundaries", "reference_data"),
    )
    with pytest.raises(ValueError, match="missing required"):
        providers_meet_requirements((entities,), (CAPABILITY_TERRAIN,))


def test_discovery_loads_overlay_instance_without_core_edit():
    terrain = MappingTerrainProvider({(0.0, 0.0): 1.0}, _PROV)
    discovery = DataProviderDiscovery(
        overlays={"mem_terrain": terrain},
        list_entry_points=lambda _group: (),
    )
    loaded = discovery.load("mem_terrain")
    assert loaded.kind is DataKind.TERRAIN
    assert discovery.names() == frozenset({"mem_terrain"})
    with pytest.raises(ValueError, match="unknown data provider"):
        discovery.load("missing")


def test_providers_package_has_no_regime_nouns_or_service_imports():
    root = Path(__file__).resolve().parents[2] / "providers"
    implementation_files = {"protection_bundle.py", "bundle_context.py"}
    token_contract_files = {"contract.py"}
    for path in root.glob("*.py"):
        if path.name in implementation_files:
            continue
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        banned = _BANNED if path.name not in token_contract_files else tuple(
            t for t in _BANNED if t not in {"fcc", "ned", "nlcd", "winnforum"}
        )
        for token in banned:
            assert token not in lowered, f"{path.name} contains banned token {token!r}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("services")
                    assert not alias.name.startswith("models")
                    assert not alias.name.startswith("routes")
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("services")
                assert not node.module.startswith("models")
                assert not node.module.startswith("routes")
                assert not node.module.startswith("protection_data")
