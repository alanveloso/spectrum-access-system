"""ASGI middleware: correlation ID, access log, latency metrics."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from services.metrics import get_metrics
from services.request_context import (
    RequestContext,
    new_request_id,
    request_id_header_name,
    reset_request_context,
    response_header_name,
    set_request_context,
)

logger = logging.getLogger(__name__)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get(request_id_header_name())
        request_id = (incoming or "").strip() or new_request_id()
        token = set_request_context(RequestContext(request_id=request_id))
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = int(response.status_code)
            response.headers[response_header_name()] = request_id
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                get_metrics().record_http(
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    duration_ms=duration_ms,
                )
                logger.info(
                    "http_request method=%s path=%s status=%s duration_ms=%.2f request_id=%s",
                    request.method,
                    request.url.path,
                    status,
                    duration_ms,
                    request_id,
                )
            except Exception:  # noqa: BLE001 — observability must not break requests
                logger.exception("observability_middleware_finalize_failed")
            reset_request_context(token)
