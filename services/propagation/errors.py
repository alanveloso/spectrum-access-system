"""Errors for the PAT propagation Admin query."""

from __future__ import annotations


class PropagationError(Exception):
    """Base propagation service error."""


class PropagationRequestError(PropagationError):
    """Invalid Admin request (maps to HTTP 400)."""


class PropagationUnavailableError(PropagationError):
    """Engines / datasets unavailable (maps to HTTP 503)."""
