"""Protocol adapter contract.

Translates an external envelope into domain operations. Physical consumer
mapping stays on ConsumerAdapter. Discovery is out of scope.
Named industry HTTP APIs are out of scope (later composition).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable

from adapters.device import ADAPTER_API_VERSION, ConsumerAdapter
from primitives.availability import AvailabilityChangeEvent, AvailabilityConstraint
from primitives.decision import Decision
from primitives.request import SpectrumRequest
from primitives.time import UtcInstant

PROTOCOL_API_VERSION = ADAPTER_API_VERSION
GENERIC_JSON_PROTOCOL_ID = "generic-json"


class DomainOperation(StrEnum):
    """Closed inbound operations. Periodic keep-alive is not a protocol verb here."""

    REQUEST_SPECTRUM = "request_spectrum"
    APPLY_AVAILABILITY = "apply_availability"


@dataclass(frozen=True, slots=True)
class ProtocolInbound:
    """Decoded envelope: operation plus canonical spectrum request.

    Optional availability fields carry eLSA-style windows/events without making
    the protocol adapter own network identity (that stays on ConsumerAdapter).
    """

    operation: DomainOperation
    request: SpectrumRequest
    availability_constraints: tuple[AvailabilityConstraint, ...] = ()
    availability_event: AvailabilityChangeEvent | None = None


@runtime_checkable
class ProtocolAdapter(Protocol):
    """Wire envelope ↔ domain. Does not own radio/network identity."""

    @property
    def api_version(self) -> str: ...

    @property
    def protocol_id(self) -> str: ...

    def decode(
        self, envelope: Mapping[str, object], consumer_adapter: ConsumerAdapter
    ) -> ProtocolInbound: ...

    def encode(self, decision: Decision) -> dict[str, object]: ...


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _parse_requested_at(raw: object) -> UtcInstant:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("requested_at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("requested_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("requested_at must be timezone-aware")
    return UtcInstant(parsed.astimezone(timezone.utc))


class GenericJsonProtocolAdapter:
    """Reference JSON envelope codec. Not an industry REST surface."""

    api_version = PROTOCOL_API_VERSION
    protocol_id = GENERIC_JSON_PROTOCOL_ID

    def decode(
        self, envelope: Mapping[str, object], consumer_adapter: ConsumerAdapter
    ) -> ProtocolInbound:
        op_raw = _require_str(envelope, "operation")
        try:
            operation = DomainOperation(op_raw)
        except ValueError as exc:
            raise ValueError(f"unknown operation: {op_raw}") from exc
        request_id = _require_str(envelope, "request_id")
        consumer = envelope.get("consumer")
        if not isinstance(consumer, Mapping):
            raise ValueError("consumer is required")
        view = consumer_adapter.to_consumer(consumer)
        request = SpectrumRequest(
            request_id=request_id,
            holder_id=view.holder_id,
            footprints=view.footprints,
            requested_at=_parse_requested_at(envelope.get("requested_at")),
        )
        return ProtocolInbound(operation=operation, request=request)

    def encode(self, decision: Decision) -> dict[str, object]:
        body: dict[str, object] = {
            "request_id": decision.request_id,
            "action": decision.action.value,
            "reason": decision.reason,
        }
        if decision.authorized_power is not None:
            body["authorized_power_dbm"] = decision.authorized_power.dbm
        return body
