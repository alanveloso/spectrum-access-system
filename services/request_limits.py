"""Operational request size limits.

Reject oversized bodies before route handlers run:

- ``Content-Length`` above the limit → early 413 (body drained, not buffered);
- missing / understated ``Content-Length`` → count ASGI ``http.request``
 bytes incrementally and 413 once the next byte would exceed ``limit``.

At most ``limit`` bytes are retained to replay a valid body to the app;
oversized streams are drained and never forwarded.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config import get_settings


def _too_large_response() -> JSONResponse:
    return JSONResponse({"detail": "request body too large"}, status_code=413)


def _bad_length_response() -> JSONResponse:
    return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)


async def _drain_body(receive: Receive) -> None:
    """Consume remaining request body messages without retaining them."""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return
        if not message.get("more_body", False):
            return


def _replay_receive(body: bytes, receive: Receive) -> Receive:
    """Return an ASGI receive that yields ``body`` once, then proxies ``receive``."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class RequestSizeLimitMiddleware:
    """Pure ASGI middleware: Content-Length check + incremental byte counting."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        limit = int(settings.sas_max_request_body_bytes)
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                announced = int(content_length)
            except ValueError:
                await _bad_length_response()(scope, receive, send)
                return
            if announced < 0:
                await _bad_length_response()(scope, receive, send)
                return
            if announced > limit:
                await _drain_body(receive)
                await _too_large_response()(scope, receive, send)
                return

 # Read/count body in chunks; retain at most ``limit`` bytes for replay.
        buf = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
 # Disconnect / odd message before a body: forward as-is.
                held = message

                async def passthrough() -> Message:
                    return held

                await self.app(scope, passthrough, send)
                return

            chunk = message.get("body", b"") or b""
            if len(buf) + len(chunk) > limit:
                if message.get("more_body", False):
                    await _drain_body(receive)
                await _too_large_response()(scope, receive, send)
                return
            if chunk:
                buf.extend(chunk)

            if not message.get("more_body", False):
                break

        await self.app(scope, _replay_receive(bytes(buf), receive), send)
