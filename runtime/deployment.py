"""Deployment configuration: HOW an installation satisfies profile requirements.

Plugin implementation names belong here — never inside a Spectrum Profile.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapters.plugin_names import validate_plugin_name
from runtime.errors import InvalidDeploymentConfigError

DEPLOYMENT_ENV = "SAS_DEPLOYMENT_CONFIG"
_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DEPLOYMENT_PATH = _PACKAGE_DIR / "deployments" / "reference.yaml"


class PluginSelection(BaseModel):
    """Select one discovered plugin by stable name."""

    model_config = ConfigDict(extra="forbid")

    plugin: str

    @field_validator("plugin")
    @classmethod
    def _valid_plugin_name(cls, value: str) -> str:
        try:
            return validate_plugin_name(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class DeploymentConfig(BaseModel):
    """Implementation selections for one installation.

    Independent from Spectrum Profile documents. Profile id may be recorded
    for operator clarity but never implies plugin choices.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    description: str | None = None
    # Optional operator hint — not used for plugin dispatch by profile id.
    profile_hint: str | None = None
    protocol: PluginSelection | None = None
    device: PluginSelection | None = None
    network: PluginSelection | None = None
    rf: PluginSelection | None = None
    providers: tuple[PluginSelection, ...] = ()

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        try:
            return validate_plugin_name(value)
        except ValueError as exc:
            raise ValueError(f"invalid deployment id: {exc}") from exc

    @field_validator("providers", mode="before")
    @classmethod
    def _coerce_providers(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, dict):
            # Allow capability-keyed map: {terrain: {plugin: ...}, ...}
            selections: list[Any] = []
            for item in value.values():
                selections.append(item)
            return selections
        return value


def deployment_hash(deployment: DeploymentConfig) -> str:
    """Deterministic hash of deployment selections (not a profile_hash)."""
    payload = deployment.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_deployment_dict(raw: dict[str, Any]) -> DeploymentConfig:
    try:
        return DeploymentConfig.model_validate(raw)
    except Exception as exc:
        raise InvalidDeploymentConfigError(
            f"invalid deployment configuration: {exc}",
            reason=str(exc),
        ) from exc


def load_deployment(path: Path | str) -> DeploymentConfig:
    target = Path(path)
    if not target.is_file():
        raise InvalidDeploymentConfigError(
            f"deployment config not found: {target}",
            reason="missing_file",
        )
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InvalidDeploymentConfigError(
            f"failed to read deployment config: {target}",
            reason=str(exc),
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidDeploymentConfigError(
            "deployment config root must be a mapping",
            reason="invalid_root",
        )
    return parse_deployment_dict(raw)


def resolve_deployment_path(
    *,
    explicit: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve deployment YAML path: explicit → env → packaged reference."""
    if explicit is not None:
        return Path(explicit)
    environ = env if env is not None else os.environ
    from_env = environ.get(DEPLOYMENT_ENV, "").strip()
    if from_env:
        return Path(from_env)
    return DEFAULT_DEPLOYMENT_PATH


def load_deployment_for_startup(
    *,
    explicit: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> DeploymentConfig:
    return load_deployment(resolve_deployment_path(explicit=explicit, env=env))
