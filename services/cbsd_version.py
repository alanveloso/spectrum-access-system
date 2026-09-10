"""Uniform CBSD-SAS unsupported-version responses (WINNF responseCode 100)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from adapters.winnforum_rest import WINNFORUM_PROCEDURES, WINNFORUM_SAS_VERSION
from services.error_handlers import INVALID_VALUE, MAXIMUM_BATCH_SIZE, VERSION_UNSUPPORTED

# Mounted implementation version for CBSD-SAS procedures (routes/cbsd_routes.py).
# Matching is case-sensitive and aligned with the concrete /v1.2 router prefix.
SUPPORTED_CBSD_SAS_VERSIONS: frozenset[str] = frozenset({WINNFORUM_SAS_VERSION})


class UnsupportedVersionBatchError(ValueError):
    """Raised when the unsupported-version request body cannot be accepted."""

    def __init__(self, message: str, *, response_code: int = INVALID_VALUE) -> None:
        super().__init__(message)
        self.response_code = response_code


@dataclass(frozen=True)
class ProcedureVersionSpec:
    request_key: str
    response_key: str
    echo_fields: tuple[str, ...]
    include_past_transmit_expire: bool = False


# Wire-schema constants — not a post-composition adapter construction.
PROCEDURE_SPECS: dict[str, ProcedureVersionSpec] = {
    spec.name: ProcedureVersionSpec(
        request_key=spec.request_key,
        response_key=spec.response_key,
        echo_fields=spec.echo_fields,
        include_past_transmit_expire=spec.include_past_transmit_expire,
    )
    for spec in WINNFORUM_PROCEDURES
}


def is_supported_cbsd_sas_version(version: str) -> bool:
    return version.strip() in SUPPORTED_CBSD_SAS_VERSIONS


def _past_transmit_expire_time(*, now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    past = instant.replace(microsecond=0) - timedelta(seconds=1)
    return past.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_unsupported_version_item(
    spec: ProcedureVersionSpec,
    raw_item: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"response": {"responseCode": VERSION_UNSUPPORTED}}
    if isinstance(raw_item, dict):
        for field in spec.echo_fields:
            if raw_item.get(field) is not None:
                item[field] = raw_item[field]
    if spec.include_past_transmit_expire:
        item["transmitExpireTime"] = _past_transmit_expire_time(now=now)
    return item


def build_unsupported_version_body(
    procedure: str,
    body: Any,
    *,
    now: datetime | None = None,
    max_batch_size: int = MAXIMUM_BATCH_SIZE,
) -> dict[str, Any]:
    """Build the WINNF batch envelope for an unsupported protocol version."""
    if procedure not in PROCEDURE_SPECS:
        raise KeyError(f"unknown CBSD procedure {procedure!r}")
    spec = PROCEDURE_SPECS[procedure]
    if not isinstance(body, dict):
        raise UnsupportedVersionBatchError(
            "request body must be a JSON object",
            response_code=INVALID_VALUE,
        )
    raw = body.get(spec.request_key)
    if raw is None:
        raw_items: list[Any] = []
    elif not isinstance(raw, list):
        raise UnsupportedVersionBatchError(
            f"{spec.request_key} must be a list",
            response_code=INVALID_VALUE,
        )
    else:
        raw_items = raw
    if len(raw_items) > max_batch_size:
        raise UnsupportedVersionBatchError(
            f"batch exceeds MaximumBatchSize ({max_batch_size})",
            response_code=INVALID_VALUE,
        )
    responses = [
        build_unsupported_version_item(spec, raw_item, now=now) for raw_item in raw_items
    ]
    return {spec.response_key: responses}


def malformed_body_response(procedure: str, *, code: int = INVALID_VALUE) -> dict[str, Any]:
    """Single-item WINNF envelope when the request body cannot be parsed."""
    if procedure not in PROCEDURE_SPECS:
        raise KeyError(f"unknown CBSD procedure {procedure!r}")
    spec = PROCEDURE_SPECS[procedure]
    item: dict[str, Any] = {"response": {"responseCode": code}}
    if spec.include_past_transmit_expire:
        item["transmitExpireTime"] = _past_transmit_expire_time()
    return {spec.response_key: [item]}
