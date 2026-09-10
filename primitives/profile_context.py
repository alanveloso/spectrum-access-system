"""Immutable profile identity attached to every generic decision (D4)."""

from __future__ import annotations

from dataclasses import dataclass


def _require_token(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class ProfileContext:
    """Decision-time profile freeze. Not a process-wide singleton."""

    profile_id: str
    profile_version: str
    profile_hash: str
    dataset_versions: tuple[tuple[str, str], ...] = ()
    mechanism_versions: tuple[tuple[str, str], ...] = ()
    rf_provenance: str | None = None

    def __post_init__(self) -> None:
        _require_token("profile_id", self.profile_id)
        _require_token("profile_version", self.profile_version)
        _require_token("profile_hash", self.profile_hash)
