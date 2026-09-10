""" certification-package integrity checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.winnforum.certification_package import (
    NO_MATCHING_CAMPAIGN,
    NoMatchingCampaignError,
    build_package,
    campaign_acceptable_for_final,
    commits_equivalent,
    select_certification_regression_summary_for_uut,
    target_specifications_text,
    validate_package,
)

_ROOT = Path(__file__).resolve().parents[2]


def _git_head(root: Path = _ROOT) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


def _write_summary(
    path: Path,
    *,
    uut: str,
    dirty: bool = False,
    verdict: str = "PASS_LOCAL",
    with_fulls: bool = True,
) -> None:
    runs = []
    if with_fulls:
        for i in range(1, 4):
            runs.append(
                {
                    "run_name": f"full_{i}",
                    "label": f"full_{i}",
                    "exit_code": 0,
                    "passed": 10,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task": "",
                "campaign_id": path.parent.name,
                "timestamp": "2026-08-08T00:00:00+00:00",
                "uut_commit": uut[:7],
                "uut_commit_full": uut,
                "dirty": dirty,
                "verdict": verdict,
                "product_regression_verdict": verdict,
                "flake_analysis": {
                    "product_regression_ok": verdict == "PASS_LOCAL",
                    "stable": verdict == "PASS_LOCAL",
                },
                "runs": runs,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_package_preview_layout(tmp_path: Path):
    out = tmp_path / "certification-package"
    meta = build_package(outdir=out, root=_ROOT, mode="preview", summaries=[])
    assert meta["required_files_ok"] is True
    assert meta["required_dirs_ok"] is True
    assert meta["mode"] == "preview"
    errs = validate_package(out, root=_ROOT)
    assert errs == [], errs
    status = json.loads((out / "package-status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "preview"
    assert status["certification_ready"] is False
    notes = (out / "compliance-matrix.NOTES.md").read_text(encoding="utf-8")
    assert "compliance/evidence/" in notes
    assert "PASS_OFFICIAL" in notes
    manifest = json.loads((out / "datasets-manifest.json").read_text(encoding="utf-8"))
    assert manifest["spectrum_profile"] == "cbrs_winnforum"
    ev_manifest = json.loads(
        (out / "evidence" / "passing-evidence-manifest.json").read_text(encoding="utf-8")
    )
    assert ev_manifest["passing_row_count"] == 58
    # Factual: 56 P3 + 2 P5
    sources = [r["source_evidence"] for r in ev_manifest["rows"]]
    assert sources.count("compliance/evidence/winnforum_scs_sds_sss_harness.md") == 56
    assert sources.count("compliance/evidence/winnforum_fad_harness.md") == 2
    assert all(
        r["classification"].startswith("HISTORICAL") or r["classification"] == "CURRENT_UUT"
        for r in ev_manifest["rows"]
    )
    assert (out / "compliance/evidence/winnforum_scs_sds_sss_harness.md").is_file()
    assert (out / "compliance/evidence/winnforum_fad_harness.md").is_file()


def test_validate_detects_missing_file(tmp_path: Path):
    out = tmp_path / "pkg"
    build_package(outdir=out, root=_ROOT, mode="preview", summaries=[])
    (out / "security-review.md").unlink()
    errs = validate_package(out, root=_ROOT)
    assert any("security-review.md" in e for e in errs)


def test_select_matching_uut_ignores_newer_wrong_uut(tmp_path: Path):
    head = _git_head()
    other = "b" * 40
    older = tmp_path / "certification_regression_20260101T000000Z" / "summary.json"
    newer = tmp_path / "certification_regression_20261231T000000Z" / "summary.json"
    _write_summary(older, uut=head)
    _write_summary(newer, uut=other)
    selected = select_certification_regression_summary_for_uut(
        _ROOT, head, summaries=[older, newer]
    )
    assert selected == older


def test_no_matching_campaign_blocks_final(tmp_path: Path):
    only_other = tmp_path / "certification_regression_x" / "summary.json"
    _write_summary(only_other, uut="c" * 40)
    out = tmp_path / "pkg"
    with pytest.raises(NoMatchingCampaignError) as exc:
        build_package(
            outdir=out,
            root=_ROOT,
            mode="final",
            summaries=[only_other],
        )
    assert NO_MATCHING_CAMPAIGN in str(exc.value) or NO_MATCHING_CAMPAIGN in exc.value.args[0]


def test_matching_summary_accepted_for_final(tmp_path: Path):
    head = _git_head()
    summary = tmp_path / "certification_regression_ok" / "summary.json"
    _write_summary(summary, uut=head, dirty=False, verdict="PASS_LOCAL")
    out = tmp_path / "pkg"
    meta = build_package(
        outdir=out, root=_ROOT, mode="final", summary_path=summary, summaries=[summary]
    )
    assert meta["mode"] == "final"
    errs = validate_package(out, root=_ROOT, require_final=True)
    assert errs == [], errs
    bundled = json.loads(
        (out / "results" / "certification_regression_summary.json").read_text(encoding="utf-8")
    )
    assert commits_equivalent(bundled["uut_commit_full"], head, root=_ROOT)


def test_short_and_full_hash_equivalent():
    head = _git_head()
    assert commits_equivalent(head, head[:7], root=_ROOT)
    assert commits_equivalent(head[:7], head, root=_ROOT)


def test_validator_cross_uut_mismatch(tmp_path: Path):
    out = tmp_path / "pkg"
    build_package(outdir=out, root=_ROOT, mode="preview", summaries=[])
    # Inject mismatched summary after build.
    (out / "results" / "certification_regression_summary.json").write_text(
        json.dumps(
            {
                "uut_commit": "1949e6e",
                "uut_commit_full": "1949e6e4edff09d14eef66e18c460e1afd3eccaa",
                "dirty": False,
                "verdict": "PASS_LOCAL",
                "campaign_id": "x",
                "timestamp": "2026-08-08T00:00:00+00:00",
                "flake_analysis": {"product_regression_ok": True},
                "runs": [
                    {"run_name": "full_1"},
                    {"run_name": "full_2"},
                    {"run_name": "full_3"},
                ],
            }
        ),
        encoding="utf-8",
    )
    # Force status final for stricter check path + mismatch always checked
    status = json.loads((out / "package-status.json").read_text(encoding="utf-8"))
    status["mode"] = "final"
    (out / "package-status.json").write_text(json.dumps(status), encoding="utf-8")
    errs = validate_package(out, root=_ROOT, require_final=True)
    assert any("UUT campaign mismatch" in e for e in errs), errs


def test_dirty_summary_rejected_for_final():
    errs = campaign_acceptable_for_final(
        {
            "dirty": True,
            "verdict": "PASS_LOCAL",
            "campaign_id": "x",
            "timestamp": "t",
            "flake_analysis": {},
            "runs": [
                {"run_name": "full_1"},
                {"run_name": "full_2"},
                {"run_name": "full_3"},
            ],
        }
    )
    assert any("dirty" in e for e in errs)


def test_aborted_summary_rejected_for_final():
    errs = campaign_acceptable_for_final(
        {
            "dirty": False,
            "verdict": "ABORTED_DIRTY",
            "campaign_id": "x",
            "timestamp": "t",
            "flake_analysis": {},
            "runs": [
                {"run_name": "full_1"},
                {"run_name": "full_2"},
                {"run_name": "full_3"},
            ],
        }
    )
    assert errs


def test_final_missing_passing_evidence_invalid(tmp_path: Path):
    head = _git_head()
    summary = tmp_path / "sum" / "summary.json"
    _write_summary(summary, uut=head)
    out = tmp_path / "pkg"
    build_package(
        outdir=out, root=_ROOT, mode="final", summary_path=summary, summaries=[summary]
    )
    # Remove bundled evidence while keeping matrix passing rows.
    ev = out / "compliance" / "evidence" / "winnforum_scs_sds_sss_harness.md"
    if ev.is_file():
        ev.unlink()
    errs = validate_package(out, root=_ROOT, require_final=True)
    assert any("evidence not bundled" in e for e in errs), errs


def test_preview_allowed_without_campaign(tmp_path: Path):
    out = tmp_path / "pkg"
    meta = build_package(outdir=out, root=_ROOT, mode="preview", summaries=[])
    assert meta["mode"] == "preview"
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "PREVIEW" in readme
    assert validate_package(out, root=_ROOT) == []


def test_canonical_evidence_path_in_notes(tmp_path: Path):
    out = tmp_path / "pkg"
    build_package(outdir=out, root=_ROOT, mode="preview", summaries=[])
    notes = (out / "compliance-matrix.NOTES.md").read_text(encoding="utf-8")
    assert "compliance/evidence/" in notes
    # legacy mention allowed only as legacy
    if "docs/compliance/evidence/" in notes:
        assert "legacy" in notes.lower()


def test_target_specifications_sas_to_sas_unique():
    text = target_specifications_text("a" * 40, "aaaaaaa", "b" * 40, "bbbbbbb")
    sas_lines = [
        ln
        for ln in text.splitlines()
        if "SAS-to-SAS" in ln or "SAS-SAS" in ln
    ]
    assert len(sas_lines) == 1


def test_repo_certification_package_if_present():
    """PREVIEW/STALE packages that do not match HEAD are skipped."""
    pkg = _ROOT / "certification-package"
    if not pkg.is_dir():
        pytest.skip("certification-package not generated yet")
    uut_path = pkg / "uut-commit.txt"
    if uut_path.is_file():
        head = _git_head()
        uut = uut_path.read_text(encoding="utf-8").strip().splitlines()[0]
        if uut != head:
            pytest.skip(
                "certification-package is PREVIEW/STALE (uut-commit != HEAD); "
                "rebuild after for FINAL"
            )
    errs = validate_package(pkg, root=_ROOT)
    assert errs == [], errs


def test_matrix_passing_evidence_paths_factual():
    data = yaml.safe_load(
        (_ROOT / "compliance" / "matrix.yaml").read_text(encoding="utf-8")
    )
    cases = data["cases"]
    passing = [c for c in cases if c.get("status") == "passing"]
    assert len(passing) == 58
    from collections import Counter

    counts = Counter(c.get("evidence") for c in passing)
    assert counts["compliance/evidence/winnforum_scs_sds_sss_harness.md"] == 56
    assert counts["compliance/evidence/winnforum_fad_harness.md"] == 2
