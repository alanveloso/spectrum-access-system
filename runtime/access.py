"""FastAPI accessors for the composed runtime (no rediscovery)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from adapters.protocol import ProtocolAdapter
from adapters.winnforum_rest import WINNFORUM_REST_PROTOCOL_ID
from providers.contract import DataProvider
from rf.port import RfPort
from runtime.composition import RuntimeComposition
from runtime.errors import RuntimeCompositionError


class RuntimeNotWiredError(RuntimeCompositionError):
    """Application started without attaching RuntimeComposition to app.state."""


def get_runtime_composition(request: Request) -> RuntimeComposition:
    composition = getattr(request.app.state, "runtime_composition", None)
    if not isinstance(composition, RuntimeComposition):
        raise RuntimeNotWiredError(
            "RuntimeComposition missing on app.state; startup must compose first"
        )
    return composition


def get_protocol_adapter(request: Request) -> ProtocolAdapter:
    adapter = get_runtime_composition(request).protocol_adapter
    if adapter is None:
        raise HTTPException(
            status_code=503,
            detail="runtime has no protocol adapter selected by deployment",
        )
    return adapter


def require_winnforum_rest_adapter(request: Request) -> ProtocolAdapter:
    """CBSD v1.2 routes require the composed WInnForum REST protocol adapter."""
    adapter = get_protocol_adapter(request)
    protocol_id = getattr(adapter, "protocol_id", None)
    if protocol_id != WINNFORUM_REST_PROTOCOL_ID:
        raise HTTPException(
            status_code=503,
            detail=(
                "CBSD-SAS v1.2 routes require protocol_id="
                f"{WINNFORUM_REST_PROTOCOL_ID!r}; composed runtime has "
                f"{protocol_id!r}"
            ),
        )
    if not hasattr(adapter, "request_key") or not hasattr(adapter, "response_key"):
        raise HTTPException(
            status_code=503,
            detail="composed protocol adapter lacks WInnForum procedure key API",
        )
    return adapter


def get_rf_port(request: Request) -> RfPort | None:
    return get_runtime_composition(request).rf


def require_rf_port(request: Request) -> RfPort:
    rf_port = get_rf_port(request)
    if rf_port is None:
        raise HTTPException(
            status_code=503,
            detail="runtime has no RF port; profile/deployment requires RF",
        )
    return rf_port


def get_provider_for(request: Request, capability: str) -> DataProvider:
    return get_runtime_composition(request).provider_for(capability)
