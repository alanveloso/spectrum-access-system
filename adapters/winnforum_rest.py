"""WInnForum CBSD-SAS REST protocol adapter.

Owns v1.2 procedure envelope keys used by existing routes. CBSD items map to
ConsumerView via an injected ConsumerAdapter. Domain admission rewrite
is out of scope. HTTP status/responseCode behavior stays in routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from adapters.device import ConsumerAdapter
from adapters.protocol import PROTOCOL_API_VERSION, DomainOperation, ProtocolInbound
from primitives.decision import Decision
from primitives.request import SpectrumRequest
from primitives.time import UtcInstant

WINNFORUM_REST_PROTOCOL_ID = "winnforum-rest"
WINNFORUM_SAS_VERSION = "v1.2"


@dataclass(frozen=True, slots=True)
class WinnForumProcedureSpec:
    name: str
    request_key: str
    response_key: str
    echo_fields: tuple[str, ...] = ()
    include_past_transmit_expire: bool = False


WINNFORUM_PROCEDURES: tuple[WinnForumProcedureSpec, ...] = (
    WinnForumProcedureSpec("registration", "registrationRequest", "registrationResponse"),
    WinnForumProcedureSpec(
        "spectrumInquiry",
        "spectrumInquiryRequest",
        "spectrumInquiryResponse",
        echo_fields=("cbsdId",),
    ),
    WinnForumProcedureSpec(
        "grant", "grantRequest", "grantResponse", echo_fields=("cbsdId",)
    ),
    WinnForumProcedureSpec(
        "heartbeat",
        "heartbeatRequest",
        "heartbeatResponse",
        echo_fields=("cbsdId", "grantId"),
        include_past_transmit_expire=True,
    ),
    WinnForumProcedureSpec(
        "relinquishment",
        "relinquishmentRequest",
        "relinquishmentResponse",
        echo_fields=("cbsdId", "grantId"),
    ),
    WinnForumProcedureSpec(
        "deregistration",
        "deregistrationRequest",
        "deregistrationResponse",
        echo_fields=("cbsdId",),
    ),
)

_BY_NAME = {spec.name: spec for spec in WINNFORUM_PROCEDURES}


class WinnForumRestProtocolAdapter:
    """Wire envelope for CBSD-SAS v1.2. Does not own radio identity."""

    api_version = PROTOCOL_API_VERSION
    protocol_id = WINNFORUM_REST_PROTOCOL_ID

    def procedure_specs(self) -> tuple[WinnForumProcedureSpec, ...]:
        return WINNFORUM_PROCEDURES

    def procedure_names(self) -> tuple[str, ...]:
        return tuple(_BY_NAME)

    def spec(self, procedure: str) -> WinnForumProcedureSpec:
        try:
            return _BY_NAME[procedure]
        except KeyError:
            raise ValueError(f"unknown WInnForum procedure: {procedure}") from None

    def request_key(self, procedure: str) -> str:
        return self.spec(procedure).request_key

    def response_key(self, procedure: str) -> str:
        return self.spec(procedure).response_key

    def classify(self, path: str, method: str) -> str:
        if method.upper() != "POST":
            raise ValueError("WInnForum CBSD-SAS procedures are POST")
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2:
            raise ValueError(f"unsupported WInnForum path: {path}")
        version, procedure = parts
        if version != WINNFORUM_SAS_VERSION:
            raise ValueError(f"unsupported WInnForum version: {version}")
        return self.spec(procedure).name

    def unwrap_request_items(
        self, procedure: str, body: Mapping[str, object]
    ) -> object:
        return body.get(self.request_key(procedure))

    def wrap_response_items(
        self, procedure: str, items: list[object]
    ) -> dict[str, object]:
        return {self.response_key(procedure): items}

    def decode(
        self, envelope: Mapping[str, object], consumer_adapter: ConsumerAdapter
    ) -> ProtocolInbound:
        item = self._first_item(envelope)
        view = consumer_adapter.to_consumer(item)
        holder = view.holder_id
        return ProtocolInbound(
            operation=DomainOperation.REQUEST_SPECTRUM,
            request=SpectrumRequest(
                request_id=holder,
                holder_id=holder,
                footprints=view.footprints,
                requested_at=UtcInstant(datetime.now(timezone.utc)),
            ),
        )

    def _first_item(self, envelope: Mapping[str, object]) -> Mapping[str, object]:
        for spec in WINNFORUM_PROCEDURES:
            raw = envelope.get(spec.request_key)
            if raw is None:
                continue
            if not isinstance(raw, list) or not raw:
                raise ValueError("WInnForum batch must contain at least one item")
            first = raw[0]
            if not isinstance(first, Mapping):
                raise ValueError("WInnForum item must be an object")
            return first
        raise ValueError("WInnForum request envelope is missing a procedure array")

    def encode(self, decision: Decision) -> dict[str, object]:
        raise ValueError("WInnForum HTTP responses are not generic Decision envelopes")


def winnforum_rest_protocol_adapter() -> WinnForumRestProtocolAdapter:
    return WinnForumRestProtocolAdapter()
