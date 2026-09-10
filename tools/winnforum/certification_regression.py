"""certification regression sequential local regression runner.

Runs the full pytest suite N times with a clean on-disk SQLite residue between
runs, optional timezone overrides, and an RSA/ECC security subset. Writes a
machine-readable JSON summary for evidence (no WInnForum PASS claims).

Full runs emit per-run JUnit XML (``full_1.xml`` …) and flake analysis compares
testcase identities across runs — not only aggregate counts.

Usage::

 .venv/bin/python -m tools.winnforum.certification_regression --runs 3 \\
 --outdir artifacts/winnforum/certification_regression_<stamp>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Independent count tokens — order must not matter.
_COUNT_TOKEN_RE = re.compile(
    r"(?P<n>\d+)\s+(?P<kind>passed|skipped|failed|errors?)\b",
    re.IGNORECASE,
)
_COLLECTION_ERROR_RE = re.compile(
    r"(?P<n>\d+)\s+error(?:s)?\s+during\s+collection",
    re.IGNORECASE,
)

# Residue files that can leak across sequential full-suite runs on the host.
_CLEAN_GLOBS = (
    "sas_mvp.db",
    ".pytest_engine_restore.db",
    "*.db-journal",
)


@dataclass(frozen=True)
class RunResult:
    label: str
    exit_code: int
    passed: int | None
    skipped: int | None
    failed: int | None
    errors: int | None
    duration_s: float
    summary_line: str
    tz: str
    log_path: str
    junit_path: str | None = None


def parse_pytest_summary(
    text: str,
) -> tuple[int | None, int | None, int | None, int | None, str]:
    """Parse pytest summary counts with order-independent tokens.

 Returns ``(passed, skipped, failed, errors, last_matching_line)``.
 """
    last = ""
    passed = skipped = failed = errors = None
    for line in text.splitlines():
        tokens = list(_COUNT_TOKEN_RE.finditer(line))
        coll = _COLLECTION_ERROR_RE.search(line)
        if not tokens and not coll:
            continue
        counts: dict[str, int] = {}
        for m in tokens:
            kind = m.group("kind").lower()
            key = "errors" if kind.startswith("error") else kind
            counts[key] = int(m.group("n"))
        if coll:
            counts["errors"] = max(counts.get("errors", 0), int(coll.group("n")))
        # Accept lines that mention at least one outcome token.
        if not counts:
            continue
        last = line.strip()
        if "passed" in counts or (
            "failed" in counts or "skipped" in counts or "errors" in counts
        ):
            if "passed" in counts:
                passed = counts.get("passed", 0)
                skipped = counts.get("skipped", 0)
                failed = counts.get("failed", 0)
                errors = counts.get("errors", 0)
            elif coll and set(counts) <= {"errors"}:
                # Collection failure line without a full summary.
                errors = counts["errors"]
                passed = skipped = failed = None
            else:
                passed = counts.get("passed", 0)
                skipped = counts.get("skipped", 0)
                failed = counts.get("failed", 0)
                errors = counts.get("errors", 0)
    return passed, skipped, failed, errors, last


def run_succeeded(result: RunResult) -> bool:
    """``exit_code != 0`` is always failure, even if counts were not parsed."""
    if result.exit_code != 0:
        return False
    if (result.failed or 0) > 0:
        return False
    if (result.errors or 0) > 0:
        return False
    return True


def evaluate_postgres_gate(result: RunResult) -> dict[str, object]:
    """PostgreSQL integration gate — exit_code is authoritative."""
    if result.exit_code != 0:
        if (result.failed or 0) > 0 or (result.errors or 0) > 0:
            classification = "FAIL_PRODUCT"
        else:
            # Parser missing counts must not hide a non-zero exit.
            classification = "UNKNOWN"
        return {
            "ok": False,
            "classification": classification,
            "exit_code": result.exit_code,
            "failed": result.failed,
            "errors": result.errors,
            "reason": "exit_code != 0 is authoritative; run is not success",
        }
    if (result.failed or 0) > 0 or (result.errors or 0) > 0:
        return {
            "ok": False,
            "classification": "FAIL_PRODUCT",
            "exit_code": result.exit_code,
            "failed": result.failed,
            "errors": result.errors,
            "reason": "parsed failures/errors with exit_code 0",
        }
    return {
        "ok": True,
        "classification": "PASS",
        "exit_code": result.exit_code,
        "failed": result.failed,
        "errors": result.errors,
        "reason": "exit_code 0 and no parsed failures/errors",
    }


def junit_case_id(classname: str, name: str) -> str:
    return f"{classname}::{name}"


def parse_junit_cases(path: Path) -> dict[str, str]:
    """Map ``classname::name`` → PASS|FAIL|ERROR|SKIP."""
    tree = ET.parse(path)
    root = tree.getroot()
    # Handle both <testsuite> and <testsuites><testsuite>…
    suites = []
    if root.tag == "testsuite":
        suites = [root]
    else:
        suites = list(root.iter("testsuite"))
    out: dict[str, str] = {}
    for suite in suites:
        for case in suite.findall("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            key = junit_case_id(classname, name)
            if case.find("error") is not None:
                out[key] = "ERROR"
            elif case.find("failure") is not None:
                out[key] = "FAIL"
            elif case.find("skipped") is not None:
                out[key] = "SKIP"
            else:
                out[key] = "PASS"
    return out


def classify_inconsistency(_states: dict[str, str]) -> str:
    """Never invent a root cause — unproven inconsistencies are UNKNOWN."""
    return "UNKNOWN"


def analyze_flakes(
    runs: list[RunResult],
    *,
    junit_cases: dict[str, dict[str, str]] | None = None,
    root: Path = _ROOT,
) -> dict[str, object]:
    """Compare full_N runs testcase-by-testcase via JUnit (not only counts).

    Only ``full_<n>`` labels participate. Timezone probes (``full_tz_*``) and
    subsets must not dilute the sequential stability claim.
    """
    full = sorted(
        [r for r in runs if re.fullmatch(r"full_\d+", r.label)],
        key=lambda r: r.label,
    )
    notes: list[str] = []
    if len(full) < 2:
        return {
            "comparable_full_runs": len(full),
            "stable": True,
            "product_regression_ok": True,
            "junit_missing": [],
            "inconsistencies": [],
            "counts_stable": True,
            "notes": ["Fewer than 2 full_N runs; flake comparison skipped."],
        }

    cases_by_run: dict[str, dict[str, str]] = {}
    junit_missing: list[str] = []
    for r in full:
        if junit_cases is not None and r.label in junit_cases:
            cases_by_run[r.label] = junit_cases[r.label]
            continue
        if not r.junit_path:
            junit_missing.append(r.label)
            continue
        path = Path(r.junit_path)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            junit_missing.append(r.label)
            continue
        cases_by_run[r.label] = parse_junit_cases(path)

    count_keys = {(r.passed, r.skipped, r.failed, r.errors, r.exit_code) for r in full}
    counts_stable = len(count_keys) == 1

    inconsistencies: list[dict[str, object]] = []
    if junit_missing:
        notes.append(f"JUnit missing for full runs: {junit_missing}")
    elif cases_by_run:
        all_ids: set[str] = set()
        for mapping in cases_by_run.values():
            all_ids |= set(mapping)
        for tid in sorted(all_ids):
            states = {
                label: cases_by_run.get(label, {}).get(tid, "MISSING")
                for label in cases_by_run
            }
            uniq = set(states.values())
            if len(uniq) == 1 and "MISSING" not in uniq:
                continue
            classification = classify_inconsistency(states)
            inconsistencies.append(
                {
                    "testcase": tid,
                    "states": states,
                    "kind": "INCONSISTENT",
                    "classification": classification,
                }
            )
        notes.append(
            f"Testcase-level comparison across {len(full)} full runs: "
            f"{len(inconsistencies)} inconsistent id(s); "
            f"aggregate counts_stable={counts_stable}"
        )
    else:
        notes.append("No JUnit cases available for testcase-level flake analysis.")

    flake_product = sum(
        1 for i in inconsistencies if i["classification"] == "FLAKE_PRODUCT"
    )
    flake_unknown = sum(
        1 for i in inconsistencies if i["classification"] == "UNKNOWN"
    )
    product_regression_ok = (
        not junit_missing and flake_product == 0 and flake_unknown == 0
    )
    stable = product_regression_ok and counts_stable
    return {
        "comparable_full_runs": len(full),
        "stable": stable,
        "product_regression_ok": product_regression_ok,
        "counts_stable": counts_stable,
        "junit_missing": junit_missing,
        "inconsistencies": inconsistencies,
        "flake_product": flake_product,
        "flake_env": 0,
        "flake_harness": 0,
        "flake_unknown": flake_unknown,
        "notes": notes,
    }


def compute_verdict(
    *,
    dirty: bool,
    flake: dict[str, object],
    results: list[RunResult],
    postgres_gate: dict[str, object] | None,
) -> str:
    """Final PASS_LOCAL is forbidden when dirty or integrity gates fail."""
    if dirty:
        return "ABORTED_DIRTY"
    if not flake.get("product_regression_ok", False):
        return "FAIL"
    relevant = [
        r
        for r in results
        if re.fullmatch(r"full_\d+", r.label) or r.label in {"rsa_ecc"}
    ]
    if any(not run_succeeded(r) for r in relevant):
        return "FAIL"
    tz_runs = [r for r in results if r.label.startswith("full_tz_")]
    if any(not run_succeeded(r) for r in tz_runs):
        return "FAIL"
    if postgres_gate is not None and not postgres_gate.get("ok", False):
        return "FAIL"
    return "PASS_LOCAL"


def clean_host_db_residue(root: Path) -> list[str]:
    """Remove common leftover SQLite files; returns removed paths (relative)."""
    removed: list[str] = []
    for pattern in _CLEAN_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                path.unlink()
                removed.append(str(path.relative_to(root)))
    return removed


def _python() -> str:
    venv = _ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def git_is_dirty(root: Path = _ROOT) -> bool:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(out.strip())


def git_branch(root: Path = _ROOT) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            text=True,
        )
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_head(*, short: bool = False, root: Path = _ROOT) -> str:
    try:
        args = ["git", "rev-parse"]
        if short:
            args.append("--short")
        args.append("HEAD")
        out = subprocess.check_output(args, cwd=root, text=True)
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_pytest(
    *,
    label: str,
    outdir: Path,
    args: list[str],
    env_extra: dict[str, str] | None = None,
    junit: bool = False,
) -> RunResult:
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / f"{label}.log"
    junit_path: Path | None = None
    env = os.environ.copy()
    env.setdefault("SAS_SCHEMA_VIA_CREATE_ALL", "1")
    if env_extra:
        env.update(env_extra)
    tz = env.get("TZ", "")
    cmd = [_python(), "-m", "pytest", "-q", "--tb=no", *args]
    if junit:
        junit_path = outdir / f"{label}.xml"
        cmd.extend([f"--junitxml={junit_path}"])
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    duration = time.monotonic() - t0
    output = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(output, encoding="utf-8")
    passed, skipped, failed, errors, summary = parse_pytest_summary(output)
    rel_junit: str | None = None
    if junit_path is not None:
        try:
            rel_junit = str(junit_path.relative_to(_ROOT))
        except ValueError:
            rel_junit = str(junit_path)
    return RunResult(
        label=label,
        exit_code=proc.returncode,
        passed=passed,
        skipped=skipped,
        failed=failed,
        errors=errors,
        duration_s=round(duration, 2),
        summary_line=summary,
        tz=tz,
        log_path=str(log_path.relative_to(_ROOT)),
        junit_path=rel_junit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Sequential full-suite runs")
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Artifact directory (default under artifacts/winnforum/)",
    )
    parser.add_argument(
        "--skip-tz-alt",
        action="store_true",
        help="Skip the extra America/Los_Angeles full-suite probe",
    )
    parser.add_argument(
        "--skip-rsa-ecc",
        action="store_true",
        help="Skip RSA/ECC security subset",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow runs on a dirty tree (verdict cannot be PASS_LOCAL)",
    )
    args = parser.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    campaign_id = f"certification_regression_{stamp}"
    outdir = args.outdir or (_ROOT / "artifacts" / "winnforum" / campaign_id)
    if not outdir.is_absolute():
        outdir = _ROOT / outdir
    outdir.mkdir(parents=True, exist_ok=True)

    dirty = git_is_dirty(_ROOT)
    uut_full = _git_head(short=False)
    uut_short = _git_head(short=True)
    branch = git_branch(_ROOT)
    harness_root = _ROOT.parent / "winnforum-sas-harness"
    harness_commit = _git_head(short=False, root=harness_root) if harness_root.is_dir() else None

    results: list[RunResult] = []
    cleaned_all: list[list[str]] = []

    if dirty and not args.allow_dirty:
        payload = {
            "task": "certification regression",
            "campaign_id": campaign_id,
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "uut_commit": uut_short,
            "uut_commit_full": uut_full,
            "branch": branch,
            "dirty": True,
            "harness_commit": harness_commit,
            "outdir": str(outdir.relative_to(_ROOT)),
            "cleaned_residue": [],
            "runs": [],
            "flake_analysis": {
                "comparable_full_runs": 0,
                "stable": False,
                "product_regression_ok": False,
                "notes": ["Aborted: dirty working tree; clean HEAD required for campaign."],
            },
            "postgres_gate": None,
            "docker_compose_full_stack": {
                "status": "NOT_RUN",
                "reason": "Aborted dirty-tree precheck.",
            },
            "official_harness": {
                "status": "NOT_RUN",
                "reason": "Aborted dirty-tree precheck.",
            },
            "verdict": "ABORTED_DIRTY",
            "product_regression_verdict": "NOT_RUN",
        }
        (outdir / "summary.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload, indent=2))
        return 1

    for i in range(1, max(1, args.runs) + 1):
        cleaned_all.append(clean_host_db_residue(_ROOT))
        results.append(
            run_pytest(
                label=f"full_{i}",
                outdir=outdir,
                args=[],
                env_extra={"TZ": "UTC"},
                junit=True,
            )
        )

    if not args.skip_rsa_ecc:
        cleaned_all.append(clean_host_db_residue(_ROOT))
        results.append(
            run_pytest(
                label="rsa_ecc",
                outdir=outdir,
                args=[
                    "tests/security/test_tls_matrix.py",
                    "tests/unit/test_cbsd_auth.py",
                    "tests/unit/test_certificate_policy.py",
                    "tests/security/test_certs_and_doctor.py",
                ],
                env_extra={"TZ": "UTC"},
                junit=True,
            )
        )

    if not args.skip_tz_alt:
        cleaned_all.append(clean_host_db_residue(_ROOT))
        results.append(
            run_pytest(
                label="full_tz_america_los_angeles",
                outdir=outdir,
                args=[],
                env_extra={"TZ": "America/Los_Angeles"},
                junit=True,
            )
        )

    cleaned_all.append(clean_host_db_residue(_ROOT))
    results.append(
        run_pytest(
            label="postgres_integrations",
            outdir=outdir,
            args=[
                "tests/integration/test_startup.py",
                "tests/integration/test_fad_publish_postgres.py",
                "tests/integration/test_cpas_multi_sas_postgres.py",
                "tests/integration/test_concurrency_postgres.py",
            ],
            env_extra={"TZ": "UTC"},
            junit=True,
        )
    )

    flake = analyze_flakes(results, root=_ROOT)
    pg = next((r for r in results if r.label == "postgres_integrations"), None)
    postgres_gate = evaluate_postgres_gate(pg) if pg is not None else None
    verdict = compute_verdict(
        dirty=dirty,
        flake=flake,
        results=results,
        postgres_gate=postgres_gate,
    )

    runs_payload = []
    for r in results:
        item = asdict(r)
        item["run_name"] = r.label
        item["success"] = run_succeeded(r)
        runs_payload.append(item)

    payload = {
        "task": "certification regression",
        "campaign_id": campaign_id,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "uut_commit": uut_short,
        "uut_commit_full": uut_full,
        "branch": branch,
        "dirty": dirty,
        "harness_commit": harness_commit,
        "outdir": str(outdir.relative_to(_ROOT)),
        "cleaned_residue": cleaned_all,
        "runs": runs_payload,
        "flake_analysis": flake,
        "postgres_gate": postgres_gate,
        "docker_compose_full_stack": {
            "status": "NOT_RUN",
            "reason": "Requires ./certs (gitignored) and optional .env; "
            "ephemeral PG covered by postgres_integrations instead.",
        },
        "official_harness": {
            "status": "NOT_RUN",
            "reason": "certification regression local regression only; Rel1Ext harness remains ENV-gated.",
        },
        "verdict": verdict,
        "product_regression_verdict": (
            "PASS_LOCAL" if verdict == "PASS_LOCAL" else verdict
        ),
    }
    summary_path = outdir / "summary.json"
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if verdict == "PASS_LOCAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
