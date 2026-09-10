"""Unit tests for  regression parsing, flakes, and gates."""

from __future__ import annotations

from pathlib import Path

from tools.winnforum.certification_regression import (
    RunResult,
    analyze_flakes,
    compute_verdict,
    evaluate_postgres_gate,
    junit_case_id,
    parse_junit_cases,
    parse_pytest_summary,
    run_succeeded,
)


def _rr(
    label: str,
    *,
    exit_code: int = 0,
    passed: int | None = 10,
    skipped: int | None = 0,
    failed: int | None = 0,
    errors: int | None = 0,
    junit_path: str | None = None,
) -> RunResult:
    return RunResult(
        label=label,
        exit_code=exit_code,
        passed=passed,
        skipped=skipped,
        failed=failed,
        errors=errors,
        duration_s=1.0,
        summary_line="",
        tz="UTC",
        log_path=f"{label}.log",
        junit_path=junit_path,
    )


def test_parse_pytest_summary_counts():
    text = """
.....
===== warnings =====
834 passed, 7 skipped, 48 warnings in 71.17s
"""
    passed, skipped, failed, errors, line = parse_pytest_summary(text)
    assert passed == 834
    assert skipped == 7
    assert failed == 0
    assert errors == 0
    assert "834 passed" in line


def test_parse_failed_before_passed_order():
    text = "1 failed, 49 passed, 1 skipped in 1.0s\n"
    passed, skipped, failed, errors, _ = parse_pytest_summary(text)
    assert passed == 49
    assert failed == 1
    assert skipped == 1
    assert errors == 0


def test_parse_failed_passed_error_order():
    text = "2 failed, 10 passed, 1 error in 1.0s\n"
    passed, skipped, failed, errors, _ = parse_pytest_summary(text)
    assert passed == 10
    assert failed == 2
    assert errors == 1


def test_parse_passed_only():
    passed, skipped, failed, errors, _ = parse_pytest_summary("10 passed in 0.1s\n")
    assert passed == 10
    assert failed == 0
    assert skipped == 0
    assert errors == 0


def test_parse_skipped_before_passed():
    passed, skipped, failed, errors, _ = parse_pytest_summary(
        "3 skipped, 12 passed in 0.2s\n"
    )
    assert passed == 12
    assert skipped == 3
    assert failed == 0


def test_parse_error_during_collection():
    passed, skipped, failed, errors, _ = parse_pytest_summary(
        "!!!!!!!!!!!!!!!!!!! error during collection !!!!!!!!!!!!!!!!!!!\n"
        "1 error during collection\n"
    )
    assert errors is not None and errors >= 1


def test_exit_code_authoritative_when_counts_missing():
    r = _rr("full_1", exit_code=1, passed=None, failed=None, errors=None)
    assert run_succeeded(r) is False


def test_postgres_exit_code_nonzero_blocks_even_without_counts():
    r = _rr(
        "postgres_integrations",
        exit_code=2,
        passed=None,
        failed=None,
        errors=None,
    )
    gate = evaluate_postgres_gate(r)
    assert gate["ok"] is False
    assert gate["classification"] in {"UNKNOWN", "FAIL_PRODUCT", "BLOCKED_BY_ENV"}


def test_postgres_exit_zero_ok():
    r = _rr("postgres_integrations", exit_code=0, failed=0, errors=0)
    assert evaluate_postgres_gate(r)["ok"] is True


def test_full_tz_excluded_from_flake_comparison():
    cases = {
        "full_1": {junit_case_id("t", "a"): "PASS"},
        "full_2": {junit_case_id("t", "a"): "PASS"},
        "full_3": {junit_case_id("t", "a"): "PASS"},
    }
    runs = [
        _rr("full_1", passed=1),
        _rr("full_2", passed=1),
        _rr("full_3", passed=1),
        _rr("full_tz_america_los_angeles", passed=0, skipped=1),
    ]
    report = analyze_flakes(runs, junit_cases=cases)
    assert report["comparable_full_runs"] == 3
    assert report["product_regression_ok"] is True


def test_equal_counts_but_testcase_divergence_detected():
    """Aggregate counts equal must NOT hide swapped PASS/SKIP flakes."""
    a = junit_case_id("mod", "test_A")
    b = junit_case_id("mod", "test_B")
    cases = {
        "full_1": {a: "PASS", b: "SKIP"},
        "full_2": {a: "SKIP", b: "PASS"},
        "full_3": {a: "PASS", b: "SKIP"},
    }
    runs = [
        _rr("full_1", passed=1, skipped=1),
        _rr("full_2", passed=1, skipped=1),
        _rr("full_3", passed=1, skipped=1),
    ]
    report = analyze_flakes(runs, junit_cases=cases)
    assert report["counts_stable"] is True
    assert report["product_regression_ok"] is False
    assert report["flake_unknown"] >= 1
    assert any(i["testcase"] == a for i in report["inconsistencies"])


def test_analyze_flakes_stable_with_junit():
    tid = junit_case_id("mod", "test_ok")
    cases = {
        "full_1": {tid: "PASS"},
        "full_2": {tid: "PASS"},
    }
    runs = [_rr("full_1", passed=838, skipped=7), _rr("full_2", passed=838, skipped=7)]
    report = analyze_flakes(runs, junit_cases=cases)
    assert report["stable"] is True
    assert report["comparable_full_runs"] == 2


def test_analyze_flakes_detects_count_divergence():
    tid = junit_case_id("mod", "t")
    cases = {
        "full_1": {tid: "PASS"},
        "full_2": {tid: "PASS"},
    }
    runs = [
        _rr("full_1", passed=838, skipped=7),
        _rr("full_2", passed=837, skipped=8),
    ]
    report = analyze_flakes(runs, junit_cases=cases)
    assert report["counts_stable"] is False
    assert report["stable"] is False


def test_junit_missing_blocks_product_regression(tmp_path: Path):
    runs = [_rr("full_1"), _rr("full_2")]
    report = analyze_flakes(runs, root=tmp_path)
    assert report["junit_missing"] == ["full_1", "full_2"]
    assert report["product_regression_ok"] is False


def test_testcase_missing_between_runs():
    a = junit_case_id("mod", "only_in_1")
    b = junit_case_id("mod", "shared")
    cases = {
        "full_1": {a: "PASS", b: "PASS"},
        "full_2": {b: "PASS"},
        "full_3": {b: "PASS"},
    }
    runs = [_rr("full_1"), _rr("full_2"), _rr("full_3")]
    report = analyze_flakes(runs, junit_cases=cases)
    assert report["product_regression_ok"] is False
    assert any(
        i["testcase"] == a and i["states"].get("full_2") == "MISSING"
        for i in report["inconsistencies"]
    )


def test_parse_junit_cases(tmp_path: Path):
    xml = tmp_path / "full_1.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3">
  <testcase classname="t.mod" name="test_ok" time="0.1"/>
  <testcase classname="t.mod" name="test_fail" time="0.1">
    <failure message="x">boom</failure>
  </testcase>
  <testcase classname="t.mod" name="test_skip" time="0.0">
    <skipped type="pytest.skip" message="env"/>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    cases = parse_junit_cases(xml)
    assert cases[junit_case_id("t.mod", "test_ok")] == "PASS"
    assert cases[junit_case_id("t.mod", "test_fail")] == "FAIL"
    assert cases[junit_case_id("t.mod", "test_skip")] == "SKIP"


def test_dirty_campaign_cannot_be_final_pass():
    flake = {"product_regression_ok": True}
    results = [_rr("full_1"), _rr("full_2"), _rr("full_3")]
    verdict = compute_verdict(
        dirty=True,
        flake=flake,
        results=results,
        postgres_gate={"ok": True},
    )
    assert verdict == "ABORTED_DIRTY"
    assert verdict != "PASS_LOCAL"
