"""Scoped lookup of an already-resolved RuntimeComposition.

Lookup never discovers plugins or calls ``compose_runtime``. Authority order:

1. Explicit ``composition`` argument (tests / caller injection)
2. ContextVar binding (request/test scope)
3. Process-local holder (API or Celery worker startup — resolve once)
4. FastAPI ``app.state.runtime_composition`` (same instance as process-local on API)

Worker threads do not inherit ContextVar; they consume process/app authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from runtime.composition import RuntimeComposition

_bound_composition: ContextVar[RuntimeComposition | None] = ContextVar(
    "runtime_composition_bound", default=None
)


def get_bound_runtime_composition() -> RuntimeComposition | None:
    """Return the ContextVar-bound composition for this execution context only."""
    return _bound_composition.get()


def lookup_runtime_composition(
    composition: RuntimeComposition | None = None,
) -> RuntimeComposition | None:
    """Resolve the already-composed RuntimeComposition for consumers.

    Never rediscovers plugins or recomposes. Returns ``None`` when no authority
    is available (callers fail closed).
    """
    if composition is not None:
        return composition
    active = get_bound_runtime_composition()
    if active is not None:
        return active
    from runtime.bootstrap import get_process_runtime_composition

    process_local = get_process_runtime_composition()
    if process_local is not None:
        return process_local
    # API process also mirrors process authority on app.state (thread-safe fallback).
    try:
        import main

        return getattr(main.app.state, "runtime_composition", None)
    except Exception:  # noqa: BLE001 — app may be unavailable in some contexts
        return None


def bind_runtime_composition(composition: RuntimeComposition) -> Token:
    return _bound_composition.set(composition)


def reset_runtime_composition(token: Token) -> None:
    _bound_composition.reset(token)


@contextmanager
def runtime_composition_scope(
    composition: RuntimeComposition,
) -> Iterator[RuntimeComposition]:
    token = bind_runtime_composition(composition)
    try:
        yield composition
    finally:
        reset_runtime_composition(token)
