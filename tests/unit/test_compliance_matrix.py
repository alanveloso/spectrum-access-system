"""Compliance matrix validation tests ()."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from compliance.matrix_schema import (
    MatrixValidationError,
    assert_passing_evidence_exists,
    assert_referenced_paths_exist,
    load_matrix,
    parse_matrix_document,
)
from tests.support.repo import REPO_ROOT
from tools.winnforum.families import FAMILY_TEST_MODULES


MATRIX_PATH = REPO_ROOT / "compliance" / "matrix.yaml"


def test_matrix_file_loads_and_validates():
    matrix = load_matrix(MATRIX_PATH, repo_root=REPO_ROOT)
    assert matrix.version == 1
    assert len(matrix.cases) >= 20
    assert "WINNF.FT.S.HBT.3" in matrix.by_id()
    assert matrix.by_id()["WINNF.FT.S.HBT.3"].status == "failing"
    assert matrix.by_id()["WINNF.FT.S.HBT.3"].evidence is None
    assert "FAMILY.REG" in matrix.by_id()
    assert matrix.by_id()["FAMILY.REG"].case_scope == "family"


def test_no_passing_without_evidence_in_checked_in_matrix():
    matrix = load_matrix(MATRIX_PATH, repo_root=REPO_ROOT)
    for case in matrix.cases:
        if case.status == "passing":
            assert case.evidence, case.id
    assert_passing_evidence_exists(matrix, repo_root=REPO_ROOT)


def test_checked_in_matrix_implementation_paths_exist():
    matrix = load_matrix(MATRIX_PATH)
    assert_referenced_paths_exist(matrix, repo_root=REPO_ROOT)


def test_matrix_covers_known_runner_families():
    matrix = load_matrix(MATRIX_PATH, repo_root=REPO_ROOT)
    families = {c.family for c in matrix.cases}
    missing = set(FAMILY_TEST_MODULES) - families
    assert not missing, sorted(missing)


def test_reject_passing_without_evidence():
    with pytest.raises(MatrixValidationError, match="requires non-null evidence"):
        parse_matrix_document(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "WINNF.FT.S.REG.1",
                        "family": "REG",
                        "status": "passing",
                        "implementation": [],
                        "tests": [],
                        "evidence": None,
                        "notes": "",
                    }
                ],
            }
        )


def test_reject_family_rollup_passing():
    with pytest.raises(MatrixValidationError, match="case_scope=family cannot be status=passing"):
        parse_matrix_document(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "FAMILY.REG",
                        "family": "REG",
                        "case_scope": "family",
                        "status": "passing",
                        "implementation": [],
                        "tests": [],
                        "evidence": "docs/compliance/evidence/fake.md",
                        "notes": "",
                    }
                ],
            }
        )


def test_reject_duplicate_ids():
    case = {
        "id": "FAMILY.REG",
        "family": "REG",
        "status": "failing",
        "implementation": [],
        "tests": [],
        "evidence": None,
        "notes": "",
    }
    with pytest.raises(MatrixValidationError, match="duplicate"):
        parse_matrix_document({"version": 1, "cases": [case, dict(case)]})


def test_passing_requires_allowed_evidence_prefix(tmp_path: Path):
    # Unit-test paths must not count as WInnForum passing evidence.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("x\n", encoding="utf-8")
    matrix = parse_matrix_document(
        {
            "version": 1,
            "cases": [
                {
                    "id": "WINNF.FT.S.REG.99",
                    "family": "REG",
                    "case_scope": "case",
                    "status": "passing",
                    "implementation": [],
                    "tests": [],
                    "evidence": "tests/test_x.py",
                    "notes": "synthetic",
                }
            ],
        }
    )
    with pytest.raises(MatrixValidationError, match="must be under one of"):
        assert_passing_evidence_exists(matrix, repo_root=tmp_path)


def test_passing_requires_existing_evidence_file(tmp_path: Path):
    evidence_dir = tmp_path / "docs" / "compliance" / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence = evidence_dir / "ev.md"
    evidence.write_text("proof\n", encoding="utf-8")
    matrix = parse_matrix_document(
        {
            "version": 1,
            "cases": [
                {
                    "id": "WINNF.FT.S.REG.99",
                    "family": "REG",
                    "case_scope": "case",
                    "status": "passing",
                    "implementation": [],
                    "tests": [],
                    "evidence": "docs/compliance/evidence/ev.md",
                    "notes": "synthetic",
                }
            ],
        }
    )
    assert_passing_evidence_exists(matrix, repo_root=tmp_path)
    with pytest.raises(MatrixValidationError, match="evidence file missing"):
        assert_passing_evidence_exists(matrix, repo_root=tmp_path / "other")


def test_reject_path_escape_in_implementation(tmp_path: Path):
    matrix = parse_matrix_document(
        {
            "version": 1,
            "cases": [
                {
                    "id": "FAMILY.REG",
                    "family": "REG",
                    "case_scope": "family",
                    "status": "failing",
                    "implementation": ["../outside.py"],
                    "tests": [],
                    "evidence": None,
                    "notes": "",
                }
            ],
        }
    )
    with pytest.raises(MatrixValidationError, match="escapes repo root"):
        assert_referenced_paths_exist(matrix, repo_root=tmp_path)


def test_reject_missing_implementation_path():
    matrix = parse_matrix_document(
        {
            "version": 1,
            "cases": [
                {
                    "id": "FAMILY.REG",
                    "family": "REG",
                    "case_scope": "family",
                    "status": "failing",
                    "implementation": ["services/does_not_exist_cpi_service.py"],
                    "tests": [],
                    "evidence": None,
                    "notes": "",
                }
            ],
        }
    )
    with pytest.raises(MatrixValidationError, match="path missing"):
        assert_referenced_paths_exist(matrix, repo_root=REPO_ROOT)


def test_checked_in_matrix_yaml_is_parseable_mapping():
    data = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert isinstance(data["cases"], list)
