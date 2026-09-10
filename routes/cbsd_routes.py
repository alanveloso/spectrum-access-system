"""CBSD-to-SAS v1.2 routes with per-item schema validation."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database import get_db
from runtime.access import require_winnforum_rest_adapter
from runtime.context import runtime_composition_scope
from schemas.deregistration import DeregistrationRequestItem
from schemas.grant import GrantRequestItem
from schemas.heartbeat import HeartbeatRequestItem
from schemas.registration import RegistrationRequestItem
from schemas.relinquishment import RelinquishmentRequestItem
from schemas.spectrum_inquiry import SpectrumInquiryRequestItem
from services.cbsd_auth import authorize_cbsd_operation
from services.cbsd_batch import merge_schema_and_service_responses, parse_item_batch
from services.deregistration_service import process_deregistration
from services.error_handlers import (
    CERT_ERROR,
    INVALID_VALUE,
    MAXIMUM_BATCH_SIZE,
    MISSING_PARAM,
)
from services.grant_service import process_grant
from services.heartbeat_service import process_heartbeat
from services.metrics import get_metrics
from services.registration_service import process_registration
from services.relinquishment_service import process_relinquishment
from services.request_context import bind_batch, bind_item_index, get_request_id
from services.spectrum_inquiry_service import process_spectrum_inquiry

router = APIRouter(prefix="/v1.2", tags=["cbsd-sas"])

ServiceRunner = Callable[[list[dict[str, Any]], str | None], list[dict[str, Any]]]


def _single_code_response(protocol: Any, procedure: str, code: int) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={protocol.response_key(procedure): [{"response": {"responseCode": code}}]},
    )


def _run_batch(
    *,
    procedure: str,
    response_key: str,
    body: dict[str, Any],
    item_model: type,
    run_service: ServiceRunner,
    request: Request,
    protocol: Any,
) -> JSONResponse:
    auth = authorize_cbsd_operation(request)
    if not auth.allowed:
        return _single_code_response(protocol, procedure, auth.denial_code or CERT_ERROR)

    request_key = protocol.request_key(procedure)
    raw_items = body.get(request_key)
    if raw_items is None:
        # Protocol envelope (HTTP 200), not FastAPI 400 → WINNF remap ambiguity.
        return _single_code_response(protocol, procedure, MISSING_PARAM)
    if not isinstance(raw_items, list):
        return _single_code_response(protocol, procedure, INVALID_VALUE)
    try:
        parsed = parse_item_batch(
            raw_items, item_model=item_model, max_batch_size=MAXIMUM_BATCH_SIZE
        )
    except ValueError as exc:
        message = str(exc)
        if "MaximumBatchSize" in message:
            # Spec allows HTTP error for oversized batches (do not remap to HTTP 200).
            return JSONResponse(
                status_code=400,
                content={
                    "detail": message,
                    "responseCode": INVALID_VALUE,
                },
            )
        return _single_code_response(protocol, procedure, INVALID_VALUE)

    bind_batch(batch_id=get_request_id())
    service_responses = (
        run_service(parsed.items_for_service, auth.certificate_hash)
        if parsed.items_for_service
        else []
    )
    # SIQ: echo cbsdId only when syntactically ``fccId/serial`` (not free-form /
    # unknown harness placeholders). Other procedures keep always-echo.
    cbsd_id_echo = "syntactic" if procedure == "spectrumInquiry" else "always"
    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=service_responses,
        echo_from_raw=raw_items,
        echo_fields=("cbsdId", "grantId"),
        cbsd_id_echo=cbsd_id_echo,
    )
    metrics = get_metrics()
    for index, item in enumerate(merged):
        # itemIndex is bound for post-merge metrics only; domain services still
        # process the full batch in one call (no per-item service re-entry).
        bind_item_index(index)
        if not isinstance(item, dict):
            continue
        resp = item.get("response") if isinstance(item.get("response"), dict) else item
        code = resp.get("responseCode") if isinstance(resp, dict) else None
        if isinstance(code, int):
            metrics.record_protocol(procedure=procedure, response_code=code)
    return JSONResponse({response_key: merged})


def _protocol_and_scope(request: Request):
    from runtime.access import get_runtime_composition

    composition = get_runtime_composition(request)
    protocol = require_winnforum_rest_adapter(request)
    return composition, protocol


@router.post("/registration")
def registration(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    composition, protocol = _protocol_and_scope(request)
    with runtime_composition_scope(composition):
        return _run_batch(
            procedure="registration",
            response_key=protocol.response_key("registration"),
            body=body,
            item_model=RegistrationRequestItem,
            request=request,
            protocol=protocol,
            run_service=lambda items, cert_hash: process_registration(
                db,
                items,
                certificate_hash=cert_hash,
                device_adapter=composition.device_adapter,
            ),
        )


@router.post("/grant")
def grant(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    composition, protocol = _protocol_and_scope(request)
    with runtime_composition_scope(composition):
        return _run_batch(
            procedure="grant",
            response_key=protocol.response_key("grant"),
            body=body,
            item_model=GrantRequestItem,
            request=request,
            protocol=protocol,
            run_service=lambda items, cert_hash: process_grant(
                db,
                items,
                certificate_hash=cert_hash,
                rf_port=composition.rf,
                device_adapter=composition.device_adapter,
            ),
        )


@router.post("/heartbeat")
def heartbeat(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    composition, protocol = _protocol_and_scope(request)
    with runtime_composition_scope(composition):
        return _run_batch(
            procedure="heartbeat",
            response_key=protocol.response_key("heartbeat"),
            body=body,
            item_model=HeartbeatRequestItem,
            request=request,
            protocol=protocol,
            run_service=lambda items, cert_hash: process_heartbeat(
                db, items, certificate_hash=cert_hash
            ),
        )


@router.post("/spectrumInquiry")
def spectrum_inquiry(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    composition, protocol = _protocol_and_scope(request)
    with runtime_composition_scope(composition):
        return _run_batch(
            procedure="spectrumInquiry",
            response_key=protocol.response_key("spectrumInquiry"),
            body=body,
            item_model=SpectrumInquiryRequestItem,
            request=request,
            protocol=protocol,
            run_service=lambda items, cert_hash: process_spectrum_inquiry(
                db, items, certificate_hash=cert_hash,
                device_adapter=composition.device_adapter,
            ),
        )


@router.post("/relinquishment")
def relinquishment(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    composition, protocol = _protocol_and_scope(request)
    with runtime_composition_scope(composition):
        return _run_batch(
            procedure="relinquishment",
            response_key=protocol.response_key("relinquishment"),
            body=body,
            item_model=RelinquishmentRequestItem,
            request=request,
            protocol=protocol,
            run_service=lambda items, cert_hash: process_relinquishment(
                db, items, certificate_hash=cert_hash
            ),
        )


@router.post("/deregistration")
def deregistration(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    composition, protocol = _protocol_and_scope(request)
    with runtime_composition_scope(composition):
        return _run_batch(
            procedure="deregistration",
            response_key=protocol.response_key("deregistration"),
            body=body,
            item_model=DeregistrationRequestItem,
            request=request,
            protocol=protocol,
            run_service=lambda items, cert_hash: process_deregistration(
                db, items, certificate_hash=cert_hash
            ),
        )
