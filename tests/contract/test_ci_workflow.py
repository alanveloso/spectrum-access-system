"""Contract: CI workflow covers  gate jobs (no invented PASS)."""

from __future__ import annotations

import yaml

from tests.support.repo import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REQUIRED_JOBS = {
    "lint",
    "typecheck",
    "unit",
    "integration-sqlite",
    "integration-postgres",
    "runtime-311",
    "docker",
    "smoke-mtls",
    "winnforum-subset-dry-run",
}


def test_ci_workflow_exists_and_lists_required_jobs():
    assert WORKFLOW.is_file(), "requires .github/workflows/ci.yml"
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert data["name"] == "ci"
    jobs = set(data["jobs"])
    missing = REQUIRED_JOBS - jobs
    assert not missing, sorted(missing)
    # Avoid a misleading job id that looks like an official harness execution.
    assert "winnforum-subset" not in jobs


def test_ci_workflow_uploads_winnforum_artifacts_without_claiming_pass():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "upload-artifact" in text
    assert "winnforum-dry-run" in text
    assert "status=passing" not in text
    assert "not an official suite run" in text


def test_ci_workflow_runs_postgres_and_mtls_smoke():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "SAS_TEST_DATABASE_URL" in text
    assert "test_fad_publish_postgres.py" in text
    assert "test_cpas_multi_sas_postgres.py" in text
    assert "test_concurrency_postgres.py" in text
    assert "tools.generate_dev_certs" in text
    assert "tools.smoke_mtls" in text
    assert "ruff check" in text
    assert "mypy compliance tools" in text


def test_ci_workflow_fetches_ntia_catalogue_for_unit_job():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "fetch_ntia_catalogue.sh" in text
    fetch = REPO_ROOT / "tools" / "winnforum" / "fetch_ntia_catalogue.sh"
    assert fetch.is_file()
    body = fetch.read_text(encoding="utf-8")
    assert "928c3150" in body
    assert "protection_zones.kml" in body


def test_ci_install_is_lock_first_before_editable_packages():
    """Resolve the lock before editable installs so pins are not rewritten."""
    text = WORKFLOW.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    for job_id, job in data["jobs"].items():
        steps = job.get("steps") or []
        install_runs = [
            step.get("run", "")
            for step in steps
            if isinstance(step, dict)
            and "pip install" in str(step.get("run", ""))
            and (
                "spectrum-propagation" in str(step.get("run", ""))
                or "pip install -e ." in str(step.get("run", ""))
            )
        ]
        for run in install_runs:
            lock_idx = run.find("requirements.lock.txt")
            assert lock_idx >= 0, f"{job_id}: missing requirements.lock.txt"
            # Editable product / plugin installs that resolve deps must follow the lock.
            for needle in (
                "pip install ./plugins/spectrum-propagation",
                "pip install -e .",
            ):
                idx = run.find(needle)
                if idx < 0:
                    continue
                assert idx > lock_idx, f"{job_id}: {needle!r} before lock"
            if "spectrum-propagation" in run:
                assert "--no-deps" in run, f"{job_id}: spectrum-propagation must use --no-deps"
            # Product editable install after lock should also be --no-deps when present.
            if "pip install -e . --no-deps" not in run and "pip install -e ." in run:
                # Allow test plugin -e installs; the root package itself must be --no-deps.
                lines = [ln.strip() for ln in run.splitlines() if ln.strip().startswith("pip ")]
                root_edits = [ln for ln in lines if ln.startswith("pip install -e .")]
                assert root_edits, f"{job_id}: expected root editable install"
                assert all("--no-deps" in ln for ln in root_edits), (
                    f"{job_id}: root editable install must use --no-deps"
                )


def test_ci_protects_python_311_runtime_gate():
    """Accepted Docker/WInnForum runtime is 3.11; CI must not be 3.12-only."""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = data["jobs"]["runtime-311"]
    setup = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python")
    )
    assert setup["with"]["python-version"] == "3.11"
    # Existing forward-compat jobs remain on 3.12.
    unit_setup = next(
        step
        for step in data["jobs"]["unit"]["steps"]
        if step.get("uses", "").startswith("actions/setup-python")
    )
    assert unit_setup["with"]["python-version"] == "3.12"
