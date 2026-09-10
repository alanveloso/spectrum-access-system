"""Access composed data providers without rediscovery.

Regulatory consumers resolve capabilities through the active RuntimeComposition
bound for the current scope (request, CPAS pipeline, or process startup).
Worker threads that lack ContextVar inheritance still consume the SAME
startup-resolved instance via application state (see ``lookup_runtime_composition``).
"""

from __future__ import annotations

from providers.contract import DataProvider, ProviderRecord
from runtime.composition import RuntimeComposition
from runtime.context import lookup_runtime_composition
from runtime.errors import MissingCapabilityError, RuntimeCompositionError


class ProviderNotBoundError(RuntimeCompositionError):
    """No RuntimeComposition is bound for the current execution scope."""


def active_runtime_composition() -> RuntimeComposition:
    composition = lookup_runtime_composition()
    if composition is None:
        raise ProviderNotBoundError(
            "RuntimeComposition is not bound; startup or request scope must bind first"
        )
    return composition


def require_composed_provider(capability: str) -> DataProvider:
    """Return the provider for ``capability`` from the bound composition."""
    return active_runtime_composition().provider_for(capability)


def fetch_composed_resource(capability: str, token: str) -> ProviderRecord:
    """Fetch a token-keyed resource from the composed provider for ``capability``."""
    return require_composed_provider(capability).fetch(token=token)


def try_composed_provider(capability: str) -> DataProvider | None:
    """Return the composed provider when bound, else None (tests / optional paths)."""
    composition = lookup_runtime_composition()
    if composition is None:
        return None
    try:
        return composition.provider_for(capability)
    except MissingCapabilityError:
        return None
