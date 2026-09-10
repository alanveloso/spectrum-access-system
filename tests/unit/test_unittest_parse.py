"""Tests for unittest harness output parsing ( review)."""

from __future__ import annotations

from tools.winnforum.unittest_parse import parse_unittest_output


def test_parse_same_line_ok():
    text = (
        "test_WINNF_FT_S_SCS_1 (mod.Cls.test_WINNF_FT_S_SCS_1) ... ok\n"
        "Ran 1 test in 0.100s\n"
        "OK\n"
    )
    result = parse_unittest_output(text)
    assert result.tests_run == 1
    assert result.raw_ok is True
    assert len(result.cases) == 1
    assert result.cases[0].status == "passed"


def test_parse_deferred_ok_after_openssl_noise():
    """OpenSSL cert tooling may print '+' progress on the result line."""
    text = (
        "test_WINNF_FT_S_SCS_17 (mod.Cls.test_WINNF_FT_S_SCS_17) ... .....+......+\n"
        "Using configuration from openssl.cnf\n"
        "Signature ok\n"
        "ok\n"
        "test_WINNF_FT_S_SCS_18 (mod.Cls.test_WINNF_FT_S_SCS_18) ... ok\n"
        "Ran 2 tests in 1.000s\n"
        "OK\n"
    )
    result = parse_unittest_output(text)
    assert result.tests_run == 2
    assert result.raw_ok is True
    assert [c.name for c in result.cases] == [
        "test_WINNF_FT_S_SCS_17",
        "test_WINNF_FT_S_SCS_18",
    ]
    assert all(c.status == "passed" for c in result.cases)


def test_parse_multiline_docstring_then_ok():
    text = (
        "test_WINNF_FT_S_SCS_6_0_default (mod.Cls.test_WINNF_FT_S_SCS_6_0_default)\n"
        "Unrecognized root of trust certificate presented during registration. ... ok\n"
        "Ran 1 test in 0.200s\n"
        "OK\n"
    )
    result = parse_unittest_output(text)
    assert len(result.cases) == 1
    assert result.cases[0].name == "test_WINNF_FT_S_SCS_6_0_default"
    assert result.cases[0].status == "passed"


def test_parse_real_gate_log_scs17_not_unexpected():
    from pathlib import Path

    log = Path(
        "artifacts/winnforum/p3_gate_20260807T130841Z/official/20260807T131016Z/harness.log"
    )
    if not log.is_file():
        return
    result = parse_unittest_output(log.read_text(errors="replace"))
    by_name = {c.name: c.status for c in result.cases}
    assert by_name.get("test_WINNF_FT_S_SCS_17") == "passed"
    assert by_name.get("test_WINNF_FT_S_SCS_18") == "passed"
    assert by_name.get("test_WINNF_FT_S_SCS_19") == "passed"
    assert result.raw_ok is True
    assert result.tests_run == 56
    # Configurable methods should also resolve (not only the 19 same-line oks).
    assert by_name.get("test_WINNF_FT_S_SCS_6_0_default") == "passed"
    assert len(result.cases) >= 50
