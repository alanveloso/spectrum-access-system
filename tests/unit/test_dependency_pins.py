"""Dependency pinning and Uvicorn ssl_context_factory compatibility ()."""

from __future__ import annotations

import inspect
import re
import sys
import tomllib

import uvicorn
from uvicorn.config import Config

from tests.support.repo import REPO_ROOT as ROOT


def test_python_meets_declared_minimum():
    assert sys.version_info >= (3, 11), (
        f"requires-python is >=3.11 but interpreter is {sys.version}"
    )


def test_pyproject_declares_python_311_and_pinned_deps():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = data["project"]["requires-python"]
    assert requires.startswith(">="), requires
    assert "3.11" in requires
    deps = data["project"]["dependencies"]
    assert deps, "project.dependencies must not be empty"
    for dep in deps:
        assert "==" in dep, f"dependency must be pinned with == : {dep}"
        assert ">=" not in dep.split(";")[0], f"floating lower-bound left in {dep}"


def test_requirements_txt_has_no_floating_lower_bounds():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert ">=" not in line, f"floating requirement: {line}"
        assert "==" in line, f"unpinned requirement: {line}"


def test_requirements_dev_pins_match_pyproject_optional_dev():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional = data["project"]["optional-dependencies"]["dev"]
    for dep in optional:
        assert "==" in dep, dep
    dev_txt = {
        line.strip()
        for line in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for dep in optional:
        assert dep in dev_txt, f"missing from requirements-dev.txt: {dep}"


def test_requirements_lock_pins_direct_dependencies():
    lock_lines = {
        line.strip()
        for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    # Case-insensitive: pip freeze may emit Shapely vs shapely.
    lock_lower = {line.lower() for line in lock_lines}
    required_names = {
        "fastapi": "0.141.1",
        "uvicorn": "0.52.1",
        "sqlalchemy": "2.0.51",
        "alembic": "1.16.4",
        "pydantic": "2.13.4",
        "pydantic-settings": "2.14.2",
        "psycopg2-binary": "2.9.12",
        "celery": "5.6.3",
        "cryptography": "50.0.0",
        "httpx": "0.28.1",
        "pyyaml": "6.0.3",
        # Direct runtime imports (PPA/IAP/PAT paths); must not rely on transitive luck.
        "numpy": "1.26.4",
        "pykml": "0.2.0",
        "shapely": "1.7.1",
        "lxml": "6.1.2",  # transitive of pykml; pin for Docker reproducibility
    }
    for name, version in required_names.items():
        assert f"{name}=={version}" in lock_lower, f"lock missing {name}=={version}"
    for line in lock_lines:
        assert "==" in line and ">=" not in line, f"lock entry not exact: {line}"


def test_dockerfile_pins_base_image_by_digest():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r"FROM\s+python:3\.11-slim-bookworm@sha256:[0-9a-f]{64}",
        text,
    ), "Dockerfile must pin python:3.11-slim-bookworm by sha256 digest"
    assert "requirements.lock.txt" in text
    assert "pip==" in text, "Dockerfile must pin pip version used for lock installs"


def test_requirements_txt_matches_pyproject_dependencies():
    """Direct runtime pins must stay aligned across install entrypoints."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_deps = {
        _normalize_req(dep) for dep in data["project"]["dependencies"]
    }
    req_deps = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        req_deps.add(_normalize_req(line))
    assert pyproject_deps == req_deps, (
        f"pyproject/requirements drift: only_in_pyproject={pyproject_deps - req_deps} "
        f"only_in_requirements={req_deps - pyproject_deps}"
    )


def _normalize_req(dep: str) -> str:
    """Normalize extras and package name case for set comparison."""
    dep = dep.strip()
    name, _, version = dep.partition("==")
    # Drop extras: uvicorn[standard] -> uvicorn
    name = name.split("[", 1)[0].strip().lower().replace("_", "-")
    return f"{name}=={version.strip()}"


def test_uvicorn_run_accepts_ssl_context_factory():
    """mTLS entrypoint passes ssl_context_factory into uvicorn.run (main.py)."""
    assert uvicorn.__version__ == "0.52.1", (
        f"installed uvicorn {uvicorn.__version__} != locked 0.52.1"
    )
    params = inspect.signature(uvicorn.run).parameters
    assert "ssl_context_factory" in params
    config_params = inspect.signature(Config.__init__).parameters
    assert "ssl_context_factory" in config_params


def test_uvicorn_h11_request_response_cycle_still_patchable():
    """mtls_auth.patch_uvicorn_for_client_cert depends on this private API surface."""
    import inspect as _inspect

    from uvicorn.protocols.http import h11_impl

    src = _inspect.getsource(h11_impl.RequestResponseCycle)
    assert "transport" in src
    from services.mtls_auth import patch_uvicorn_for_client_cert

    patch_uvicorn_for_client_cert()
    assert getattr(h11_impl.RequestResponseCycle.__init__, "_sas_mtls_patched", False)


def test_uvicorn_httptools_request_response_cycle_still_patchable():
    """Default uvicorn[standard] HTTP stack must expose transport for mTLS binding."""
    import inspect as _inspect

    from uvicorn.protocols.http import httptools_impl

    # Source must keep a named ``transport`` arg even if runtime __init__ is patched.
    src = _inspect.getsource(httptools_impl.RequestResponseCycle)
    assert "transport" in src
    from services.mtls_auth import patch_uvicorn_for_client_cert

    patch_uvicorn_for_client_cert()
    assert getattr(
        httptools_impl.RequestResponseCycle.__init__, "_sas_mtls_patched", False
    )
