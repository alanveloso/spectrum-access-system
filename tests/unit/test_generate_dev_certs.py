"""Tests for ephemeral CERTS_DIR generation (CI / local smoke)."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import clear_settings_cache, get_settings
from services.cert_layout import validate_certificate_layout
from tools.generate_dev_certs import generate_dev_certs
from tools.smoke_mtls import probe_rejects_missing_client_cert


def test_generate_dev_certs_satisfies_layout(tmp_path: Path, monkeypatch):
    out = tmp_path / "certs"
    paths = generate_dev_certs(out)
    assert paths["client.cert"].is_file()
    assert paths["client.key"].is_file()
    monkeypatch.setenv("CERTS_DIR", str(out))
    clear_settings_cache()
    result = validate_certificate_layout(get_settings())
    assert result.ok, result.missing
    clear_settings_cache()


def test_generate_dev_certs_refuses_nonempty_without_force(tmp_path: Path):
    out = tmp_path / "certs"
    generate_dev_certs(out)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        generate_dev_certs(out)
    generate_dev_certs(out, force=True)


def test_probe_does_not_treat_connection_refused_as_mtls_rejection(tmp_path: Path):
    certs = generate_dev_certs(tmp_path / "certs")
    # Closed port → must not report success as "rejected missing client".
    ok = probe_rejects_missing_client_cert(
        base_url="https://127.0.0.1:1", ca_certs=certs["ca.cert"]
    )
    assert ok is False
