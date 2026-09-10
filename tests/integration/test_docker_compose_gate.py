"""Docker Compose gate checks that require no project .env file."""

from __future__ import annotations

import shutil
import subprocess

from tests.support.repo import REPO_ROOT as ROOT


def _require_docker_compose() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("docker executable not found")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"docker compose unavailable: {probe.stderr or probe.stdout}")


def test_compose_config_works_with_builtin_defaults():
    """Gate: `docker compose config` succeeds without requiring a project .env."""
    _require_docker_compose()
    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "api:" in result.stdout
    assert "worker:" in result.stdout
    assert "@db:5432/" in result.stdout
    assert "sqlite:" not in result.stdout


def test_compose_env_file_is_optional():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "required: false" in text
    assert "path: .env" in text
    # In-stack DB URL must be composed from service DNS, not host sqlite.
    assert "@db:5432/" in text or "@db:5432" in text


def test_env_example_keeps_host_sqlite_default():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:" in text
    assert "@db:5432" not in text
