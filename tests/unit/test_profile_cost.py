"""automatic Spectrum Profile cost metrics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primitives.registry import (
    MechanismAxis,
    MechanismContract,
    MechanismRegistry,
    builtin_mechanism_registry,
)
from profiles.cost import (
    CostBucket,
    classify_repo_path,
    count_nonblank_loc,
    load_changed_files_list,
    measure_parsed_profile_cost,
    measure_profile_cost,
    mechanism_reuse,
    render_profile_cost_report,
)
from profiles.parse import parse_profile_document
from tools.profile_cost import main as profile_cost_main

_REPO = Path(__file__).resolve().parents[2]
_CAMPUS = _REPO / "profiles" / "profiles" / "examples" / "custom_campus_6ghz.yaml"
_CBRS = _REPO / "profiles" / "definitions" / "cbrs_winnforum.yaml"


def _minimal_doc(**overrides: object) -> dict:
    doc: dict = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {
            "id": "cost_fixture",
            "version": "1.0.0",
            "status": "custom",
            "based_on": None,
            "references": [],
        },
        "spectrum": {
            "ranges": [{"id": "primary", "low_hz": 100, "high_hz": 200}],
            "channelization": {
                "mechanism": "fixed_width_channelization",
                "width_hz": 10,
                "origin_hz": 100,
                "role": "assignment",
            },
        },
        "access": {
            "mechanism": "ordered_classes",
            "classes": [
                {"id": "a", "priority": 2, "preemptible": False},
                {"id": "b", "priority": 1, "preemptible": True},
            ],
        },
        "authorization": {"mechanism": "dynamic_lease", "duration_s": 60},
        "power": {
            "mechanism": "rule_table",
            "rules": [{"max_eirp_dbm": 20.0}],
        },
        "geography": {
            "mechanism": "point_radius",
            "center": {"latitude_deg": 0.0, "longitude_deg": 0.0},
            "radius_m": 100.0,
        },
        "temporal": {"reevaluation": {"mechanism": "periodic", "interval_s": 30}},
        "protection": {"mechanisms": ["protection_entitlement"]},
        "coordination": {"mechanism": "snapshot_evaluate_apply"},
        "rf": {
            "required": True,
            "policy": "path_loss_plus_aggregate",
            "propagation_model": "path_loss",
        },
        "data": {"required_capabilities": ["terrain"]},
        "requirements": {
            "device_capabilities": ["geolocation", "frequency_range", "max_eirp"]
        },
    }
    doc.update(overrides)
    return doc


def test_count_nonblank_loc(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("a\n\n# c\n\nb\n", encoding="utf-8")
    assert count_nonblank_loc(path) == 3


def test_classify_repo_path_buckets() -> None:
    assert classify_repo_path("adapters/cbsd.py") == CostBucket.PLUGIN
    assert classify_repo_path("providers/contract.py") == CostBucket.PLUGIN
    assert classify_repo_path("rf/port.py") == CostBucket.RF
    assert classify_repo_path("primitives/registry.py") == CostBucket.PRIMITIVE
    assert classify_repo_path("tests/unit/t.py") == CostBucket.TEST
    assert classify_repo_path("services/iap/x.py") == CostBucket.CORE
    assert classify_repo_path("main.py") == CostBucket.CORE
    assert classify_repo_path("docs/plugins/creating_plugins.md") == CostBucket.TOOLING


def test_mechanism_reuse_full_catalog() -> None:
    reused, novel, pct = mechanism_reuse(
        ("path_loss", "ordered_classes", "fixed_width_channelization")
    )
    assert novel == ()
    assert set(reused) == {
        "path_loss",
        "ordered_classes",
        "fixed_width_channelization",
    }
    assert pct == 100.0


def test_mechanism_reuse_detects_novel() -> None:
    reused, novel, pct = mechanism_reuse(("path_loss", "made_up_mech"))
    assert reused == ("path_loss",)
    assert novel == ("made_up_mech",)
    assert pct == 50.0


def test_campus_example_yaml_only_cost() -> None:
    report = measure_profile_cost(path=_CAMPUS, repo_root=_REPO)
    assert report.profile_id == "custom_campus_6ghz"
    assert report.yaml_loc > 50
    assert report.mechanism_reuse_pct == 100.0
    assert report.mechanisms_novel == ()
    assert report.plugin_loc == 0
    assert report.core_files_changed == 0
    assert "path_loss" in report.mechanisms_used
    assert "optional LOC buckets empty" in report.notes[0]


def test_cbrs_reference_by_id() -> None:
    report = measure_profile_cost(profile_id="cbrs_winnforum", repo_root=_REPO)
    assert report.profile_id == "cbrs_winnforum"
    assert report.yaml_loc == count_nonblank_loc(_CBRS)
    assert report.mechanism_reuse_pct == 100.0
    assert report.core_files_changed == 0


def test_explicit_buckets_and_changed_files(tmp_path: Path) -> None:
    plugin = tmp_path / "adapters" / "radio.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("class A:\n    pass\n", encoding="utf-8")
    test_f = tmp_path / "tests" / "unit" / "test_x.py"
    test_f.parent.mkdir(parents=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    core = tmp_path / "services" / "core.py"
    core.parent.mkdir(parents=True)
    core.write_text("CORE = 1\n", encoding="utf-8")
    rf = tmp_path / "rf" / "model.py"
    rf.parent.mkdir(parents=True)
    rf.write_text("def path_loss():\n    return 1\n", encoding="utf-8")

    yaml_path = tmp_path / "profile.yaml"
    yaml_path.write_text(_CAMPUS.read_text(encoding="utf-8"), encoding="utf-8")

    changed_list = tmp_path / "changed.txt"
    changed_list.write_text(
        "\n".join(
            [
                "adapters/radio.py",
                "tests/unit/test_x.py",
                "services/core.py",
                "rf/model.py",
                "# ignore",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = measure_profile_cost(
        path=yaml_path,
        repo_root=tmp_path,
        changed_files=load_changed_files_list(changed_list),
    )
    assert report.plugin_loc == count_nonblank_loc(plugin)
    assert report.tests_loc == count_nonblank_loc(test_f)
    assert report.core_files_changed == 1
    assert report.core_file_paths == ("services/core.py",)
    assert report.rf_files_changed == 1
    assert report.rf_loc == count_nonblank_loc(rf)
    assert report.mechanism_reuse_pct == 100.0


def test_render_and_cli_json(capsys: pytest.CaptureFixture[str]) -> None:
    report = measure_profile_cost(path=_CAMPUS, repo_root=_REPO)
    text = render_profile_cost_report(report)
    assert "yaml_loc:" in text
    assert "mechanism_reuse_pct: 100.0" in text

    rc = profile_cost_main(["--json", str(_CAMPUS)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile_id"] == "custom_campus_6ghz"
    assert payload["mechanism_reuse_pct"] == 100.0
    assert payload["yaml_loc"] == report.yaml_loc


def test_novel_mechanism_reuse_pct(tmp_path: Path) -> None:
    builtin = builtin_mechanism_registry()
    extended = MechanismRegistry(
        tuple(builtin.get(mid) for mid in sorted(builtin.ids()))
        + (MechanismContract("novel_only", MechanismAxis.PROTECTION, "1.0.0"),)
    )
    doc = _minimal_doc()
    doc["protection"] = {"mechanisms": ["protection_entitlement", "novel_only"]}
    parsed = parse_profile_document(doc, registry=extended)
    yaml_path = tmp_path / "novel.yaml"
    yaml_path.write_text("placeholder: 1\n", encoding="utf-8")
    report = measure_parsed_profile_cost(
        parsed,
        yaml_path=yaml_path,
        source=str(yaml_path),
    )
    assert "novel_only" in report.mechanisms_novel
    assert report.mechanism_reuse_pct < 100.0
    assert "NOVEL_MECHANISMS" in render_profile_cost_report(report)


def test_cli_id_ok(capsys: pytest.CaptureFixture[str]) -> None:
    rc = profile_cost_main(["--id", "cbrs_winnforum"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cbrs_winnforum" in out
    assert "mechanism_reuse_pct: 100.0" in out
