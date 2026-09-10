"""NTIA EXZ coastal contours resolve via composed reference_data provider."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.models import Base
from protection_data.loader import get_data_root, set_data_root
from services.exclusion_zone_service import (
    ExclusionZoneUnavailable,
    enable_ntia_exclusion_zones,
    load_ntia_coastal_geojson,
)

_MIN_COASTAL_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>West Combined Contour</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -122.0,37.0,0 -122.0,38.0,0 -121.0,38.0,0 -121.0,37.0,0 -122.0,37.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


@pytest.fixture
def memory_db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_data_root():
    set_data_root(None)
    yield
    set_data_root(None)


def test_canonical_data_root_loads_via_composed_provider(memory_db):
    root = get_data_root()
    kml = root / "ntia" / "protection_zones.kml"
    assert kml.is_file(), (
        f"expected official KML at {kml}; "
        "run tools/winnforum/fetch_ntia_catalogue.sh"
    )

    src = Path("services/exclusion_zone_service.py").read_text(encoding="utf-8")
    assert "parents[2]" not in src
    assert "get_data_root" not in src

    features = load_ntia_coastal_geojson()["features"]
    assert len(features) >= 1
    enable_ntia_exclusion_zones(memory_db)


def _reference_only_composition():
    from profiles import load_profile
    from runtime import PluginRegistry, compose_runtime
    from runtime.deployment import DeploymentConfig, PluginSelection

    deployment = DeploymentConfig(
        id="exz_reference_only",
        version="1.0.0",
        description="reference_data provider only for EXZ root tests",
        device=PluginSelection(plugin="cbsd"),
        rf=PluginSelection(plugin="free_space"),
        protocol=PluginSelection(plugin="winnforum_rest"),
        providers=(PluginSelection(plugin="protection_reference"),),
    )
    return compose_runtime(
        load_profile("cbrs_winnforum"),
        deployment,
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )


def test_missing_payload_fail_closed(memory_db, tmp_path: Path):
    empty_root = tmp_path / "data"
    (empty_root / "ntia").mkdir(parents=True)
    set_data_root(empty_root)

    from runtime.context import runtime_composition_scope

    composition = _reference_only_composition()
    with runtime_composition_scope(composition):
        assert load_ntia_coastal_geojson()["features"] == []
        with pytest.raises(ExclusionZoneUnavailable):
            enable_ntia_exclusion_zones(memory_db)


def test_overridden_data_root_used_by_provider(memory_db, tmp_path: Path):
    override = tmp_path / "alt_data"
    ntia = override / "ntia"
    ntia.mkdir(parents=True)
    (ntia / "protection_zones.kml").write_text(_MIN_COASTAL_KML, encoding="utf-8")
    set_data_root(override)

    from runtime.context import runtime_composition_scope

    composition = _reference_only_composition()
    with runtime_composition_scope(composition):
        features = load_ntia_coastal_geojson()["features"]
        assert len(features) == 1
        assert features[0]["properties"]["name"] == "West Combined Contour"
        enable_ntia_exclusion_zones(memory_db)


def test_explicit_kml_path_parsing_bypasses_provider(tmp_path: Path):
    kml = tmp_path / "coastal.kml"
    kml.write_text(_MIN_COASTAL_KML, encoding="utf-8")
    features = load_ntia_coastal_geojson(kml_path=kml)["features"]
    assert len(features) == 1
