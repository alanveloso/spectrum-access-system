"""Load and validate the per-case WInnForum compliance matrix."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

ALLOWED_STATUS = frozenset({"untested", "failing", "passing", "blocked"})
Status = Literal["untested", "failing", "passing", "blocked"]

# Passing claims require harness/compliance evidence artifacts — not unit tests or source.
ALLOWED_EVIDENCE_PREFIXES: tuple[str, ...] = (
    "docs/compliance/evidence/",
    "compliance/evidence/",
    "artifacts/winnforum/",
)


@dataclass(frozen=True)
class MatrixCase:
    id: str
    family: str
    status: Status
    implementation: tuple[str, ...]
    tests: tuple[str, ...]
    evidence: str | None
    notes: str
    case_scope: str  # "case" | "family"


@dataclass(frozen=True)
class ComplianceMatrix:
    version: int
    cases: tuple[MatrixCase, ...]

    def by_id(self) -> dict[str, MatrixCase]:
        return {c.id: c for c in self.cases}


class MatrixValidationError(ValueError):
    pass


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


def resolve_under_repo(repo_root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``repo_root``; reject absolute paths and escapes."""
    root = repo_root.resolve()
    raw = Path(relative)
    if raw.is_absolute():
        raise MatrixValidationError(f"path must be repo-relative, got absolute: {relative!r}")
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MatrixValidationError(f"path escapes repo root: {relative!r}") from exc
    return resolved


def validate_passing_evidence_path(evidence: str, *, repo_root: Path) -> Path:
    """Ensure passing evidence is a real file under an allowed compliance prefix."""
    normalized = evidence.replace("\\", "/").lstrip("./")
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_EVIDENCE_PREFIXES):
        allowed = ", ".join(ALLOWED_EVIDENCE_PREFIXES)
        raise MatrixValidationError(
            f"passing evidence must be under one of: {allowed} (got {evidence!r})"
        )
    path = resolve_under_repo(repo_root, normalized)
    if not path.is_file():
        raise MatrixValidationError(f"evidence file missing: {evidence}")
    return path


def parse_matrix_document(data: Any) -> ComplianceMatrix:
    if not isinstance(data, dict):
        raise MatrixValidationError("matrix root must be a mapping")
    version = data.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise MatrixValidationError("version must be a positive int")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise MatrixValidationError("cases must be a non-empty list")

    cases: list[MatrixCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise MatrixValidationError("each case must be a mapping")
        case_id = raw.get("id")
        family = raw.get("family")
        status = raw.get("status")
        if not isinstance(case_id, str) or not case_id.strip():
            raise MatrixValidationError("case id is required")
        case_id = case_id.strip()
        if case_id in seen:
            raise MatrixValidationError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        if not isinstance(family, str) or not family.strip():
            raise MatrixValidationError(f"{case_id}: family is required")
        if status not in ALLOWED_STATUS:
            raise MatrixValidationError(
                f"{case_id}: status {status!r} not in {sorted(ALLOWED_STATUS)}"
            )
        evidence = raw.get("evidence", None)
        if evidence is not None and not isinstance(evidence, str):
            raise MatrixValidationError(f"{case_id}: evidence must be string or null")
        if evidence is not None and not evidence.strip():
            evidence = None
        scope = raw.get("case_scope", "case")
        if scope not in {"case", "family"}:
            raise MatrixValidationError(f"{case_id}: invalid case_scope {scope!r}")
        if status == "passing" and scope == "family":
            raise MatrixValidationError(
                f"{case_id}: case_scope=family cannot be status=passing "
                "(mark individual cases with harness evidence)"
            )
        if status == "passing" and not evidence:
            raise MatrixValidationError(
                f"{case_id}: status=passing requires non-null evidence path"
            )
        notes = raw.get("notes", "")
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            raise MatrixValidationError(f"{case_id}: notes must be a string")
        cases.append(
            MatrixCase(
                id=case_id,
                family=family.strip().upper(),
                status=status,  # type: ignore[arg-type]
                implementation=tuple(
                    _as_str_list(raw.get("implementation"), field="implementation", case_id=case_id)
                ),
                tests=tuple(_as_str_list(raw.get("tests"), field="tests", case_id=case_id)),
                evidence=evidence.strip() if isinstance(evidence, str) else None,
                notes=notes,
                case_scope=scope,
            )
        )
    return ComplianceMatrix(version=version, cases=tuple(cases))


def assert_passing_evidence_exists(matrix: ComplianceMatrix, *, repo_root: Path) -> None:
    """Fail if any passing row has missing or disallowed evidence."""
    for case in matrix.cases:
        if case.status != "passing":
            continue
        assert case.evidence is not None
        try:
            validate_passing_evidence_path(case.evidence, repo_root=repo_root)
        except MatrixValidationError as exc:
            raise MatrixValidationError(f"{case.id}: {exc}") from exc


def assert_referenced_paths_exist(matrix: ComplianceMatrix, *, repo_root: Path) -> None:
    """Fail if implementation/tests entries do not exist under the repo."""
    for case in matrix.cases:
        for field, paths in (
            ("implementation", case.implementation),
            ("tests", case.tests),
        ):
            for rel in paths:
                try:
                    resolved = resolve_under_repo(repo_root, rel)
                except MatrixValidationError as exc:
                    raise MatrixValidationError(f"{case.id}: {field}: {exc}") from exc
                if not resolved.is_file():
                    raise MatrixValidationError(f"{case.id}: {field} path missing: {rel}")


def load_matrix(path: Path, *, repo_root: Path | None = None) -> ComplianceMatrix:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    matrix = parse_matrix_document(data)
    if repo_root is not None:
        assert_passing_evidence_exists(matrix, repo_root=repo_root)
        assert_referenced_paths_exist(matrix, repo_root=repo_root)
    return matrix
