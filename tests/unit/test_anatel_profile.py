"""br_anatel_slp_3700 Spectrum Profile — YAML-only, matrix-linked, no BR Python."""

from __future__ import annotations

from pathlib import Path

import yaml

from profiles.cost import measure_profile_cost
from profiles.doctor import run_profile_doctor
from profiles.parse import load_profile, load_profile_document
from tools.profile_doctor import main as profile_doctor_main

_REPO = Path(__file__).resolve().parents[2]
_PROFILE = (
    _REPO / "profiles" / "definitions" / "br_anatel_slp_3700.yaml"
)
_MATRIX = (
    _REPO / "compliance" / "anatel" / "slp_3700_3800_requirements_matrix.yaml"
)


def test_profile_exists_and_doctor_passes() -> None:
    assert _PROFILE.is_file()
    report = run_profile_doctor(profile_id="br_anatel_slp_3700")
    assert report.ok, "; ".join(
        f"{f.name}={f.detail}" for f in report.findings if not f.ok
    )
    assert report.profile_id == "br_anatel_slp_3700"
    assert report.profile_hash


def test_cli_doctor_by_id() -> None:
    assert profile_doctor_main(["--id", "br_anatel_slp_3700"]) == 0


def test_band_channelization_and_power_from_ato_915() -> None:
    parsed = load_profile("br_anatel_slp_3700")
    assert parsed.metadata.status == "reference"
    assert parsed.metadata.id == "br_anatel_slp_3700"
    assert parsed.access is None
    assert parsed.authorization is not None
    assert parsed.authorization.mechanism == "static_authorization"
    assert parsed.authorization.duration_s is None
    assert parsed.temporal is None or parsed.temporal.reevaluation is None

    rng = parsed.spectrum.ranges[0]
    assert rng.low_hz == 3_700_000_000
    assert rng.high_hz == 3_800_000_000
    ch = parsed.spectrum.channelization
    assert ch is not None
    assert ch.mechanism == "fixed_width_channelization"
    assert ch.width_hz == 10_000_000
    assert ch.origin_hz == 3_700_000_000

    assert parsed.power is not None
    assert parsed.power.mechanism == "rule_table"
    by_key = {(r.indoor_outdoor, r.device_class): r for r in parsed.power.rules}
    indoor_base = by_key[("indoor", "base_nodal")]
    outdoor_base = by_key[("outdoor", "base_nodal")]
    assert indoor_base.max_eirp_dbm == 30.0
    assert indoor_base.max_psd_dbm_mhz == 20.0
    assert outdoor_base.max_eirp_dbm == 26.0
    assert outdoor_base.max_psd_dbm_mhz == 16.0
    terminal = by_key[("indoor", "terminal")]
    assert terminal.max_eirp_dbm == 26.0


def test_geography_authorized_area_and_non_iap_protection() -> None:
    parsed = load_profile("br_anatel_slp_3700")
    assert parsed.geography is not None
    assert parsed.geography.mechanism == "authorized_area"
    assert parsed.geography.authorized_areas
    assert parsed.protection is not None
    mechs = set(parsed.protection.mechanisms)
    assert "distance_exclusion" in mechs
    assert "exclusion_zone" in mechs
    assert "single_link_threshold" not in mechs
    assert "aggregate_linear_power" not in mechs
    assert parsed.rf is not None
    assert parsed.rf.required is False


def test_data_capabilities_without_invented_providers() -> None:
    parsed = load_profile("br_anatel_slp_3700")
    assert parsed.data is not None
    assert set(parsed.data.required_capabilities) == {
        "protected_entities",
        "boundaries",
    }
    assert "terrain" not in parsed.data.required_capabilities


def test_metadata_references_matrix_requirement_ids() -> None:
    parsed = load_profile_document(_PROFILE)
    refs = set(parsed.metadata.references)
    assert "ATO_915_2024" in refs
    matrix = yaml.safe_load(_MATRIX.read_text(encoding="utf-8"))
    planned = {
        row["id"]
        for row in matrix["requirements"]
        if row["status"] == "PLANNED_YAML"
    }
    assert planned <= refs, f"PLANNED_YAML missing from metadata.references: {planned - refs}"


def test_zero_profile_specific_python_and_cbrs_catalog_untouched() -> None:
    cost = measure_profile_cost(profile_id="br_anatel_slp_3700", repo_root=_REPO)
    assert cost.profile_python_loc == 0
    assert cost.mechanism_reuse_pct == 100.0
    assert cost.mechanisms_novel == ()
    cbrs = load_profile("cbrs_winnforum")
    assert cbrs.metadata.id == "cbrs_winnforum"


def test_no_brazil_branching_module() -> None:
    assert not (_REPO / "adapters" / "anatel.py").exists()
    assert not (_REPO / "services" / "anatel").exists()
    assert not list((_REPO / "profiles").glob("**/br_anatel*.py"))
