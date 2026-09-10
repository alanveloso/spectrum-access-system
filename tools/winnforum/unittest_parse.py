"""Parse unittest -v console output into structured results (no PASS fabrication)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["passed", "failed", "error", "skipped", "unexpected"]

# Result may be on the same line, or deferred when openssl/cert tooling prints noise.
_LINE_START_RE = re.compile(
    r"^(?P<name>test_\S+)\s+\((?P<cls>[^)]+)\)(?:\s+\.\.\.\s*(?P<status>\S+))?\s*$"
)
_BARE_STATUS_RE = re.compile(r"^\s*\.\.\.\s*(?P<status>\S+)\s*$")
_TRAILING_STATUS_RE = re.compile(r"\.\.\.\s*(?P<status>\S+)\s*$")
_RAN_RE = re.compile(r"^Ran\s+(?P<n>\d+)\s+tests?\s+in\s+(?P<sec>[0-9.]+)s")
_FAIL_SUMMARY_RE = re.compile(
    r"^FAILED\s+\((?P<body>[^)]*)\)\s*$"
)
_OK_SUMMARY_RE = re.compile(r"^OK\b")
_KNOWN_STATUS = frozenset({"ok", "passed", "pass", "fail", "failed", "failure", "error", "errors", "skip", "skipped"})


@dataclass
class CaseResult:
    name: str
    class_name: str
    status: Status
    message: str = ""


@dataclass
class HarnessRunResult:
    cases: list[CaseResult] = field(default_factory=list)
    tests_run: int | None = None
    duration_seconds: float | None = None
    summary_line: str = ""
    raw_ok: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": [asdict(c) for c in self.cases],
            "tests_run": self.tests_run,
            "duration_seconds": self.duration_seconds,
            "summary_line": self.summary_line,
            "raw_ok": self.raw_ok,
            "passed": sum(1 for c in self.cases if c.status == "passed"),
            "failed": sum(1 for c in self.cases if c.status == "failed"),
            "error": sum(1 for c in self.cases if c.status == "error"),
            "skipped": sum(1 for c in self.cases if c.status == "skipped"),
        }


def _map_status(token: str) -> Status:
    t = token.lower().rstrip(".")
    if t in {"ok", "passed", "pass"}:
        return "passed"
    if t in {"fail", "failed", "failure"}:
        return "failed"
    if t in {"error", "errors"}:
        return "error"
    if t in {"skip", "skipped"}:
        return "skipped"
    return "unexpected"


def _is_terminal_status_token(token: str) -> bool:
    return token.lower().rstrip(".") in _KNOWN_STATUS


def parse_unittest_output(text: str) -> HarnessRunResult:
    """Parse ``unittest -v`` output, including deferred ``ok`` after OpenSSL noise."""
    result = HarnessRunResult()
    pending: CaseResult | None = None

    def _close_pending(status: Status) -> None:
        nonlocal pending
        if pending is None:
            return
        pending.status = status
        result.cases.append(pending)
        pending = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        bare = line.lower()
        if bare in {"ok", "fail", "failed", "error", "errors", "skip", "skipped"}:
            _close_pending(_map_status(bare))
            continue

        deferred = _BARE_STATUS_RE.match(line)
        if deferred and pending is not None and _is_terminal_status_token(
            deferred.group("status")
        ):
            _close_pending(_map_status(deferred.group("status")))
            continue

 # Docstring / noise lines that end with ``... ok`` while a case is pending.
        trailing = _TRAILING_STATUS_RE.search(line)
        if (
            pending is not None
            and trailing is not None
            and _is_terminal_status_token(trailing.group("status"))
            and not line.startswith("test_")
        ):
            _close_pending(_map_status(trailing.group("status")))
            continue

        m = _LINE_START_RE.match(line)
        if m:
 # A new test starts — close any prior pending as unexpected.
            if pending is not None:
                result.cases.append(pending)
                pending = None
            status_token = m.group("status")
            case = CaseResult(
                name=m.group("name"),
                class_name=m.group("cls"),
                status="unexpected",
            )
            if status_token and _is_terminal_status_token(status_token):
                case.status = _map_status(status_token)
                result.cases.append(case)
            else:
 # No status, or OpenSSL ``... +++`` noise on the same line.
                pending = case
            continue

        ran = _RAN_RE.match(line)
        if ran:
            if pending is not None:
                result.cases.append(pending)
                pending = None
            result.tests_run = int(ran.group("n"))
            result.duration_seconds = float(ran.group("sec"))
            continue
        if _OK_SUMMARY_RE.match(line):
            if pending is not None:
                result.cases.append(pending)
                pending = None
            result.summary_line = line
            result.raw_ok = True
            continue
        fail = _FAIL_SUMMARY_RE.match(line)
        if fail:
            if pending is not None:
                result.cases.append(pending)
                pending = None
            result.summary_line = line
            result.raw_ok = False
            continue

    if pending is not None:
        result.cases.append(pending)

    if result.raw_ok is None and result.cases:
        result.raw_ok = all(c.status == "passed" for c in result.cases)
    return result
