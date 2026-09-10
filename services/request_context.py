"""Request/batch/item correlation context."""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

_HEADER = "x-request-id"
_RESPONSE_HEADER = "X-Request-ID"


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    batch_id: str | None = None
    item_index: int | None = None


_ctx: ContextVar[RequestContext | None] = ContextVar("sas_request_context", default=None)


def new_request_id() -> str:
    return str(uuid.uuid4())


def get_request_context() -> RequestContext | None:
    return _ctx.get()


def get_request_id() -> str | None:
    ctx = _ctx.get()
    return ctx.request_id if ctx else None


def set_request_context(ctx: RequestContext) -> Token:
    return _ctx.set(ctx)


def reset_request_context(token: Token) -> None:
    _ctx.reset(token)


def bind_batch(*, batch_id: str | None = None) -> RequestContext | None:
    """Attach batch_id to the current request context (CBSD multi-item)."""
    current = _ctx.get()
    if current is None:
        return None
    updated = RequestContext(
        request_id=current.request_id,
        batch_id=batch_id or current.request_id,
        item_index=current.item_index,
    )
    _ctx.set(updated)
    return updated


def bind_item_index(item_index: int) -> RequestContext | None:
    current = _ctx.get()
    if current is None:
        return None
    updated = RequestContext(
        request_id=current.request_id,
        batch_id=current.batch_id or current.request_id,
        item_index=item_index,
    )
    _ctx.set(updated)
    return updated


def context_as_dict() -> dict[str, Any]:
    ctx = _ctx.get()
    if ctx is None:
        return {}
    out: dict[str, Any] = {"requestId": ctx.request_id}
    if ctx.batch_id is not None:
        out["batchId"] = ctx.batch_id
    if ctx.item_index is not None:
        out["itemIndex"] = ctx.item_index
    return out


def request_id_header_name() -> str:
    return _HEADER


def response_header_name() -> str:
    return _RESPONSE_HEADER
