"""Preflight helpers for (no secrets)."""

from __future__ import annotations

from pathlib import Path

from tools.winnforum.preflight import PreflightCode, harness_certs_dir


def test_preflight_codes_are_distinct():
    values = {c.value for c in PreflightCode}
    assert "MISSING_CHECKOUT" in values
    assert "MISSING_CERTIFICATE" in values
    assert "TLS_FAILURE" in values
    assert "ADMIN_API_FAILURE" in values
    assert len(values) >= 8


def test_harness_certs_dir_prefers_src_harness_certs(tmp_path: Path):
    root = tmp_path / "harness"
    preferred = root / "src" / "harness" / "certs"
    preferred.mkdir(parents=True)
    legacy = root / "src" / "harness" / "testcases" / "testdata" / "certs"
    legacy.mkdir(parents=True)
    assert harness_certs_dir(root) == preferred
