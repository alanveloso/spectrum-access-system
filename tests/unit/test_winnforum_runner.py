"""Unit tests for the WInnForum harness runner ()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.run_winnforum import build_parser, main
from tools.winnforum.families import resolve_unittest_targets
from tools.winnforum.junit import write_junit_xml
from tools.winnforum.runner import ensure_harness_checkout, install_sas_cfg, RunnerConfig
from tools.winnforum.sas_cfg import default_sas_cfg_params, render_sas_cfg, write_sas_cfg
from tools.winnforum.unittest_parse import parse_unittest_output
from tests.support.repo import REPO_ROOT


SAMPLE_LOG = """\
test_WINNF_FT_S_REG_1 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... ok
test_WINNF_FT_S_REG_2 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... FAIL
test_WINNF_FT_S_REG_3 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... ERROR

======================================================================
FAIL: test_WINNF_FT_S_REG_2
----------------------------------------------------------------------
AssertionError: expected

----------------------------------------------------------------------
Ran 3 tests in 1.234s

FAILED (failures=1, errors=1)
"""


def test_resolve_family_module():
    targets = resolve_unittest_targets(["REG"], None)
    assert len(targets) == 1
    assert targets[0].module == "testcases.WINNF_FT_S_REG_testcase"
    assert targets[0].method is None


def test_resolve_case_shorthand():
    targets = resolve_unittest_targets(None, ["REG.1"])
    assert len(targets) == 1
    assert targets[0].module == "testcases.WINNF_FT_S_REG_testcase"
    assert targets[0].method == "test_WINNF_FT_S_REG_1"
    assert "::" in targets[0].label()


def test_resolve_unknown_family_raises():
    with pytest.raises(ValueError, match="unknown family"):
        resolve_unittest_targets(["ZZZ"], None)


def test_parse_unittest_output_counts():
    result = parse_unittest_output(SAMPLE_LOG)
    assert result.tests_run == 3
    assert result.duration_seconds == 1.234
    assert result.raw_ok is False
    assert result.to_dict()["passed"] == 1
    assert result.to_dict()["failed"] == 1
    assert result.to_dict()["error"] == 1


def test_render_sas_cfg_has_required_keys():
    text = render_sas_cfg(default_sas_cfg_params(host="127.0.0.1", rsa_port=9000))
    assert "AdminApiBaseUrl: 127.0.0.1:9000" in text
    assert "CbsdSasVersion: v1.2" in text
    assert "SasSasVersion: v1.3" in text
    assert "CbsdSas" in text


def test_write_junit_xml(tmp_path: Path):
    result = parse_unittest_output(SAMPLE_LOG)
    path = tmp_path / "junit.xml"
    write_junit_xml(result, path, suite_name="demo")
    xml = path.read_text(encoding="utf-8")
    assert 'failures="1"' in xml
    assert 'errors="1"' in xml
    assert "test_WINNF_FT_S_REG_1" in xml


def test_cli_dry_run_writes_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(REPO_ROOT)
    artifacts = tmp_path / "artifacts"
    rc = main(
        [
            "--dry-run",
            "--family",
            "REG",
            "--artifacts-root",
            str(artifacts),
        ]
    )
    assert rc == 0
    runs = list(artifacts.iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    for name in (
        "environment.json",
        "sas.cfg",
        "uut.log",
        "harness.log",
        "results.json",
        "junit.xml",
        "summary.md",
    ):
        assert (run_dir / name).is_file(), name
    env = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    assert env["dry_run"] is True
    assert env["targets"] == ["testcases.WINNF_FT_S_REG_testcase"]
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert results["status"] == "dry_run"
    assert results.get("raw_ok") in (None, False) or results["tests_run"] in (0, None)


def test_cli_requires_family_or_case():
    rc = main(["--dry-run"])
    assert rc == 2


def test_parser_help_smoke():
    parser = build_parser()
    assert parser.parse_args(["--dry-run", "--family", "HBT"]).family == ["HBT"]


def test_blocked_without_explicit_cert_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(REPO_ROOT)
    for key in (
        "WINNFORUM_CLIENT_CERT",
        "WINNFORUM_CLIENT_KEY",
        "WINNFORUM_CA_CERTS",
    ):
        monkeypatch.delenv(key, raising=False)
    harness = tmp_path / "harness"
    (harness / "src" / "harness").mkdir(parents=True)
    artifacts = tmp_path / "out"
    rc = main(
        [
            "--harness-dir",
            str(harness),
            "--family",
            "REG",
            "--artifacts-root",
            str(artifacts),
            "--skip-healthcheck",
        ]
    )
    assert rc == 2
    run_dir = next(artifacts.iterdir())
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert results["status"] == "blocked_missing_certs"
    # Must not invent hardcoded admin.cert defaults in notes.
    notes = " ".join(results["notes"])
    assert "admin.cert" not in notes


def test_install_sas_cfg_restores_previous(tmp_path: Path):
    workdir = tmp_path / "harness"
    workdir.mkdir()
    original = workdir / "sas.cfg"
    original.write_text("ORIGINAL\n", encoding="utf-8")
    generated = tmp_path / "generated.cfg"
    write_sas_cfg(generated, default_sas_cfg_params())
    with install_sas_cfg(workdir, generated):
        assert "AdminApiBaseUrl" in original.read_text(encoding="utf-8")
    assert original.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_refuse_checkout_on_user_harness_dir(tmp_path: Path):
    harness = tmp_path / "harness"
    harness.mkdir()
    cfg = RunnerConfig(
        repo_root=REPO_ROOT,
        artifacts_root=tmp_path / "art",
        harness_dir=harness,
        harness_ref="deadbeef",
        update_harness_ref=False,
    )
    with pytest.raises(RuntimeError, match="refusing to git checkout"):
        ensure_harness_checkout(cfg, [])


def test_prepare_artifact_dir_avoids_same_stamp_collision(tmp_path: Path):
    from tools.winnforum.runner import prepare_artifact_dir

    first = prepare_artifact_dir(tmp_path, stamp="fixedstamp")
    second = prepare_artifact_dir(tmp_path, stamp="fixedstamp")
    assert first.directory == tmp_path / "fixedstamp"
    assert second.directory == tmp_path / "fixedstamp_1"
    assert first.directory.is_dir() and second.directory.is_dir()


def test_source_has_no_hardcoded_admin_cert_basename():
    text = (REPO_ROOT / "tools" / "winnforum" / "runner.py").read_text(encoding="utf-8")
    assert "admin.cert" not in text
    assert "admin.key" not in text
