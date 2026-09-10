"""Catch-all CBSD-SAS routes for unsupported protocol versions → responseCode 100."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cbsd_version import (
    PROCEDURE_SPECS,
    UnsupportedVersionBatchError,
    build_unsupported_version_body,
    is_supported_cbsd_sas_version,
    malformed_body_response,
)
from services.error_handlers import INVALID_VALUE

router = APIRouter(tags=["cbsd-sas-version"])


def _register_procedure(procedure: str) -> None:
    if procedure not in PROCEDURE_SPECS:
        raise KeyError(procedure)

    async def _unsupported(version: str, request: Request) -> JSONResponse:
        if is_supported_cbsd_sas_version(version):
 # Concrete /v1.2/{procedure} should have matched; do not invent protocol success.
            return JSONResponse(
                status_code=404,
                content={"detail": f"supported version must use /v1.2/{procedure}"},
            )
        try:
            body: Any = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=200,
                content=malformed_body_response(procedure, code=INVALID_VALUE),
            )
        try:
            payload = build_unsupported_version_body(procedure, body)
        except UnsupportedVersionBatchError as exc:
            if "MaximumBatchSize" in str(exc):
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": str(exc),
                        "responseCode": INVALID_VALUE,
                    },
                )
            return JSONResponse(
                status_code=200,
                content=malformed_body_response(procedure, code=exc.response_code),
            )
        return JSONResponse(status_code=200, content=payload)

    _unsupported.__name__ = f"{procedure}_unsupported_version"
    _unsupported.__doc__ = (
        f"Unsupported CBSD-SAS version for {procedure} → responseCode 100."
    )
    router.add_api_route(
        f"/{{version}}/{procedure}",
        _unsupported,
        methods=["POST"],
        name=f"{procedure}_unsupported_version",
    )


for _procedure in PROCEDURE_SPECS:
    _register_procedure(_procedure)
