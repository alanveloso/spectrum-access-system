"""Shared spectrum profile load/validation errors (format-agnostic)."""

from __future__ import annotations


class ProfileError(Exception):
    """Base error for spectrum profile loading failures."""


class ProfileNotFoundError(ProfileError):
    """Raised when no profile file exists for the requested id."""


class ProfileValidationError(ProfileError):
    """Raised when a profile file fails schema or structural validation."""


class ProfilePathError(ProfileError):
    """Raised when a resolved profile path escapes an allowlisted directory."""
