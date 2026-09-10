"""WINNF-TS-4010 REL1Ext delta matrix validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from compliance.matrix_schema import MatrixValidationError
from compliance.rel1ext_delta_schema import (
    CANONICAL_REL1EXT_CASE_IDS,
    assert_canonical_coverage,
    load_rel1ext_delta,
    parse_rel1ext_delta_document,
)
from tests.support.repo import REPO_ROOT


DELTA_PATH = REPO_ROOT / "compliance" / "rel1ext_delta.yaml"


def test_rel1ext_delta_loads_and_covers_canonical_set():
    matrix = load_rel1ext_delta(DELTA_PATH, repo_root=REPO_ROOT)
    assert matrix.version == 1
    assert matrix.source_spec == "WINNF-TS-4010"
    assert matrix.source_version == "V1.1.0"
    assert matrix.baseline_spec == "WINNF-TS-0061"
    assert {c.id for c in matrix.cases} == CANONICAL_REL1EXT_CASE_IDS
    assert len(matrix.cases) == len(CANONICAL_REL1EXT_CASE_IDS)


def test_rel1ext_delta_required_mapping_fields_present():
    matrix = load_rel1ext_delta(DELTA_PATH, repo_root=REPO_ROOT)
    for case in matrix.cases:
        assert case.section
        assert case.title
        assert case.data_change
        assert case.calculation_change
        assert case.response_time_change
        if case.relation == "new":
            assert case.baseline_case is None
        else:
            assert case.baseline_case is not None
            assert case.baseline_case.startswith("WINNF.FT.S.")


def test_rel1ext_new_cases_are_exactly_hbt13_pat2_ipr8():
    matrix = load_rel1ext_delta(DELTA_PATH, repo_root=REPO_ROOT)
    new_ids = {c.id for c in matrix.cases if c.relation == "new"}
    assert new_ids == {
        "WINNF.REL1Ext.FT.S.HBT.13",
        "WINNF.REL1Ext.FT.S.PAT.2",
        "WINNF.REL1Ext.FT.S.IPR.8",
    }


def test_rel1ext_no_passing_without_evidence():
    matrix = load_rel1ext_delta(DELTA_PATH, repo_root=REPO_ROOT)
    # must not invent Rel1Ext PASS claims.
    assert all(c.status != "passing" for c in matrix.cases)
    for case in matrix.cases:
        assert case.status in {"untested", "failing", "blocked"}, case.id
        assert case.requirements, case.id


def test_reject_empty_requirements():
    with pytest.raises(MatrixValidationError, match="requirements must be a non-empty list"):
        parse_rel1ext_delta_document(
            {
                "version": 1,
                "source_spec": "WINNF-TS-4010",
                "source_version": "V1.1.0",
                "baseline_spec": "WINNF-TS-0061",
                "baseline_version": "V1.5.1",
                "requirements_spec": "WINNF-TS-1020",
                "requirements_version": "V1.1.0",
                "cases": [
                    {
                        "id": "WINNF.REL1Ext.FT.S.HBT.13",
                        "family": "HBT",
                        "relation": "new",
                        "baseline_case": None,
                        "section": "6.4.4.13",
                        "title": "x",
                        "requirements": [],
                        "data_change": "d",
                        "calculation_change": "c",
                        "response_time_change": "r",
                        "local_tests": [],
                        "evidence": None,
                        "status": "failing",
                        "notes": "",
                    }
                ],
            }
        )


def test_reject_passing_with_disallowed_evidence_prefix():
    """Passing rows must use the same evidence prefixes as compliance/matrix.yaml."""
    from compliance.rel1ext_delta_schema import assert_passing_evidence_exists

    cases = []
    for case_id in sorted(CANONICAL_REL1EXT_CASE_IDS):
        is_new = case_id.endswith((".HBT.13", ".PAT.2", ".IPR.8"))
        relation = "new" if is_new else ("modifies" if case_id.endswith(".BPR.1") else "replaces")
        baseline = None if is_new else case_id.replace("REL1Ext.", "")
        if case_id.endswith(".BPR.1"):
            baseline = "WINNF.FT.S.BPR.1"
        cases.append(
            {
                "id": case_id,
                "family": case_id.split(".")[-2],
                "relation": relation,
                "baseline_case": baseline,
                "section": "x",
                "title": "t",
                "requirements": ["r"],
                "data_change": "d",
                "calculation_change": "c",
                "response_time_change": "r",
                "local_tests": [],
                "evidence": "tests/unit/test_rel1ext_delta.py" if case_id.endswith(".HBT.1") else None,
                "status": "passing" if case_id.endswith(".HBT.1") else "failing",
                "notes": "",
            }
        )
    matrix = parse_rel1ext_delta_document(
        {
            "version": 1,
            "source_spec": "WINNF-TS-4010",
            "source_version": "V1.1.0",
            "baseline_spec": "WINNF-TS-0061",
            "baseline_version": "V1.5.1",
            "requirements_spec": "WINNF-TS-1020",
            "requirements_version": "V1.1.0",
            "cases": cases,
        }
    )
    with pytest.raises(MatrixValidationError, match="passing evidence must be under"):
        assert_passing_evidence_exists(matrix, repo_root=REPO_ROOT)


def test_reject_incomplete_canonical_coverage():
    base = {
        "version": 1,
        "source_spec": "WINNF-TS-4010",
        "source_version": "V1.1.0",
        "baseline_spec": "WINNF-TS-0061",
        "baseline_version": "V1.5.1",
        "requirements_spec": "WINNF-TS-1020",
        "requirements_version": "V1.1.0",
        "cases": [
            {
                "id": "WINNF.REL1Ext.FT.S.HBT.13",
                "family": "HBT",
                "relation": "new",
                "baseline_case": None,
                "section": "6.4.4.13",
                "title": "x",
                "requirements": ["REL1Ext-R1-IPM-02"],
                "data_change": "d",
                "calculation_change": "c",
                "response_time_change": "r",
                "local_tests": [],
                "evidence": None,
                "status": "failing",
                "notes": "",
            }
        ],
    }
    matrix = parse_rel1ext_delta_document(base)
    with pytest.raises(MatrixValidationError, match="coverage mismatch"):
        assert_canonical_coverage(matrix)


def test_reject_new_with_baseline():
    with pytest.raises(MatrixValidationError, match="relation=new requires baseline_case=null"):
        parse_rel1ext_delta_document(
            {
                "version": 1,
                "source_spec": "WINNF-TS-4010",
                "source_version": "V1.1.0",
                "baseline_spec": "WINNF-TS-0061",
                "baseline_version": "V1.5.1",
                "requirements_spec": "WINNF-TS-1020",
                "requirements_version": "V1.1.0",
                "cases": [
                    {
                        "id": "WINNF.REL1Ext.FT.S.HBT.13",
                        "family": "HBT",
                        "relation": "new",
                        "baseline_case": "WINNF.FT.S.HBT.1",
                        "section": "6.4.4.13",
                        "title": "x",
                        "requirements": ["REL1Ext-R1-IPM-02"],
                        "data_change": "d",
                        "calculation_change": "c",
                        "response_time_change": "r",
                        "local_tests": [],
                        "evidence": None,
                        "status": "failing",
                        "notes": "",
                    }
                ],
            }
        )


def test_reject_missing_local_test_path(tmp_path: Path):
    missing = "tests/unit/does_not_exist_rel1ext.py"
    doc = {
        "version": 1,
        "source_spec": "WINNF-TS-4010",
        "source_version": "V1.1.0",
        "baseline_spec": "WINNF-TS-0061",
        "baseline_version": "V1.5.1",
        "requirements_spec": "WINNF-TS-1020",
        "requirements_version": "V1.1.0",
        "cases": [
            {
                "id": case_id,
                "family": case_id.split(".")[-2],
                "relation": "new" if case_id.endswith((".HBT.13", ".PAT.2", ".IPR.8")) else "replaces",
                "baseline_case": (
                    None
                    if case_id.endswith((".HBT.13", ".PAT.2", ".IPR.8"))
                    else case_id.replace("REL1Ext.", "")
                ),
                "section": "x",
                "title": "t",
                "requirements": ["r"],
                "data_change": "d",
                "calculation_change": "c",
                "response_time_change": "r",
                "local_tests": [missing] if case_id.endswith(".HBT.1") else [],
                "evidence": None,
                "status": "failing",
                "notes": "",
            }
            for case_id in sorted(CANONICAL_REL1EXT_CASE_IDS)
        ],
    }
    # BPR uses modifies
    for raw in doc["cases"]:
        if raw["id"].endswith(".BPR.1"):
            raw["relation"] = "modifies"
            raw["baseline_case"] = "WINNF.FT.S.BPR.1"
    path = tmp_path / "delta.yaml"
    import yaml

    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(MatrixValidationError, match="local_tests path missing"):
        load_rel1ext_delta(path, repo_root=REPO_ROOT)
