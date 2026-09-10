"""Contract: scheduled WInnForum workflow is regression-only (not a merge gate)."""

from __future__ import annotations

import yaml

from tests.support.repo import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "winnforum-scheduled.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_on(data: dict) -> dict:
    # PyYAML may parse unquoted `on:` as boolean True.
    return data.get("on") or data.get(True) or {}


def test_scheduled_winnforum_workflow_exists_and_is_not_merge_gate():
    assert WORKFLOW.is_file()
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert data["name"] == "winnforum-scheduled"
    triggers = _workflow_on(data)
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers
    assert "push" not in triggers
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "NOT a merge gate" in text
    assert "NOT certification" in text
    assert "Does not invent PASS" in text or "does not invent PASS" in text
    assert "FDB.8" in text
    assert "America/Los_Angeles" in text or "wait_la_cpas_window" in text
    assert "continue-on-error: true" in text


def test_scheduled_workflow_is_separate_from_ci_required_jobs():
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert "winnforum-scheduled" not in ci["jobs"]
    assert "official-harness-near-la-window" not in ci.get("jobs", {})


def test_prepare_and_window_scripts_exist():
    prepare = REPO_ROOT / "tools" / "winnforum" / "ci_prepare_scheduled.sh"
    wait = REPO_ROOT / "tools" / "winnforum" / "wait_la_cpas_window.sh"
    assert prepare.is_file()
    assert wait.is_file()
    assert "928c3150" in prepare.read_text(encoding="utf-8")
    assert "America/Los_Angeles" in wait.read_text(encoding="utf-8")
