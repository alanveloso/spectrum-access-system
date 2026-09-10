"""Smoke tests for SAS startup, profile selection, certs and imports."""

from __future__ import annotations

import importlib
import os
import pkgutil
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from config import clear_settings_cache
from profiles import load_profile, primary_spectrum_range

from tests.support.repo import REPO_ROOT as ROOT
PYTHON = sys.executable

# Settings keys that would otherwise leak host TLS paths into smoke tests.
_TLS_ENV_KEYS = (
    "CERTS_DIR",
    "SSL_CERTFILE",
    "SSL_KEYFILE",
    "SSL_ECC_CERTFILE",
    "SSL_ECC_KEYFILE",
    "SSL_CA_CERTS",
    "SSL_CRL_DIR",
    "CLIENT_CERTFILE",
    "CLIENT_KEYFILE",
)


def _scrub_tls_env(env: dict[str, str]) -> dict[str, str]:
    for key in _TLS_ENV_KEYS:
        env.pop(key, None)
    return env


def _run_startup(
    *,
    database_url: str,
    sas_profile: str = "cbrs_winnforum",
    extra_env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    env = _scrub_tls_env(os.environ.copy())
    env["DATABASE_URL"] = database_url
    env["SAS_PROFILE"] = sas_profile
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [PYTHON, "-c", "import main; main.on_startup()"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'startup_smoke.db'}"


def test_import_all_project_modules():
    """Every first-party package/module under the repo root must import cleanly."""
    package_roots = [
        "models",
        "routes",
        "schemas",
        "services",
        "profiles",
        "protection_data",
        "runtime",
        "adapters",
        "providers",
        "rf",
        "primitives",
    ]
    failures: list[str] = []

    for name in ["main", "config", "database", "celery_app", "tasks", *package_roots]:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - collect all import failures
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    for package_name in package_roots:
        package = importlib.import_module(package_name)
        prefix = package.__name__ + "."
        for module in pkgutil.walk_packages(package.__path__, prefix):
            try:
                importlib.import_module(module.name)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{module.name}: {type(exc).__name__}: {exc}")

    assert not failures, "import failures:\n" + "\n".join(failures)


def test_startup_sqlite_default_profile(sqlite_url: str):
    expected = load_profile("cbrs_winnforum")
    band = primary_spectrum_range(expected)
    result = _run_startup(database_url=sqlite_url, sas_profile="cbrs_winnforum")
    output = _combined_output(result)
    assert result.returncode == 0, output
    assert "Active spectrum profile: cbrs_winnforum" in result.stdout
    assert "Runtime composition: deployment=reference" in result.stdout
    assert "rule=winnforum_cbrs_baseline_v1" in result.stdout
    assert f"{band.low_hz}-{band.high_hz}" in result.stdout


def test_startup_invalid_profile_fails(sqlite_url: str):
    result = _run_startup(
        database_url=sqlite_url,
        sas_profile="profile_does_not_exist",
    )
    output = _combined_output(result)
    assert result.returncode != 0, output
    assert "profile_does_not_exist" in output
    assert "ProfileNotFoundError" in output or "not found" in output.lower()


def test_entrypoint_missing_certificates_exits_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import main

    for key in _TLS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CERTS_DIR", str(tmp_path))
    clear_settings_cache()
    try:
        with pytest.raises(SystemExit) as excinfo:
            main.main()
    finally:
        clear_settings_cache()

    message = str(excinfo.value)
    assert "Certificados TLS incompletos" in message
    assert "CERTS_DIR=" in message
    assert "server.cert" in message or "ca.cert" in message or "server.key" in message


def test_entrypoint_missing_certificates_subprocess(tmp_path: Path, sqlite_url: str):
    env = _scrub_tls_env(os.environ.copy())
    env["DATABASE_URL"] = sqlite_url
    env["CERTS_DIR"] = str(tmp_path)
    env["SAS_PROFILE"] = "cbrs_winnforum"
    result = subprocess.run(
        [PYTHON, "-c", "import main; main.main()"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = _combined_output(result)
    assert result.returncode != 0, output
    assert "Certificados TLS incompletos" in output


def _postgres_url_from_env() -> str | None:
    return os.environ.get("SAS_TEST_DATABASE_URL") or None


def _start_ephemeral_postgres() -> tuple[str, str]:
    """Start a disposable Postgres via Docker. Raises RuntimeError on failure."""
    if shutil.which("docker") is None:
        raise RuntimeError("docker executable not found")

    container = f"sas-startup-postgres-{os.getpid()}"
    user, password, db = "sas", "sas_test", "sas"
    host_port = str(55432 + (os.getpid() % 1000))
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={db}",
            "-p",
            f"127.0.0.1:{host_port}:5432",
            "postgres:15-alpine",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        detail = (run.stderr or run.stdout or "").strip()
        raise RuntimeError(f"docker run postgres failed: {detail}")

    url = f"postgresql+psycopg2://{user}:{password}@127.0.0.1:{host_port}/{db}"
    probe_code = (
        "import sqlalchemy as sa\n"
        f"e = sa.create_engine({url!r})\n"
        "with e.connect() as c:\n"
        "    c.exec_driver_sql('SELECT 1')\n"
    )
    deadline = time.time() + 90
    last_err = ""
    while time.time() < deadline:
        probe = subprocess.run(
            [PYTHON, "-c", probe_code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if probe.returncode == 0:
            return url, container
        last_err = (probe.stderr or probe.stdout or "").strip()
        time.sleep(1.0)

    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    raise RuntimeError(f"ephemeral postgres not ready: {last_err}")


@pytest.fixture(scope="module")
def postgres_url():
    env_url = _postgres_url_from_env()
    if env_url:
        yield env_url
        return

    if shutil.which("docker") is None:
        pytest.skip(
            "PostgreSQL integration unavailable: set SAS_TEST_DATABASE_URL "
            "or install Docker to start postgres:15-alpine"
        )

    try:
        url, container = _start_ephemeral_postgres()
    except RuntimeError as exc:
        # Docker is present; failing to start PG is a real infrastructure/product signal.
        pytest.fail(str(exc))

    try:
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def test_startup_postgres_integration(postgres_url: str):
    result = _run_startup(database_url=postgres_url, sas_profile="cbrs_winnforum", timeout=90)
    output = _combined_output(result)
    assert result.returncode == 0, output
    assert "Active spectrum profile: cbrs_winnforum" in result.stdout
