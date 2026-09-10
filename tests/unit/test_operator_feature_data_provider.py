"""operator feature bundles — no invented regulatory geometries."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from primitives.geography import GeoPoint
from providers.contract import DataKind
from providers.discovery import DataProviderDiscovery
from providers.operator_feature_bundle import (
    BUNDLE_ENV,
    DataBundleUnavailableError,
    OperatorFeatureBundleProvider,
    boundaries_provider,
    load_feature_bundle,
    protected_entities_provider,
    resolve_bundle_path,
)
from profiles.doctor import run_profile_doctor

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATE = (
    _REPO / "data" / "geo" / "anatel" / "slp_3700_operator_bundle.template.yaml"
)
_README = _REPO / "data" / "geo" / "anatel" / "README.md"


def test_template_and_readme_exist_without_invented_features() -> None:
    assert _TEMPLATE.is_file()
    assert _README.is_file()
    doc = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))
    assert doc["api_version"] == "spectrum-access-data/v1"
    assert doc["protected_entities"]["features"] == []
    assert doc["boundaries"]["features"] == []
    assert "Do NOT invent" in _TEMPLATE.read_text(encoding="utf-8")


def test_missing_bundle_fetch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BUNDLE_ENV, raising=False)
    monkeypatch.chdir(_REPO)
    default = _REPO / "data" / "geo" / "anatel" / "slp_3700_operator_bundle.yaml"
    assert not default.exists()
    assert resolve_bundle_path(cwd=_REPO) is None
    provider = OperatorFeatureBundleProvider(DataKind.PROTECTED_ENTITIES)
    assert provider.advertised_capabilities() == frozenset({"protected_entities"})
    assert not provider.bundle_ready()
    with pytest.raises(DataBundleUnavailableError):
        provider.fetch(point=GeoPoint(latitude_deg=-22.8, longitude_deg=-43.2))


def test_empty_section_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    provider = OperatorFeatureBundleProvider(DataKind.BOUNDARIES, bundle_path=path)
    assert provider.bundle_ready() is False
    with pytest.raises(DataBundleUnavailableError, match="empty"):
        provider.fetch(point=GeoPoint(0.0, 0.0))


def test_operator_supplied_bundle_fetch(tmp_path: Path) -> None:
    """Synthetic fixture only — not regulatory product data."""
    path = tmp_path / "op.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "api_version": "spectrum-access-data/v1",
                "provenance": {
                    "dataset_id": "fixture_only",
                    "dataset_version": "1",
                    "provider_id": "operator_feature_bundle",
                },
                "protected_entities": {
                    "features": [
                        {
                            "id": "fixture_zone",
                            "ring": [
                                [-43.2, -22.9],
                                [-43.1, -22.9],
                                [-43.1, -22.8],
                                [-43.2, -22.8],
                                [-43.2, -22.9],
                            ],
                        }
                    ]
                },
                "boundaries": {"features": []},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_feature_bundle(path)
    assert loaded.protected_entities.features[0][0] == "fixture_zone"
    entities = OperatorFeatureBundleProvider(
        DataKind.PROTECTED_ENTITIES, document=loaded
    )
    inside = GeoPoint(latitude_deg=-22.85, longitude_deg=-43.15)
    outside = GeoPoint(latitude_deg=0.0, longitude_deg=0.0)
    assert entities.fetch(point=inside).feature_ids == ("fixture_zone",)
    assert entities.fetch(point=outside).feature_ids == ()
    bounds = OperatorFeatureBundleProvider(DataKind.BOUNDARIES, document=loaded)
    with pytest.raises(DataBundleUnavailableError):
        bounds.fetch(point=inside)


def test_env_bundle_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "from_env.yaml"
    path.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_ENV, str(path))
    assert resolve_bundle_path() == path
    assert isinstance(protected_entities_provider(), OperatorFeatureBundleProvider)
    assert isinstance(boundaries_provider(), OperatorFeatureBundleProvider)


def test_discovery_overlay_covers_br_profile_capabilities() -> None:
    discovery = DataProviderDiscovery(
        overlays={
            "bundle_protected_entities": protected_entities_provider,
            "bundle_boundaries": boundaries_provider,
        },
        list_entry_points=lambda _g: (),
    )
    report = run_profile_doctor(
        profile_id="br_anatel_slp_3700",
        data_discovery=discovery,
    )
    assert report.ok
    by_name = {f.name: f for f in report.findings}
    assert by_name["data_plugins"].ok


def test_no_committed_operator_bundle_with_geometries() -> None:
    committed = _REPO / "data" / "geo" / "anatel" / "slp_3700_operator_bundle.yaml"
    assert not committed.exists()
