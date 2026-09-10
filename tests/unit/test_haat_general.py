"""general HAAT for arbitrary coordinates — samples + documented tolerances."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.terrain import (
    CAT_A_OUTDOOR_HAAT_LIMIT_M,
    CallableTerrainProvider,
    HAAT_NED_ABS_TOL_M,
    HAAT_REPEATABILITY_ABS_TOL_M,
    HAAT_SYNTHETIC_ABS_TOL_M,
    NedTerrainProvider,
    WinnForumHaatProvider,
)
from services.terrain.haat import resolve_ned_dataset_version
from tests.support.repo import REPO_ROOT

NED_DIR = REPO_ROOT / "data" / "geo" / "ned"

# Independent NED samples (not harness REG.7 device_8). Golden values recorded
# 2026-08-07 against local USGS NED 1″ tiles with WinnForumHaatProvider.
#
# Format: (lat, lon, height_agl_m, elev_m, norm_haat_m, haat_m, skip_gate_tiles)
#
# ``skip_gate_tiles`` is **test-only diagnostic metadata** for ``pytest.skip`` when
# files are absent. It does NOT participate in terrain loading, tile selection,
# cache keys, dataset versioning, or HAAT reproducibility — production
# ``NedTerrainProvider`` resolves tiles dynamically from (lat, lon) during
# elevation lookups. Incomplete skip lists are a non-blocking CI DX debt
# (missing neighbor → hard fail instead of skip), not a product defect.
_NED_INDEPENDENT_SAMPLES: tuple[tuple, ...] = (
    (
        38.95,
        -77.25,
        5.0,
        106.300367355,
        12.390415290,
        17.390415290,
        (
            "usgs_ned_1_n39w077_gridfloat_std.flt",
            "usgs_ned_1_n39w078_gridfloat_std.flt",
            "usgs_ned_1_n40w077_gridfloat_std.flt",
            "usgs_ned_1_n40w078_gridfloat_std.flt",
        ),
    ),
    (
        38.75,
        -97.5,
        4.0,
        398.190345764,
        8.224692999,
        12.224692999,
        ("usgs_ned_1_n39w098_gridfloat_std.flt",),
    ),
    (
        39.75,
        -100.5,
        3.0,
        839.483947754,
        18.742956475,
        21.742956475,
        ("usgs_ned_1_n40w101_gridfloat_std.flt",),
    ),
)

# Near integer-degree boundary: 16 km radials cross into neighboring 1° tiles.
# skip_gate_tiles lists every tile touched by the HAAT radial set (diagnostic only).
_NED_BOUNDARY_SAMPLE = (
    38.995,
    -77.005,
    4.0,
    59.532573700,
    -12.364151207,
    -8.364151207,
    (
        "usgs_ned_1_n39w077_gridfloat_std.flt",
        "usgs_ned_1_n39w078_gridfloat_std.flt",
        "usgs_ned_1_n40w077_gridfloat_std.flt",
        "usgs_ned_1_n40w078_gridfloat_std.flt",
    ),
)


def test_documented_tolerances_are_exported():
    assert HAAT_SYNTHETIC_ABS_TOL_M == 1e-9
    assert HAAT_NED_ABS_TOL_M == 1e-3
    assert HAAT_REPEATABILITY_ABS_TOL_M == 0.0
    assert CAT_A_OUTDOOR_HAAT_LIMIT_M == 6.0


def test_flat_terrain_norm_haat_is_zero_for_arbitrary_sites():
    terrain = CallableTerrainProvider(lambda _lat, _lon: 123.0)
    provider = WinnForumHaatProvider(terrain)
    for lat, lon, height in (
        (10.0, 20.0, 4.0),
        (-33.5, 151.0, 2.5),
        (51.5, -0.1, 6.0),
    ):
        norm, elev = provider.compute_normalized_haat_m(lat, lon)
        haat = provider.compute_haat_m(lat, lon, height, height_is_agl=True)
        assert elev == pytest.approx(123.0, abs=HAAT_SYNTHETIC_ABS_TOL_M)
        assert norm == pytest.approx(0.0, abs=HAAT_SYNTHETIC_ABS_TOL_M)
        assert haat == pytest.approx(height, abs=HAAT_SYNTHETIC_ABS_TOL_M)


def test_site_peak_norm_haat_is_site_minus_radial_mean():
    site_lat, site_lon = 40.0, -105.0
    site_elev, radial_elev = 250.0, 100.0

    def elev(lat: float, lon: float) -> float:
        if lat == site_lat and lon == site_lon:
            return site_elev
        return radial_elev

    provider = WinnForumHaatProvider(CallableTerrainProvider(elev))
    norm, elev_site = provider.compute_normalized_haat_m(site_lat, site_lon)
    assert elev_site == pytest.approx(site_elev, abs=HAAT_SYNTHETIC_ABS_TOL_M)
    assert norm == pytest.approx(
        site_elev - radial_elev, abs=HAAT_SYNTHETIC_ABS_TOL_M
    )
    haat = provider.compute_haat_m(site_lat, site_lon, 4.0, height_is_agl=True)
    assert haat == pytest.approx(4.0 + (site_elev - radial_elev), abs=HAAT_SYNTHETIC_ABS_TOL_M)
    assert haat > CAT_A_OUTDOOR_HAAT_LIMIT_M


def test_agl_and_amsl_agree_on_analytic_terrain():
    terrain = CallableTerrainProvider(lambda lat, lon: 50.0 + 0.01 * lat)
    provider = WinnForumHaatProvider(terrain)
    lat, lon, height_agl = 35.0, -120.0, 4.0
    elev = terrain.elevation_m(lat, lon)
    haat_agl = provider.compute_haat_m(lat, lon, height_agl, height_is_agl=True)
    haat_amsl = provider.compute_haat_m(
        lat, lon, elev + height_agl, height_is_agl=False
    )
    assert haat_agl == pytest.approx(haat_amsl, abs=HAAT_SYNTHETIC_ABS_TOL_M)


def test_haat_repeatable_bit_identical_on_same_inputs():
    terrain = CallableTerrainProvider(lambda lat, lon: 10.0 + lat - lon)
    provider = WinnForumHaatProvider(terrain)
    a = provider.compute_haat_m(12.34, -56.78, 3.0)
    b = provider.compute_haat_m(12.34, -56.78, 3.0)
    assert a == b
    assert abs(a - b) <= HAAT_REPEATABILITY_ABS_TOL_M


def test_no_street_haat_lookup_table_in_production():
    joined = "\n".join(
        (REPO_ROOT / rel).read_text(encoding="utf-8")
        for rel in (
            "services/terrain/haat.py",
            "services/terrain/ned.py",
            "services/terrain/fake.py",
            "services/registration_service.py",
        )
    )
    assert "_KNOWN_STREET_HAAT" not in joined
    assert "KNOWN_STREET_HAAT_M" not in joined


def test_resolve_ned_dataset_version_prefers_version_marker(tmp_path: Path):
    (tmp_path / "VERSION").write_text("usgs_ned_1_gridfloat_v1\n", encoding="utf-8")
    assert resolve_ned_dataset_version(tmp_path) == "usgs_ned_1_gridfloat_v1"


def test_resolve_ned_dataset_version_env_overrides_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "VERSION").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("SAS_TERRAIN_DATASET_VERSION", "from-env")
    assert resolve_ned_dataset_version(tmp_path) == "from-env"


def test_resolve_ned_dataset_version_blank_env_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "VERSION").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("SAS_TERRAIN_DATASET_VERSION", "   ")
    assert resolve_ned_dataset_version(tmp_path) == "from-file"


def test_resolve_ned_dataset_version_default_when_missing(tmp_path: Path):
    from services.terrain.ned import DEFAULT_NED_DATASET_VERSION

    assert resolve_ned_dataset_version(tmp_path) == DEFAULT_NED_DATASET_VERSION


@pytest.mark.parametrize(
    "lat,lon,height,elev_ref,norm_ref,haat_ref,skip_gate_tiles",
    _NED_INDEPENDENT_SAMPLES,
    ids=["dc_alt", "ks_n39w098", "ks_n40w101"],
)
def test_ned_independent_samples_within_documented_tolerance(
    lat, lon, height, elev_ref, norm_ref, haat_ref, skip_gate_tiles
):
    missing = [name for name in skip_gate_tiles if not (NED_DIR / name).is_file()]
    if missing:
        pytest.skip(f"NED tiles missing: {missing}")

    # Provider ignores skip_gate_tiles; tiles are resolved dynamically from coords.
    provider = WinnForumHaatProvider(NedTerrainProvider(NED_DIR))
    norm, elev = provider.compute_normalized_haat_m(lat, lon)
    haat = provider.compute_haat_m(lat, lon, height, height_is_agl=True)
    haat_amsl = provider.compute_haat_m(lat, lon, elev + height, height_is_agl=False)

    assert elev == pytest.approx(elev_ref, abs=HAAT_NED_ABS_TOL_M)
    assert norm == pytest.approx(norm_ref, abs=HAAT_NED_ABS_TOL_M)
    assert haat == pytest.approx(haat_ref, abs=HAAT_NED_ABS_TOL_M)
    assert haat_amsl == pytest.approx(haat, abs=HAAT_NED_ABS_TOL_M)

    # Bit-identical repeatability on NED path.
    assert provider.compute_haat_m(lat, lon, height) == haat


def test_ned_haat_near_tile_boundary_uses_neighbor_tiles():
    """Site just south of 39°N / near −77°: radials cross into n40* and w078 tiles.

    skip_gate_tiles lists every tile the radial set touches (diagnostic skip only).
    """
    lat, lon, height, elev_ref, norm_ref, haat_ref, skip_gate_tiles = (
        _NED_BOUNDARY_SAMPLE
    )
    missing = [name for name in skip_gate_tiles if not (NED_DIR / name).is_file()]
    if missing:
        pytest.skip(f"NED tiles missing for boundary sample: {missing}")

    provider = WinnForumHaatProvider(NedTerrainProvider(NED_DIR))
    norm, elev = provider.compute_normalized_haat_m(lat, lon)
    haat = provider.compute_haat_m(lat, lon, height, height_is_agl=True)
    assert elev == pytest.approx(elev_ref, abs=HAAT_NED_ABS_TOL_M)
    assert norm == pytest.approx(norm_ref, abs=HAAT_NED_ABS_TOL_M)
    assert haat == pytest.approx(haat_ref, abs=HAAT_NED_ABS_TOL_M)


def test_ned_missing_neighbor_tile_fail_closed(tmp_path: Path):
    """Production tile selection is dynamic: absent neighbor → TerrainDataUnavailable.

    Does not use skip_gate_tiles; proves incomplete DEM cannot silently succeed.
    """
    from services.terrain import TerrainDataUnavailable

    empty = tmp_path / "ned-empty"
    empty.mkdir()
    terrain = NedTerrainProvider(empty)
    provider = WinnForumHaatProvider(terrain)
    with pytest.raises(TerrainDataUnavailable):
        provider.compute_haat_m(38.995, -77.005, 4.0)
