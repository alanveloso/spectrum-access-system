"""Loaders for the test-only synthetic Spectrum Profile / DeploymentConfig.

The synthetic profile lives under ``tests/fixtures`` on purpose: proving that a
novel profile composes must not require adding it to the production
``profiles/definitions`` tree.
"""

from __future__ import annotations

from pathlib import Path

from profiles.parse import load_profile_document
from profiles.schema import ProfileDocument
from runtime.deployment import DeploymentConfig, load_deployment

SYNTHETIC_PROFILE_ID = "synthetic_minimal"
_FIXTURES = Path(__file__).resolve().parent
SYNTHETIC_PROFILE_PATH = _FIXTURES / "profiles" / f"{SYNTHETIC_PROFILE_ID}.yaml"
SYNTHETIC_DEPLOYMENT_PATH = _FIXTURES / "deployments" / f"{SYNTHETIC_PROFILE_ID}.yaml"


def synthetic_profile() -> ProfileDocument:
    return load_profile_document(SYNTHETIC_PROFILE_PATH)


def synthetic_deployment() -> DeploymentConfig:
    return load_deployment(SYNTHETIC_DEPLOYMENT_PATH)
