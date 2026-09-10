"""Load and validate the WINNF-TS-4010 V1.1.0 REL1Ext delta matrix."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from compliance.matrix_schema import (
    MatrixValidationError,
    resolve_under_repo,
    validate_passing_evidence_path,
)

Relation = Literal["replaces", "new", "modifies"]
DeltaStatus = Literal["untested", "failing", "passing", "blocked"]

ALLOWED_RELATION = frozenset({"replaces", "new", "modifies"})
ALLOWED_DELTA_STATUS = frozenset({"untested", "failing", "passing", "blocked"})

# Canonical case set from WINNF-TS-4010 V1.1.0 §5.5 (Summary of Changes).
# IDs use ``REL1Ext`` (mixed case) as defined in §5.1.
CANONICAL_REL1EXT_CASE_IDS: frozenset[str] = frozenset(
    {
        "WINNF.REL1Ext.FT.S.HBT.1",
        "WINNF.REL1Ext.FT.S.HBT.2",
        "WINNF.REL1Ext.FT.S.HBT.4",
        "WINNF.REL1Ext.FT.S.HBT.7",
        "WINNF.REL1Ext.FT.S.HBT.8",
        "WINNF.REL1Ext.FT.S.HBT.9",
        "WINNF.REL1Ext.FT.S.HBT.10",
        "WINNF.REL1Ext.FT.S.HBT.13",
        "WINNF.REL1Ext.FT.S.PAT.2",
        "WINNF.REL1Ext.FT.S.IPR.1",
        "WINNF.REL1Ext.FT.S.IPR.2",
        "WINNF.REL1Ext.FT.S.IPR.3",
        "WINNF.REL1Ext.FT.S.IPR.4",
        "WINNF.REL1Ext.FT.S.IPR.5",
        "WINNF.REL1Ext.FT.S.IPR.6",
        "WINNF.REL1Ext.FT.S.IPR.7",
        "WINNF.REL1Ext.FT.S.IPR.8",
        "WINNF.REL1Ext.FT.S.MCP.1",
        "WINNF.REL1Ext.FT.S.BPR.1",
    }
)


@dataclass(frozen=True)
class Rel1ExtDeltaCase:
    id: str
    family: str
    relation: Relation
    baseline_case: str | None
    section: str
    title: str
    requirements: tuple[str, ...]
    data_change: str
    calculation_change: str
    response_time_change: str
    local_tests: tuple[str, ...]
    evidence: str | None
    status: DeltaStatus
    notes: str


@dataclass(frozen=True)
class Rel1ExtDeltaMatrix:
    version: int
    source_spec: str
    source_version: str
    baseline_spec: str
    baseline_version: str
    requirements_spec: str
    requirements_version: str
    cases: tuple[Rel1ExtDeltaCase, ...]

    def by_id(self) -> dict[str, Rel1ExtDeltaCase]:
        return {c.id: c for c in self.cases}


def _require_nonempty_str(raw: Any, *, field: str, case_id: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise MatrixValidationError(f"{case_id}: {field} must be a non-empty string")
    return raw.strip()


def _as_str_list(value: Any, *, field: str, case_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MatrixValidationError(f"{case_id}: {field} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MatrixValidationError(f"{case_id}: {field} entries must be non-empty strings")
        out.append(item.strip())
    return out


def parse_rel1ext_delta_document(data: Any) -> Rel1ExtDeltaMatrix:
    if not isinstance(data, dict):
        raise MatrixValidationError("rel1ext delta root must be a mapping")
    version = data.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise MatrixValidationError("version must be a positive int")

    meta_fields = (
        "source_spec",
        "source_version",
        "baseline_spec",
        "baseline_version",
        "requirements_spec",
        "requirements_version",
    )
    meta: dict[str, str] = {}
    for field in meta_fields:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MatrixValidationError(f"{field} must be a non-empty string")
        meta[field] = value.strip()

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise MatrixValidationError("cases must be a non-empty list")

    cases: list[Rel1ExtDeltaCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise MatrixValidationError("each delta case must be a mapping")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise MatrixValidationError("case id is required")
        case_id = case_id.strip()
        if case_id in seen:
            raise MatrixValidationError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        if not case_id.startswith("WINNF.REL1Ext.FT.S."):
            raise MatrixValidationError(f"{case_id}: id must start with WINNF.REL1Ext.FT.S.")

        family = _require_nonempty_str(raw.get("family"), field="family", case_id=case_id)
        relation = raw.get("relation")
        if relation not in ALLOWED_RELATION:
            raise MatrixValidationError(
                f"{case_id}: relation {relation!r} not in {sorted(ALLOWED_RELATION)}"
            )
        baseline = raw.get("baseline_case", None)
        if baseline is not None and not isinstance(baseline, str):
            raise MatrixValidationError(f"{case_id}: baseline_case must be string or null")
        if isinstance(baseline, str):
            baseline = baseline.strip() or None
        if relation == "new" and baseline is not None:
            raise MatrixValidationError(f"{case_id}: relation=new requires baseline_case=null")
        if relation in {"replaces", "modifies"} and baseline is None:
            raise MatrixValidationError(
                f"{case_id}: relation={relation} requires non-null baseline_case"
            )

        status = raw.get("status", "untested")
        if status not in ALLOWED_DELTA_STATUS:
            raise MatrixValidationError(
                f"{case_id}: status {status!r} not in {sorted(ALLOWED_DELTA_STATUS)}"
            )
        evidence = raw.get("evidence", None)
        if evidence is not None and not isinstance(evidence, str):
            raise MatrixValidationError(f"{case_id}: evidence must be string or null")
        if isinstance(evidence, str) and not evidence.strip():
            evidence = None
        if status == "passing" and not evidence:
            raise MatrixValidationError(
                f"{case_id}: status=passing requires non-null evidence path"
            )

        requirements = tuple(
            _as_str_list(raw.get("requirements"), field="requirements", case_id=case_id)
        )
        if not requirements:
            raise MatrixValidationError(
                f"{case_id}: requirements must be a non-empty list (mapping required)"
            )

        notes = raw.get("notes", "")
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            raise MatrixValidationError(f"{case_id}: notes must be a string")

        cases.append(
            Rel1ExtDeltaCase(
                id=case_id,
                family=family.upper(),
                relation=relation,  # type: ignore[arg-type]
                baseline_case=baseline,
                section=_require_nonempty_str(raw.get("section"), field="section", case_id=case_id),
                title=_require_nonempty_str(raw.get("title"), field="title", case_id=case_id),
                requirements=requirements,
                data_change=_require_nonempty_str(
                    raw.get("data_change"), field="data_change", case_id=case_id
                ),
                calculation_change=_require_nonempty_str(
                    raw.get("calculation_change"),
                    field="calculation_change",
                    case_id=case_id,
                ),
                response_time_change=_require_nonempty_str(
                    raw.get("response_time_change"),
                    field="response_time_change",
                    case_id=case_id,
                ),
                local_tests=tuple(
                    _as_str_list(raw.get("local_tests"), field="local_tests", case_id=case_id)
                ),
                evidence=evidence.strip() if isinstance(evidence, str) else None,
                status=status,  # type: ignore[arg-type]
                notes=notes,
            )
        )

    return Rel1ExtDeltaMatrix(
        version=version,
        source_spec=meta["source_spec"],
        source_version=meta["source_version"],
        baseline_spec=meta["baseline_spec"],
        baseline_version=meta["baseline_version"],
        requirements_spec=meta["requirements_spec"],
        requirements_version=meta["requirements_version"],
        cases=tuple(cases),
    )


def assert_canonical_coverage(matrix: Rel1ExtDeltaMatrix) -> None:
    """Every TS-4010 §5.5 case must appear exactly once; no extras."""
    present = {c.id for c in matrix.cases}
    missing = CANONICAL_REL1EXT_CASE_IDS - present
    extra = present - CANONICAL_REL1EXT_CASE_IDS
    if missing or extra:
        raise MatrixValidationError(
            f"REL1Ext delta coverage mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )


def assert_referenced_local_tests_exist(matrix: Rel1ExtDeltaMatrix, *, repo_root: Path) -> None:
    for case in matrix.cases:
        for rel in case.local_tests:
            resolved = resolve_under_repo(repo_root, rel)
            if not resolved.is_file():
                raise MatrixValidationError(f"{case.id}: local_tests path missing: {rel}")


def assert_passing_evidence_exists(matrix: Rel1ExtDeltaMatrix, *, repo_root: Path) -> None:
    """Fail if any passing Rel1Ext row has missing or disallowed evidence (same rules as matrix)."""
    for case in matrix.cases:
        if case.status != "passing":
            continue
        assert case.evidence is not None
        try:
            validate_passing_evidence_path(case.evidence, repo_root=repo_root)
        except MatrixValidationError as exc:
            raise MatrixValidationError(f"{case.id}: {exc}") from exc


def load_rel1ext_delta(path: Path, *, repo_root: Path | None = None) -> Rel1ExtDeltaMatrix:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    matrix = parse_rel1ext_delta_document(data)
    assert_canonical_coverage(matrix)
    if repo_root is not None:
        assert_referenced_local_tests_exist(matrix, repo_root=repo_root)
        assert_passing_evidence_exists(matrix, repo_root=repo_root)
    return matrix
