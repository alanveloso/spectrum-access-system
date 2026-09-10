"""Unit tests for DeploymentConfig loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime import (
    DEPLOYMENT_ENV,
    InvalidDeploymentConfigError,
    deployment_hash,
    load_deployment,
    parse_deployment_dict,
    resolve_deployment_path,
)
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH


def test_valid_minimal_deployment():
    dep = parse_deployment_dict(
        {
            "id": "lab",
            "version": "1.0.0",
            "device": {"plugin": "cbsd"},
        }
    )
    assert dep.id == "lab"
    assert dep.device is not None
    assert dep.device.plugin == "cbsd"
    assert dep.providers == ()


def test_provider_map_and_list_forms():
    as_list = parse_deployment_dict(
        {
            "id": "lab",
            "providers": [{"plugin": "bundle_boundaries"}],
        }
    )
    as_map = parse_deployment_dict(
        {
            "id": "lab",
            "providers": {"boundaries": {"plugin": "bundle_boundaries"}},
        }
    )
    assert len(as_list.providers) == 1
    assert as_list.providers[0].plugin == "bundle_boundaries"
    assert as_map.providers[0].plugin == "bundle_boundaries"


def test_unknown_fields_forbidden():
    with pytest.raises(InvalidDeploymentConfigError):
        parse_deployment_dict({"id": "lab", "rf_plugin": "free_space"})


def test_missing_id_rejected():
    with pytest.raises(InvalidDeploymentConfigError):
        parse_deployment_dict({"device": {"plugin": "cbsd"}})


def test_invalid_plugin_selection_syntax():
    with pytest.raises(InvalidDeploymentConfigError):
        parse_deployment_dict({"id": "lab", "rf": {"plugin": "mem-terrain"}})
    with pytest.raises(InvalidDeploymentConfigError):
        parse_deployment_dict({"id": "lab", "device": {"plugin": "Bad Name"}})


def test_deterministic_loading_and_hash(tmp_path: Path):
    path = tmp_path / "dep.yaml"
    path.write_text(
        "id: lab\nversion: '1.2.3'\ndevice:\n  plugin: cbsd\n",
        encoding="utf-8",
    )
    a = load_deployment(path)
    b = load_deployment(path)
    assert a == b
    assert deployment_hash(a) == deployment_hash(b)
    assert len(deployment_hash(a)) == 64


def test_resolve_deployment_path_env_and_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("id: explicit\n", encoding="utf-8")
    assert resolve_deployment_path(explicit=explicit) == explicit

    env_path = tmp_path / "from_env.yaml"
    env_path.write_text("id: envdep\n", encoding="utf-8")
    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    assert resolve_deployment_path(env={}) == DEFAULT_DEPLOYMENT_PATH
    assert resolve_deployment_path(env={DEPLOYMENT_ENV: str(env_path)}) == env_path


def test_reference_deployment_loads():
    dep = load_deployment(DEFAULT_DEPLOYMENT_PATH)
    assert dep.id == "reference"
    assert dep.protocol is not None
    assert dep.protocol.plugin == "winnforum_rest"
    assert dep.device is not None
    assert dep.device.plugin == "cbsd"
    assert dep.rf is not None
    assert dep.rf.plugin == "free_space"
