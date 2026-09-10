"""Spectrum Profile load trust: paths, id validation, provenance.

Plugins remain operator-installed trusted code. This module
hardens *how* profiles are resolved and records immutable load provenance for
decisions/doctor — it does not execute YAML or introduce a regulatory DSL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from profiles.errors import (
    ProfileNotFoundError,
    ProfilePathError,
    ProfileValidationError,
)
from profiles.context import profile_hash
from profiles.schema import ProfileDocument

# Closed id shape used by reference + custom profiles (same schema).
_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_BUILTIN_PROFILES_DIR = Path(__file__).resolve().parent / "definitions"


class ProfileTrustTier(str, Enum):
    """Where a Spectrum Profile document was resolved from."""

    BUILTIN = "builtin"
    OPERATOR_EXPLICIT = "operator_explicit"


@dataclass(frozen=True, slots=True)
class ProfileLoadProvenance:
    """Immutable record of a successful Spectrum Profile load (paths + hash)."""

    trust_tier: ProfileTrustTier
    source_path: str
    profile_id: str
    profile_version: str
    profile_hash: str
    metadata_status: str
    based_on: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "trust_tier": self.trust_tier.value,
            "source_path": self.source_path,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "metadata_status": self.metadata_status,
            "based_on": self.based_on,
        }


def builtin_profiles_dir() -> Path:
    return _BUILTIN_PROFILES_DIR.resolve()


def validate_profile_id(profile_id: str) -> str:
    """Fail closed on path-like or non-token profile ids."""
    if not isinstance(profile_id, str) or not profile_id:
        raise ProfilePathError("invalid profile id")
    if "\x00" in profile_id:
        raise ProfilePathError("invalid profile id")
    if (
        "/" in profile_id
        or "\\" in profile_id
        or ".." in profile_id
        or profile_id.startswith(".")
        or profile_id.endswith(".")
    ):
        raise ProfilePathError(f"invalid profile id '{profile_id}'")
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        raise ProfilePathError(f"invalid profile id '{profile_id}'")
    return profile_id


def assert_path_within(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ProfilePathError(
            f"profile path '{resolved}' is outside allowlisted directory "
            f"'{root_resolved}'"
        ) from exc
    return resolved


def assert_yaml_profile_file(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() not in {".yaml", ".yml"}:
        raise ProfileValidationError(
            f"unsupported profile format '{resolved.suffix}' for '{resolved}'"
        )
    if not resolved.is_file():
        raise ProfileNotFoundError(f"spectrum profile file not found: {resolved}")
    return resolved


def provenance_for(
    parsed: ProfileDocument,
    *,
    source_path: Path | str,
    trust_tier: ProfileTrustTier,
) -> ProfileLoadProvenance:
    return ProfileLoadProvenance(
        trust_tier=trust_tier,
        source_path=str(Path(source_path).expanduser().resolve()),
        profile_id=parsed.metadata.id,
        profile_version=parsed.metadata.version,
        profile_hash=profile_hash(parsed),
        metadata_status=parsed.metadata.status,
        based_on=parsed.metadata.based_on,
    )


def assert_metadata_id_matches(parsed: ProfileDocument, profile_id: str) -> None:
    if parsed.metadata.id != profile_id:
        raise ProfileValidationError(
            f"profile id mismatch: requested '{profile_id}', "
            f"document declares '{parsed.metadata.id}'"
        )
