"""certification package — build the local certification evidence package.

Populates ``certification-package/`` from the repository HEAD, lockfiles,
profiles, compliance matrix, and a certification regression summary that **must**
belong to the same UUT commit for FINAL mode.

Does **not** invent WInnForum PASS / PASS_OFFICIAL claims.

Usage::

 .venv/bin/python -m tools.winnforum.certification_package \\
 --outdir certification-package --mode preview
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]

NO_MATCHING_CAMPAIGN = "NO_MATCHING_CERTIFICATION_CAMPAIGN_FOR_UUT"

REQUIRED_FILES = (
    "README.md",
    "target-specifications.md",
    "uut-commit.txt",
    "harness-commit.txt",
    "datasets-manifest.json",
    "compliance-matrix.yaml",
    "compliance-matrix.NOTES.md",
    "known-deviations.md",
    "security-review.md",
    "package-status.json",
)

REQUIRED_DIRS = (
    "dependency-locks",
    "profiles",
    "configs",
    "results",
    "junit",
    "logs",
    "evidence",
)

_STALE_EVIDENCE_RE = re.compile(
    r"(GATE_VERIFY_\d{4}-\d{2}-\d{2}|_VERIFY_\d{8}|certification_regression_\d{8})",
    re.IGNORECASE,
)


class NoMatchingCampaignError(RuntimeError):
    """Raised when FINAL mode cannot find a certification regression summary for the UUT."""

    code = NO_MATCHING_CAMPAIGN


def _git_rev(path: Path, *, short: bool = False) -> str | None:
    if not path.exists():
        return None
    try:
        args = ["git", "rev-parse"]
        if short:
            args.append("--short")
        args.append("HEAD")
        out = subprocess.check_output(args, cwd=path, text=True)
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_resolve(commit: str, root: Path) -> str | None:
    """Resolve short/full hash to full SHA when unambiguous."""
    if not commit or commit in {"UNKNOWN", "unknown", "NOT_AVAILABLE"}:
        return None
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def commits_equivalent(a: str | None, b: str | None, *, root: Path) -> bool:
    if not a or not b:
        return False
    a = a.strip()
    b = b.strip()
    if a == b:
        return True
    # Prefix match only when one is a prefix of the other (short vs full).
    if a.startswith(b) or b.startswith(a):
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if len(shorter) >= 7 and longer.startswith(shorter):
            # Prefer git resolution when available.
            ra = _git_resolve(a, root)
            rb = _git_resolve(b, root)
            if ra and rb:
                return ra == rb
            return True
    ra = _git_resolve(a, root)
    rb = _git_resolve(b, root)
    if ra and rb:
        return ra == rb
    return False


def summary_uut_commit(summary: dict[str, Any]) -> str | None:
    return summary.get("uut_commit_full") or summary.get("uut_commit")


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def build_datasets_manifest(root: Path) -> dict[str, object]:
    ned = (root / "data" / "geo" / "ned" / "VERSION").read_text(encoding="utf-8").strip()
    ntia = (root / "data" / "ntia" / "VERSION").read_text(encoding="utf-8").strip()
    protection = root / "protection_data" / "manifests" / "cbrs_winnforum_protection.yaml"
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "spectrum_profile": "cbrs_winnforum",
        "ned_version_file": ned,
        "ntia_version_file": ntia,
        "protection_manifest": str(protection.relative_to(root)),
        "notes": [
            "NED/NTIA payload tiles/KML may be absent locally (VERSION-only checkouts).",
            "Do not treat this manifest as evidence of official harness PASS.",
        ],
    }


def list_certification_regression_summaries(root: Path) -> list[Path]:
    base = root / "artifacts" / "winnforum"
    if not base.is_dir():
        return []
    return sorted(base.glob("certification_regression_*/summary.json"))


def select_certification_regression_summary_for_uut(
    root: Path,
    uut: str,
    *,
    summaries: list[Path] | None = None,
) -> Path | None:
    """Newest summary whose uut_commit matches ``uut`` (never another UUT)."""
    paths = summaries if summaries is not None else list_certification_regression_summaries(root)
    for path in reversed(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        suut = summary_uut_commit(data)
        if suut and commits_equivalent(suut, uut, root=root):
            return path
    return None


def campaign_acceptable_for_final(summary: dict[str, Any]) -> list[str]:
    """Return blocking reasons if summary cannot back a FINAL package."""
    errors: list[str] = []
    if summary.get("dirty") is True:
        errors.append("certification regression summary dirty=True rejected for FINAL")
    verdict = str(summary.get("verdict") or summary.get("product_regression_verdict") or "")
    if verdict in {
        "NOT_RUN",
        "ABORTED_DIRTY",
        "ABORTED",
        "PRECHECK",
        "PRECHECK_ONLY",
        "",
    }:
        errors.append(f"certification regression summary verdict={verdict!r} rejected for FINAL")
    if "aborted" in verdict.lower() or "precheck" in verdict.lower():
        errors.append(f"certification regression summary verdict={verdict!r} rejected for FINAL")
    runs = summary.get("runs") or []
    labels = {r.get("run_name") or r.get("label") for r in runs if isinstance(r, dict)}
    for need in ("full_1", "full_2", "full_3"):
        if need not in labels:
            errors.append(f"certification regression summary missing run {need}")
    if not summary.get("flake_analysis"):
        errors.append("certification regression summary missing flake_analysis")
    if not (
        summary.get("product_regression_verdict")
        or summary.get("verdict")
        or summary.get("flake_analysis", {}).get("product_regression_ok") is not None
    ):
        errors.append("certification regression summary missing product regression verdict")
    if not summary.get("campaign_id") and not summary.get("timestamp") and not summary.get("at"):
        errors.append("certification regression summary missing campaign_id/timestamp")
    return errors


def classify_evidence_path(
    rel: str,
    *,
    uut: str,
    root: Path,
) -> str:
    """Classify evidence provenance relative to the package UUT."""
    if not rel:
        return "MISSING"
    if _STALE_EVIDENCE_RE.search(rel.replace("\\", "/")):
        return "HISTORICAL_UNVERIFIED"
    src = root / rel
    if not src.is_file():
        return "MISSING"
    text = src.read_text(encoding="utf-8", errors="replace")
    # Explicit current-UUT pin (full or short).
    short = uut[:7] if len(uut) >= 7 else uut
    if uut in text or (len(short) >= 7 and short in text):
        # Still historical gate-verify style names stay historical.
        name = Path(rel).name
        if "GATE_VERIFY" in name or re.search(r"_\d{4}-\d{2}-\d{2}", name):
            return "HISTORICAL_VERIFIED"
        return "CURRENT_UUT"
    # Known historical campaign evidence (P3/P5 gates).
    if Path(rel).name in {
        "winnforum_scs_sds_sss_harness.md",
        "winnforum_fad_harness.md",
        "winnforum_p6_legacy.md",
    }:
        return "HISTORICAL_VERIFIED"
    return "HISTORICAL_UNVERIFIED"


def load_passing_matrix_rows(matrix_path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(matrix_path.read_text(encoding="utf-8")) or {}
    cases: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for key in ("cases", "entries", "rows"):
            if isinstance(data.get(key), list):
                cases = data[key]
                break
        if not cases:
            for value in data.values():
                if (
                    isinstance(value, list)
                    and value
                    and isinstance(value[0], dict)
                    and "status" in value[0]
                ):
                    cases = value
                    break
    elif isinstance(data, list):
        cases = data
    return [
        c
        for c in cases
        if isinstance(c, dict) and str(c.get("status", "")).lower() == "passing"
    ]


def bundle_passing_evidence(
    *,
    outdir: Path,
    root: Path,
    uut: str,
    matrix_src: Path,
) -> list[dict[str, Any]]:
    """Copy referenced passing evidence into the package and build a manifest."""
    rows = load_passing_matrix_rows(matrix_src)
    manifest: list[dict[str, Any]] = []
    evidence_dir = outdir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        case_id = str(row.get("id") or row.get("case") or "")
        family = str(row.get("family") or "")
        src_rel = str(row.get("evidence") or "").strip()
        classification = classify_evidence_path(src_rel, uut=uut, root=root)
        bundled_rel = ""
        present = False
        if src_rel:
            src = root / src_rel
            if src.is_file():
                # Preserve relative path under evidence/
                dest = evidence_dir / Path(src_rel).name
                # Prefer nested mirror when under compliance/evidence/
                if src_rel.startswith("compliance/evidence/"):
                    dest = outdir / src_rel
                _copy_file(src, dest)
                try:
                    bundled_rel = str(dest.relative_to(outdir))
                except ValueError:
                    bundled_rel = str(dest)
                present = True
        manifest.append(
            {
                "case": case_id,
                "family": family,
                "status": "passing",
                "source_evidence": src_rel,
                "bundled_path": bundled_rel,
                "evidence_present": present,
                "classification": classification,
                "historical_or_current": (
                    "current"
                    if classification == "CURRENT_UUT"
                    else "historical"
                    if classification.startswith("HISTORICAL")
                    else "missing"
                ),
            }
        )
    _write(
        outdir / "evidence" / "passing-evidence-manifest.json",
        json.dumps(
            {
                "uut_commit": uut,
                "passing_row_count": len(manifest),
                "note": (
                    "HISTORICAL_* rows are not re-validated as PASS on the current UUT. "
                    "CURRENT_UUT requires an explicit commit pin in the evidence body."
                ),
                "rows": manifest,
            },
            indent=2,
        )
        + "\n",
    )
    return manifest


def target_specifications_text(uut: str, uut_short: str, harness: str, harness_short: str) -> str:
    return f"""# Target specifications

| Item | Value |
|------|-------|
| UUT commit | `{uut_short}` (`{uut}`) |
| Harness commit | `{harness_short}` (`{harness}`) |
| Spectrum profile | `cbrs_winnforum` |
| SAS-to-SAS version | v1.3 (config) |
| Selected suites | Release 1 FT.S families + Rel1Ext delta (see matrix / rel1ext_delta) |

## Local traceability sources

- `compliance/matrix.yaml` (snapshot in this package)
- `compliance/rel1ext_delta.yaml` (when present)
- `compliance/evidence/` (curated validation evidence bundled when referenced)

Official WInnForum TS documents remain external to this repository.
"""


def build_package(
    *,
    outdir: Path,
    root: Path = _ROOT,
    mode: str = "preview",
    summary_path: Path | None = None,
    summaries: list[Path] | None = None,
) -> dict[str, object]:
    mode_norm = mode.strip().lower()
    if mode_norm not in {"preview", "final"}:
        raise ValueError(f"mode must be preview|final, got {mode!r}")

    outdir = outdir if outdir.is_absolute() else root / outdir
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    uut = _git_rev(root) or "UNKNOWN"
    uut_short = _git_rev(root, short=True) or "UNKNOWN"
    harness_root = root.parent / "winnforum-sas-harness"
    harness_raw = _git_rev(harness_root)
    harness_short_raw = _git_rev(harness_root, short=True)
    if harness_raw is None:
        harness = "NOT_AVAILABLE"
        harness_short = "NOT_AVAILABLE"
        harness_note = (
            "Sibling ../winnforum-sas-harness not found or not a git checkout. "
            "Historical evidence pins often cite 928c315 — verify before campaigns."
        )
    else:
        harness = harness_raw
        harness_short = harness_short_raw or harness_raw[:7]
        harness_note = f"Resolved from {harness_root}"

    _write(outdir / "uut-commit.txt", uut)
    _write(
        outdir / "harness-commit.txt",
        f"{harness}\n# short={harness_short}\n# {harness_note}\n",
    )

    for name in ("requirements.lock.txt", "requirements.txt", "requirements-dev.txt"):
        src = root / name
        if src.is_file():
            _copy_file(src, outdir / "dependency-locks" / name)

    profile = root / "profiles" / "definitions" / "cbrs_winnforum.yaml"
    _copy_file(profile, outdir / "profiles" / "cbrs_winnforum.yaml")

    _copy_file(root / ".env.example", outdir / "configs" / "env.example")
    _write(
        outdir / "configs" / "execution-mode.md",
        """# Execution mode notes

- `SAS_EXECUTION_MODE=certification`: CPAS inline; rate limiting forced OFF.
- `SAS_EXECUTION_MODE=production`: Celery/RabbitMQ path for daily activities.
- Inject secrets via environment / secret manager — never commit real `.env`.
- App-layer CRL re-reads on disk; TLS handshake context needs process restart
  after cert/CRL replace (see `services/trust_reload.py`).
""",
    )

    matrix_src = root / "compliance" / "matrix.yaml"
    _copy_file(matrix_src, outdir / "compliance-matrix.yaml")
    _write(
        outdir / "compliance-matrix.NOTES.md",
        """# Compliance matrix snapshot — read before treating `passing` as proven

This file is a **byte copy** of `compliance/matrix.yaml` at package build time.

## Rules

1. **Family rollups** (`FAMILY.*`) must remain non-`passing` until official evidence
   policy says otherwise — see `DEV-MATRIX-ROLLUPS` in `known-deviations.md`.
2. Individual rows with `status: passing` are **historical case claims** that are
   only valid together with the `evidence:` path named in that row. Canonical
   evidence lives under `compliance/evidence/` (bundled under `evidence/` in this
   package when present). Paths under `docs/compliance/evidence/` are legacy and
   are not the canonical tree.
3. Shipping this YAML inside `certification-package/` does **not** create new
   WInnForum PASS / PASS_OFFICIAL claims and does not replace harness JUnit.
4. Before citing any `passing` row as **current-UUT** PASS, confirm the bundled
   evidence classification in `evidence/passing-evidence-manifest.json` is
   `CURRENT_UUT` for this package's `uut-commit.txt`. Historical PASS evidence
   is retained explicitly and is **not** revalidation on the current UUT.
5. FINAL packages require every `passing` row to have bundled evidence files.
   PREVIEW packages are **not** certification-ready.
""",
    )
    if (root / "compliance" / "rel1ext_delta.yaml").is_file():
        _copy_file(
            root / "compliance" / "rel1ext_delta.yaml",
            outdir / "configs" / "rel1ext_delta.yaml",
        )

    manifest = build_datasets_manifest(root)
    _write(outdir / "datasets-manifest.json", json.dumps(manifest, indent=2) + "\n")

    evidence_manifest = bundle_passing_evidence(
        outdir=outdir, root=root, uut=uut, matrix_src=matrix_src
    )

    selected_summary: Path | None = summary_path
    if selected_summary is None:
        selected_summary = select_certification_regression_summary_for_uut(
            root, uut, summaries=summaries
        )

    summary_data: dict[str, Any] | None = None
    campaign_errors: list[str] = []
    results_note = outdir / "results" / "README.md"
    if selected_summary is not None and selected_summary.is_file():
        summary_data = json.loads(selected_summary.read_text(encoding="utf-8"))
        suut = summary_uut_commit(summary_data) or ""
        if not commits_equivalent(suut, uut, root=root):
            campaign_errors.append(
                f"UUT campaign mismatch: package uut={uut} summary uut={suut}"
            )
            selected_summary = None
            summary_data = None
        else:
            _copy_file(
                selected_summary,
                outdir / "results" / "certification_regression_summary.json",
            )
            try:
                src_rel = str(selected_summary.relative_to(root))
            except ValueError:
                src_rel = str(selected_summary)
            _write(
                results_note,
                f"Local certification regression regression summary copied from `{src_rel}` "
                "(must match package UUT). Verdict is PASS_LOCAL only — not "
                "official harness PASS.\n",
            )
            if mode_norm == "final":
                campaign_errors.extend(campaign_acceptable_for_final(summary_data))
    else:
        _write(
            results_note,
            "No certification regression summary matching this UUT under "
            "artifacts/winnforum/certification_regression_*/. "
            "Run `python -m tools.winnforum.certification_regression` on a clean HEAD, then rebuild.\n",
        )
        if mode_norm == "final":
            campaign_errors.append(NO_MATCHING_CAMPAIGN)

    if mode_norm == "final" and campaign_errors:
        # Leave a partial tree for inspection but mark failure clearly.
        failed_status: dict[str, object] = {
            "mode": "final",
            "uut_commit": uut,
            "certification_campaign_id": None,
            "certification_uut_commit": None,
            "validation_errors": campaign_errors,
            "current_uut_campaign_match": False,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "certification_ready": False,
        }
        _write(outdir / "package-status.json", json.dumps(failed_status, indent=2) + "\n")
        raise NoMatchingCampaignError("; ".join(campaign_errors))

    _write(
        outdir / "junit" / "README.md",
        "Official harness JUnit XML is not bundled here. "
        "certification regression per-run JUnit (full_1.xml…) lives under the campaign artifact dir. "
        "Do not invent PASS.\n",
    )
    _write(
        outdir / "logs" / "README.md",
        "Operational / harness logs are not copied into git. "
        "See gitignored `artifacts/winnforum/` for local runs.\n",
    )

    specs = target_specifications_text(uut, uut_short, harness, harness_short)
    _write(outdir / "target-specifications.md", specs)

    _write(
        outdir / "known-deviations.md",
        f"""# Known deviations / ENV gaps

Generated for UUT `{uut_short}` — **not** an official waiver list.

| ID | Classification | Description |
|----|----------------|-------------|
| DEV-CERTS | ENV | `./certs` often absent locally; doctor FAIL; Compose api/worker mTLS not exercised |
| DEV-REL1EXT-HARNESS | HARNESS | Rel1Ext/TS-4010 suite files missing at common pin `928c315` |
| DEV-ITM | ENV | Official ITM / `reference_models` may be absent in UUT venv |
| DEV-PG-CONCURRENCY | ENV | `tests/integration/test_concurrency_postgres.py` skips without `SAS_TEST_DATABASE_URL` / `:55432` |
| DEV-COMPOSE-STACK | ENV | Full Docker Compose (api+worker+RabbitMQ+certs) not required for local pytest PASS_LOCAL |
| DEV-OCSP | TARGET | OCSP network validation disabled (`SAS_SSL_OCSP_MODE=disabled`); CRL PEM target |
| DEV-PEER-INJECT-HTTP | LEGACY | Invalid peer inject URL returns empty Admin 200 without persistence |
| DEV-MATRIX-ROLLUPS | EVIDENCE | Family rollups remain `failing` until official evidence is migrated — no invented `passing` |
| DEV-MATRIX-SNAPSHOT | EVIDENCE | Case-level `passing` rows may be HISTORICAL; see `evidence/passing-evidence-manifest.json` |

## Non-claims

- This package does **not** assert WInnForum family PASS or PASS_OFFICIAL.
- Local pytest / certification regression PASS_LOCAL is product hardening evidence only.
- Package mode `{mode_norm}` — PREVIEW is not certification-ready.
""",
    )

    _write(
        outdir / "security-review.md",
        f"""# Security review snapshot

UUT `{uut_short}`. Hardening delivered under the security hardening phase:

| Control | Status |
|---------|--------|
| Canonical role OIDs | `services/winnf_role_oids.py` shared by mtls_auth + rbac |
| SSRF egress | `services/ssrf.py` (default fail-closed; lab paths opt-in) |
| Request body limits | ASGI incremental count + Content-Length early 413 |
| Rate limiting | Off in `SAS_EXECUTION_MODE=certification`; production keys from TLS peer cert (not spoofable headers) |
| Trust/CRL reload | Admin `/admin/security/trust_material` + reload |
| Secrets guidance | `.env.example` PRODUCTION SECRETS; empty DB_SYNC defaults |

## Residual risks

- Multi-worker in-process rate limit / metrics are not cluster-global.
- Body middleware retains at most `SAS_MAX_REQUEST_BODY_BYTES` for replay.
- Official mTLS campaigns still need provisioned `./certs`.

Evidence: compliance security evidence.
""",
    )

    mode_banner = (
        "**PACKAGE STATUS: PREVIEW** — not certification-ready."
        if mode_norm == "preview"
        else "**PACKAGE STATUS: FINAL** — requires matching clean certification regression campaign."
    )
    _write(
        outdir / "README.md",
        f"""# SAS Core — certification package

**Generated:** {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}

**UUT:** `{uut_short}`

**Harness checkout:** `{harness_short}`

{mode_banner}

This directory is a **reproducible evidence bundle** for lab/certification prep.
It is **not** a claim that official WInnForum suites passed.

## Layout

The package layout is described by the files listed below and generated from the current compliance matrix/evidence sources.

## Rebuild

```bash
.venv/bin/python -m tools.winnforum.certification_package --outdir certification-package --mode preview
```

## Read first

1. `package-status.json`
2. `target-specifications.md`
3. `known-deviations.md`
4. `security-review.md`
5. `compliance-matrix.yaml` **with** `compliance-matrix.NOTES.md`
6. `evidence/passing-evidence-manifest.json`
7. `results/` (UUT-matching local regression summary only)

## Gate note

A final package requires a matching clean local certification-regression campaign.
Official Rel1Ext PASS×3 remains environment/harness gated (see known-deviations).
""",
    )

    p8_campaign_id = (summary_data or {}).get("campaign_id")
    p8_uut = summary_uut_commit(summary_data) if summary_data else None
    status: dict[str, object] = {
        "mode": mode_norm,
        "uut_commit": uut,
        "certification_campaign_id": p8_campaign_id,
        "certification_uut_commit": p8_uut,
        "validation_errors": [],
        "current_uut_campaign_match": bool(
            summary_data and commits_equivalent(p8_uut, uut, root=root)
        ),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "certification_ready": mode_norm == "final",
        "passing_evidence_rows": len(evidence_manifest),
    }
    _write(outdir / "package-status.json", json.dumps(status, indent=2) + "\n")

    try:
        outdir_rel = str(outdir.relative_to(root))
    except ValueError:
        outdir_rel = str(outdir)

    return {
        "outdir": outdir_rel,
        "mode": mode_norm,
        "uut_commit": uut,
        "harness_commit": harness,
        "certification_summary": str(selected_summary) if selected_summary else None,
        "required_files_ok": all((outdir / p).is_file() for p in REQUIRED_FILES),
        "required_dirs_ok": all((outdir / p).is_dir() for p in REQUIRED_DIRS),
    }


def validate_package(
    outdir: Path,
    *,
    root: Path = _ROOT,
    require_final: bool | None = None,
) -> list[str]:
    """Return validation errors (empty = OK for the declared package mode)."""
    outdir = outdir if outdir.is_absolute() else root / outdir
    errors: list[str] = []
    if not outdir.is_dir():
        return [f"missing package directory: {outdir}"]
    for rel in REQUIRED_FILES:
        if not (outdir / rel).is_file():
            errors.append(f"missing file: {rel}")
    for rel in REQUIRED_DIRS:
        if not (outdir / rel).is_dir():
            errors.append(f"missing dir: {rel}")
    locks = outdir / "dependency-locks" / "requirements.lock.txt"
    if not locks.is_file():
        errors.append("missing dependency-locks/requirements.lock.txt")

    for name in ("known-deviations.md", "security-review.md", "README.md"):
        path = outdir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "all families passing" in text.lower():
            errors.append(f"{name}: forbidden unqualified all-families passing claim")

    notes = outdir / "compliance-matrix.NOTES.md"
    if notes.is_file():
        ntext = notes.read_text(encoding="utf-8")
        if "docs/compliance/evidence/" in ntext and "legacy" not in ntext.lower():
            errors.append(
                "compliance-matrix.NOTES.md: must use canonical compliance/evidence/ "
                "(docs/compliance/evidence/ only as explicit legacy)"
            )
        if "compliance/evidence/" not in ntext:
            errors.append(
                "compliance-matrix.NOTES.md: missing canonical compliance/evidence/ path"
            )

    specs = outdir / "target-specifications.md"
    if specs.is_file():
        stext = specs.read_text(encoding="utf-8")
        sas_lines = [
            ln
            for ln in stext.splitlines()
            if re.search(r"SAS[- ]?to[- ]?SAS|SAS-SAS", ln, re.I)
        ]
        if len(sas_lines) > 1:
            errors.append(
                f"target-specifications.md: duplicated SAS-to-SAS lines ({len(sas_lines)})"
            )

    uut_path = outdir / "uut-commit.txt"
    uut = ""
    if uut_path.is_file():
        uut = uut_path.read_text(encoding="utf-8").strip().splitlines()[0]
        head = _git_rev(root)
        if head and uut != head and uut != "UNKNOWN":
            errors.append(f"uut-commit.txt ({uut}) != git HEAD ({head})")

    status_path = outdir / "package-status.json"
    mode = "preview"
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            mode = str(status.get("mode") or "preview").lower()
        except json.JSONDecodeError:
            errors.append("package-status.json: invalid JSON")
            status = {}
    else:
        status = {}

    if require_final is True:
        mode = "final"

    summary_path = outdir / "results" / "certification_regression_summary.json"
    summary: dict[str, Any] | None = None
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("certification_regression_summary.json: invalid JSON")
        else:
            suut = summary_uut_commit(summary)
            if uut and suut and not commits_equivalent(suut, uut, root=root):
                errors.append(
                    f"UUT campaign mismatch: uut-commit.txt ({uut}) != "
                    f"certification_regression_summary.uut_commit ({suut})"
                )

    if mode == "final":
        if summary is None:
            errors.append(NO_MATCHING_CAMPAIGN)
        else:
            errors.extend(campaign_acceptable_for_final(summary))
            if summary.get("dirty") is True:
                errors.append("FINAL rejected: dirty certification regression campaign")
        # Passing rows require bundled evidence.
        matrix_path = outdir / "compliance-matrix.yaml"
        if matrix_path.is_file():
            for row in load_passing_matrix_rows(matrix_path):
                ev = str(row.get("evidence") or "").strip()
                case_id = row.get("id")
                if not ev:
                    errors.append(f"passing row {case_id}: missing evidence field")
                    continue
                candidates = [
                    outdir / ev,
                    outdir / "evidence" / Path(ev).name,
                ]
                if not any(c.is_file() for c in candidates):
                    errors.append(
                        f"passing row {case_id}: evidence not bundled ({ev})"
                    )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("certification-package"),
        help="Output directory (default: certification-package)",
    )
    parser.add_argument(
        "--mode",
        choices=("preview", "final"),
        default="preview",
        help="PREVIEW allows missing campaign; FINAL requires matching certification regression",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate an existing package",
    )
    parser.add_argument(
        "--require-final",
        action="store_true",
        help="With --validate-only, enforce FINAL rules",
    )
    args = parser.parse_args(argv)
    if args.validate_only:
        errs = validate_package(
            args.outdir, require_final=True if args.require_final else None
        )
        if errs:
            print(json.dumps({"ok": False, "errors": errs}, indent=2))
            return 1
        print(json.dumps({"ok": True, "errors": []}, indent=2))
        return 0

    try:
        meta = build_package(outdir=args.outdir, mode=args.mode)
    except NoMatchingCampaignError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": NO_MATCHING_CAMPAIGN,
                    "detail": str(exc),
                },
                indent=2,
            )
        )
        return 1
    errs = validate_package(
        args.outdir, require_final=(args.mode == "final")
    )
    meta["validation_errors"] = errs
    # Persist errors into package-status.json
    status_path = (
        args.outdir if args.outdir.is_absolute() else _ROOT / args.outdir
    ) / "package-status.json"
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["validation_errors"] = errs
        status["certification_ready"] = args.mode == "final" and not errs
        _write(status_path, json.dumps(status, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
