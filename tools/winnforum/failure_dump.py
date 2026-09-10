"""Per-failed-case diagnostic dumps for the WInnForum harness runner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.winnforum.unittest_parse import CaseResult, HarnessRunResult

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_case_dirname(case: CaseResult) -> str:
    raw = f"{case.name}"
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._") or "case"
    return cleaned[:120]


def extract_case_log_excerpt(harness_log: str, case_name: str, *, max_chars: int = 8000) -> str:
    """Best-effort excerpt around the first FAIL/ERROR block for ``case_name``."""
    lines = harness_log.splitlines()
    start = None
    for i, line in enumerate(lines):
        if case_name in line and (
            "FAIL" in line.upper() or "ERROR" in line.upper() or "..." in line
        ):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if case_name in line:
                start = i
                break
    if start is None:
        return harness_log[:max_chars]
 # Prefer the detailed FAIL:/ERROR: section when present.
    for i, line in enumerate(lines):
        if line.startswith("FAIL:") or line.startswith("ERROR:"):
            if case_name in line or case_name.replace("test_", "") in line:
                start = i
                break
    chunk = "\n".join(lines[start : start + 80])
    if len(chunk) > max_chars:
        return chunk[:max_chars] + "\n…[truncated]\n"
    return chunk + "\n"


def write_failure_dumps(
    artifacts_dir: Path,
    parsed: HarnessRunResult,
    *,
    harness_log_text: str,
) -> list[Path]:
    """Write ``failures/<case>/`` for each failed/error case. Returns dirs created."""
    created: list[Path] = []
    failures_root = artifacts_dir / "failures"
    for case in parsed.cases:
        if case.status not in {"failed", "error"}:
            continue
        case_dir = failures_root / safe_case_dirname(case)
        case_dir.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "name": case.name,
            "className": case.class_name,
            "status": case.status,
            "message": case.message,
        }
        (case_dir / "case.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        excerpt = extract_case_log_excerpt(harness_log_text, case.name)
        (case_dir / "harness_excerpt.txt").write_text(excerpt, encoding="utf-8")
        created.append(case_dir)
    return created
