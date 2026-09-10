"""Active profile id selection (SAS_PROFILE / override). Format-agnostic."""

from __future__ import annotations

from config import get_settings

DEFAULT_PROFILE_ID = "cbrs_winnforum"

_override_id: str | None = None


def active_profile_id() -> str:
    if _override_id:
        return _override_id
    profile_id = (get_settings().sas_profile or DEFAULT_PROFILE_ID).strip()
    return profile_id or DEFAULT_PROFILE_ID


def set_profile_override(profile_id: str) -> None:
    """Force-select a profile id (tests / admin). Does not load YAML."""
    global _override_id
    _override_id = profile_id


def clear_profile_override() -> None:
    """Clear the shared profile-id override."""
    global _override_id
    _override_id = None
