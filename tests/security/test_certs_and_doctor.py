"""Tests for canonical CERTS_DIR validation and tools.doctor ()."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import clear_settings_cache
from services.cert_layout import (
    REQUIRED_FILES,
    format_certificate_error,
    validate_certificate_layout,
)
from tools.doctor import main as doctor_main
from tools.doctor import run_doctor

from tests.support.repo import REPO_ROOT as ROOT
PYTHON = sys.executable

_PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
_KEY = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _clear_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _write_dummy_certs(certs_dir: Path, *, with_crl_pem: bool = True) -> None:
    certs_dir.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        body = _KEY if name.endswith(".key") else _PEM
        (certs_dir / name).write_text(body, encoding="utf-8")
    crl_dir = certs_dir / "crl"
    crl_dir.mkdir(parents=True, exist_ok=True)
    if with_crl_pem:
        (crl_dir / "example.crl.pem").write_text(
            "-----BEGIN X509 CRL-----\nMIIB\n-----END X509 CRL-----\n",
            encoding="utf-8",
        )


def test_validate_certificate_layout_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    certs = tmp_path / "certs"
    _write_dummy_certs(certs)
    monkeypatch.setenv("CERTS_DIR", str(certs))
    clear_settings_cache()
    from config import get_settings

    result = validate_certificate_layout(get_settings())
    assert result.ok
    assert result.missing == []
    assert result.invalid_files == []


def test_validate_certificate_layout_requires_ecc_crl_and_pem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    certs = tmp_path / "certs"
    certs.mkdir()
    for name in ("server.cert", "server.key", "ca.cert"):
        (certs / name).write_text(_PEM if name.endswith(".cert") else _KEY, encoding="utf-8")
    monkeypatch.setenv("CERTS_DIR", str(certs))
    clear_settings_cache()
    from config import get_settings

    result = validate_certificate_layout(get_settings())
    assert not result.ok
    assert "server-ecc.cert" in result.missing_files
    assert "server-ecc.key" in result.missing_files
    assert "crl" in result.missing_dirs
    message = format_certificate_error(result)
    assert "CERTS_DIR=" in message
    assert "server-ecc.cert" in message


def test_validate_rejects_non_pem_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    certs = tmp_path / "certs"
    _write_dummy_certs(certs)
    (certs / "server.cert").write_text("not-a-pem\n", encoding="utf-8")
    monkeypatch.setenv("CERTS_DIR", str(certs))
    clear_settings_cache()
    from config import get_settings

    result = validate_certificate_layout(get_settings())
    assert not result.ok
    assert "server.cert" in result.invalid_files


def test_validate_requires_crl_pem_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    certs = tmp_path / "certs"
    _write_dummy_certs(certs, with_crl_pem=False)
    monkeypatch.setenv("CERTS_DIR", str(certs))
    clear_settings_cache()
    from config import get_settings

    result = validate_certificate_layout(get_settings())
    assert not result.ok
    assert "crl/*.crl.pem" in result.missing_files


def test_empty_certs_dir_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CERTS_DIR", "")
    clear_settings_cache()
    from config import get_settings

    settings = get_settings()
    assert settings.certs_dir.name == "certs"


def test_readme_and_config_agree_on_canonical_certs_dir():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`./certs`" in readme or "**`./certs`**" in readme
    assert "../src/harness/certs/" not in readme
    assert "CERTS_DIR" in readme
    assert "python -m tools.doctor" in readme
    from config import Settings, _DEFAULT_CERTS

    assert Settings.model_fields["certs_dir"].default == _DEFAULT_CERTS
    assert _DEFAULT_CERTS.name == "certs"


def test_doctor_fails_without_certificates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CERTS_DIR", str(tmp_path / "missing-certs"))
    clear_settings_cache()
    report = run_doctor()
    assert not report.ok
    cert_finding = next(item for item in report.findings if item.name == "certificates")
    assert not cert_finding.ok
    assert doctor_main([]) == 1


def test_doctor_passes_with_complete_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    certs = tmp_path / "certs"
    _write_dummy_certs(certs)
    monkeypatch.setenv("CERTS_DIR", str(certs))
    clear_settings_cache()
    report = run_doctor()
    assert report.ok
    assert doctor_main([]) == 0


def test_doctor_module_entrypoint(tmp_path: Path):
    env = os.environ.copy()
    env["CERTS_DIR"] = str(tmp_path / "empty")
    result = subprocess.run(
        [PYTHON, "-m", "tools.doctor"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert "certificates" in result.stdout
