"""Map FastAPI/Pydantic validation failures to WINNF response codes 100–105.

Harness clients expect HTTP 200 with `{ "<method>Response": [ { "response": { "responseCode": N } } ] }`
rather than FastAPI's default HTTP 422 / 500 payloads.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import get_settings

# WINNF-TS-0016 / Response.schema.json
VERSION_UNSUPPORTED = 100
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_VALUE = 103
CERT_ERROR = 104
DEREGISTER = 105

MAXIMUM_BATCH_SIZE = get_settings().max_batch_size

_CBSD_RESPONSE_KEYS: dict[str, str] = {
    "registration": "registrationResponse",
    "spectrumInquiry": "spectrumInquiryResponse",
    "grant": "grantResponse",
    "heartbeat": "heartbeatResponse",
    "relinquishment": "relinquishmentResponse",
    "deregistration": "deregistrationResponse",
}

_MISSING_TYPES = frozenset(
    {
        "missing",
        "missing_argument",
        "missing_positional_only_argument",
        "missing_keyword_only_argument",
    }
)

_TOO_LONG_TYPES = frozenset({"too_long"})


def _cbsd_method_from_path(path: str) -> str | None:
 # /v1.2/registration or /vX.Y/heartbeat
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0].startswith("v") and parts[1] in _CBSD_RESPONSE_KEYS:
        return parts[1]
    return None


def _code_from_errors(errors: list[dict[str, Any]]) -> int:
    """Pick a single WINNF code representing the validation failures."""
    if not errors:
        return INVALID_VALUE
    for err in errors:
        etype = err.get("type") or ""
        if etype in _MISSING_TYPES:
            return MISSING_PARAM
    for err in errors:
        etype = err.get("type") or ""
        if etype in _TOO_LONG_TYPES:
            return INVALID_VALUE
        if "json" in etype:
            return INVALID_VALUE
    return INVALID_VALUE


def _is_batch_size_error(errors: list[dict[str, Any]]) -> bool:
    for err in errors:
        if (err.get("type") or "") in _TOO_LONG_TYPES:
            ctx = err.get("ctx") or {}
 # Pydantic v2 too_long on list with max_length
            if ctx.get("max_length") == MAXIMUM_BATCH_SIZE:
                return True
            loc = err.get("loc") or ()
            if any(
                isinstance(x, str) and x.endswith("Request")
                for x in loc
            ):
                return True
    return False


def _winnf_body(method: str | None, code: int) -> dict[str, Any]:
    key = _CBSD_RESPONSE_KEYS.get(method or "", "registrationResponse")
    return {key: [{"response": {"responseCode": code}}]}


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = list(exc.errors())
    method = _cbsd_method_from_path(request.url.path)

 # Oversized batch → HTTP 400 (spec allows HTTP error or protocol code).
    if _is_batch_size_error(errors):
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Batch exceeds MaximumBatchSize ({MAXIMUM_BATCH_SIZE})",
                "responseCode": INVALID_VALUE,
            },
        )

    code = _code_from_errors(errors)

 # CBSD endpoints: HTTP 200 with per-item envelope when the body lists items.
    if method is not None:
        body = exc.body
        request_key = f"{method}Request"
        raw_items = None
        if isinstance(body, dict):
            raw_items = body.get(request_key)
        if isinstance(raw_items, list) and raw_items:
            responses: list[dict[str, Any]] = []
 # Map errors to indices; default INVALID_VALUE for unmapped slots only
 # when the failure is at the batch envelope (not per-item).
            index_codes: dict[int, int] = {}
            for err in errors:
                loc = err.get("loc") or ()
                for i, part in enumerate(loc):
                    if part == request_key and i + 1 < len(loc) and isinstance(
                        loc[i + 1], int
                    ):
                        idx = int(loc[i + 1])
                        index_codes[idx] = _code_from_errors([err])
                        break
            for idx, raw in enumerate(raw_items):
                item_code = index_codes.get(idx, code if not index_codes else None)
                if item_code is None:
 # Sibling items without schema errors were not processed.
                    item_code = INVALID_VALUE
                entry: dict[str, Any] = {"response": {"responseCode": item_code}}
                if isinstance(raw, dict):
                    for echo in ("cbsdId", "grantId"):
                        if raw.get(echo) is not None:
                            entry[echo] = raw[echo]
                responses.append(entry)
            key = _CBSD_RESPONSE_KEYS[method]
            return JSONResponse(status_code=200, content={key: responses})
        return JSONResponse(status_code=200, content=_winnf_body(method, code))

 # Admin / other: keep a compact JSON error without FastAPI 422 noise.
    return JSONResponse(
        status_code=400,
        content={"response": {"responseCode": code}, "detail": errors},
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    method = _cbsd_method_from_path(request.url.path)
    if method is not None and exc.status_code == 400:
        return JSONResponse(
            status_code=200,
            content=_winnf_body(method, INVALID_VALUE),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )
