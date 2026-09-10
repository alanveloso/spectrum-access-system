"""eLSA1 protocol adapter vertical slice.

Proves protocol + network adapter composition: eLSA1 wire envelopes decode via
an injected ``ConsumerAdapter`` (typically ``ManagedNetworkAdapter``), never via
fake CBSD identity. Full ETSI information-element bit encoding is out of scope;
this slice covers procedure envelopes for eLSRAI notification/request and
eLSR grant request, mapping zones onto ``AvailabilityConstraint``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from adapters.device import AdapterKind, ConsumerAdapter
from adapters.protocol import PROTOCOL_API_VERSION, DomainOperation, ProtocolInbound
from primitives.availability import (
    AvailabilityChangeEvent,
    AvailabilityConstraint,
    AvailabilityEventKind,
    AvailabilityMode,
    AvailabilityScope,
    AvailabilityZoneKind,
)
from primitives.decision import Decision
from primitives.frequency import FrequencyRange
from primitives.geography import LinearRing
from primitives.power import PowerDbm
from primitives.request import SpectrumRequest
from primitives.time import TimeInterval, UtcInstant

ELSA1_PROTOCOL_ID = "elsa1"

_PROCEDURE_ELSR_GRANT = "elsrGrantRequest"
_PROCEDURE_ELSRAI_NOTIFICATION = "elsraiNotification"
_PROCEDURE_ELSRAI_REQUEST = "elsraiRequest"
_PROCEDURE_ELSRAI_CONFIRMATION = "elsraiConfirmation"

_SUPPORTED = frozenset(
    {
        _PROCEDURE_ELSR_GRANT,
        _PROCEDURE_ELSRAI_NOTIFICATION,
        _PROCEDURE_ELSRAI_REQUEST,
        _PROCEDURE_ELSRAI_CONFIRMATION,
    }
)

_REJECTED_CBSD_KEYS = frozenset(
    {
        "cbsdId",
        "fccId",
        "cbsdSerialNumber",
        "grantId",
        "installationParam",
        "cbsdInfo",
        "heartbeatRequest",
        "grantRequest",
    }
)


def _reject_cbsd_shaped(envelope: Mapping[str, object]) -> None:
    present = sorted(key for key in _REJECTED_CBSD_KEYS if key in envelope)
    if present:
        raise ValueError(
            "elsa1 protocol rejects CBSD/Grant envelope keys: " + ", ".join(present)
        )


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _parse_instant(raw: object, *, field: str) -> UtcInstant:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return UtcInstant(parsed.astimezone(timezone.utc))


def _parse_zone(raw: Mapping[str, object]) -> AvailabilityConstraint:
    zone_id = _require_str(raw, "id")
    kind_raw = _require_str(raw, "kind")
    try:
        kind = AvailabilityZoneKind(kind_raw)
    except ValueError as exc:
        raise ValueError(f"unsupported zone kind: {kind_raw}") from exc
    low = raw.get("low_hz")
    high = raw.get("high_hz")
    if not isinstance(low, int) or not isinstance(high, int):
        raise ValueError(f"zone {zone_id}: low_hz and high_hz are required integers")
    ring = raw.get("ring")
    area = None
    if ring is not None:
        if not isinstance(ring, (list, tuple)):
            raise ValueError(f"zone {zone_id}: ring must be a list")
        area = LinearRing.from_lon_lat(ring)
    max_eirp = None
    eirp = raw.get("max_eirp_dbm")
    if eirp is not None:
        if not isinstance(eirp, (int, float)):
            raise ValueError(f"zone {zone_id}: max_eirp_dbm must be numeric")
        max_eirp = PowerDbm(float(eirp))
    start = _parse_instant(raw.get("validity_start"), field=f"zone {zone_id}.validity_start")
    end = _parse_instant(raw.get("validity_end"), field=f"zone {zone_id}.validity_end")
    mode_raw = raw.get("mode", "on_demand")
    if not isinstance(mode_raw, str):
        raise ValueError(f"zone {zone_id}: mode must be a string")
    try:
        mode = AvailabilityMode(mode_raw)
    except ValueError as exc:
        raise ValueError(f"zone {zone_id}: unsupported mode {mode_raw}") from exc
    source = raw.get("source_id")
    source_id = source.strip() if isinstance(source, str) and source.strip() else None
    return AvailabilityConstraint(
        constraint_id=zone_id,
        mode=mode,
        validity=TimeInterval(start=start, end=end),
        scope=AvailabilityScope(
            frequency=FrequencyRange(low_hz=low, high_hz=high),
            area=area,
            max_eirp=max_eirp,
        ),
        zone_kind=kind,
        source_id=source_id,
    )


def _parse_elsrai(
    raw: object,
) -> tuple[tuple[AvailabilityConstraint, ...], AvailabilityChangeEvent | None]:
    if not isinstance(raw, Mapping):
        raise ValueError("elsrai is required")
    zones_raw = raw.get("zones")
    if not isinstance(zones_raw, Sequence) or isinstance(zones_raw, (str, bytes)):
        raise ValueError("elsrai.zones must be a list")
    if not zones_raw:
        raise ValueError("elsrai.zones must be non-empty")
    zones: list[AvailabilityConstraint] = []
    for index, item in enumerate(zones_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"elsrai.zones[{index}] must be a mapping")
        zones.append(_parse_zone(item))
    event = None
    event_kind = raw.get("event_kind")
    if event_kind is not None:
        if not isinstance(event_kind, str):
            raise ValueError("elsrai.event_kind must be a string")
        try:
            kind = AvailabilityEventKind(event_kind)
        except ValueError as exc:
            raise ValueError(f"unsupported elsrai.event_kind: {event_kind}") from exc
        event = AvailabilityChangeEvent(
            event_id=_require_str(raw, "event_id"),
            constraint_id=zones[0].constraint_id,
            observed_at=_parse_instant(raw.get("observed_at"), field="elsrai.observed_at"),
            kind=kind,
        )
    return tuple(zones), event


class Elsa1ProtocolAdapter:
    """eLSA1 procedure envelope codec. Network identity stays on ConsumerAdapter."""

    api_version = PROTOCOL_API_VERSION
    protocol_id = ELSA1_PROTOCOL_ID

    def procedure_names(self) -> tuple[str, ...]:
        return tuple(sorted(_SUPPORTED))

    def decode(
        self, envelope: Mapping[str, object], consumer_adapter: ConsumerAdapter
    ) -> ProtocolInbound:
        _reject_cbsd_shaped(envelope)
        if consumer_adapter.kind is not AdapterKind.NETWORK:
            raise ValueError("elsa1 requires a network ConsumerAdapter")
        procedure = _require_str(envelope, "procedure")
        if procedure not in _SUPPORTED:
            raise ValueError(f"unsupported elsa1 procedure: {procedure}")
        request_id = _require_str(envelope, "transaction_id")
        consumer = envelope.get("consumer")
        if not isinstance(consumer, Mapping):
            raise ValueError("consumer is required")
        view = consumer_adapter.to_consumer(consumer)
        requested_at = _parse_instant(
            envelope.get("requested_at"), field="requested_at"
        )
        request = SpectrumRequest(
            request_id=request_id,
            holder_id=view.holder_id,
            footprints=view.footprints,
            requested_at=requested_at,
        )
        if procedure == _PROCEDURE_ELSR_GRANT:
            return ProtocolInbound(
                operation=DomainOperation.REQUEST_SPECTRUM,
                request=request,
            )
        if procedure == _PROCEDURE_ELSRAI_CONFIRMATION:
 # Confirmation carries ack only; still require network consumer context.
            return ProtocolInbound(
                operation=DomainOperation.APPLY_AVAILABILITY,
                request=request,
            )
        constraints, event = _parse_elsrai(envelope.get("elsrai"))
        return ProtocolInbound(
            operation=DomainOperation.APPLY_AVAILABILITY,
            request=request,
            availability_constraints=constraints,
            availability_event=event,
        )

    def encode(self, decision: Decision) -> dict[str, object]:
        body: dict[str, object] = {
            "protocol_id": self.protocol_id,
            "transaction_id": decision.request_id,
            "action": decision.action.value,
            "reason": decision.reason,
        }
        if decision.authorized_power is not None:
            body["authorized_power_dbm"] = decision.authorized_power.dbm
        return body


def elsa1_protocol_adapter() -> Elsa1ProtocolAdapter:
    """Entry-point factory for spectrum_access.protocol_adapters."""
    return Elsa1ProtocolAdapter()
