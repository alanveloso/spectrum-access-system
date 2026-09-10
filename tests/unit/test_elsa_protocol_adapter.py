"""eLSA1 protocol + managed network adapter vertical slice."""

from __future__ import annotations

import pytest

from adapters.device import MappingDeviceAdapter
from adapters.elsa1 import ELSA1_PROTOCOL_ID, Elsa1ProtocolAdapter, elsa1_protocol_adapter
from adapters.managed_consumer import ManagedNetworkAdapter
from adapters.protocol import DomainOperation, ProtocolAdapter
from primitives.availability import AvailabilityEventKind, AvailabilityZoneKind
from primitives.decision import Decision, DecisionAction
from primitives.profile_context import ProfileContext

_PROFILE = ProfileContext(
    profile_id="eu_elsa_probe",
    profile_version="0.0.1",
    profile_hash="abc",
)

_RING = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]


def _consumer() -> dict[str, object]:
    return {
        "network_id": "mfcn-1",
        "vsp_id": "vsp-a",
        "ring": _RING,
        "low_hz": 2_300_000_000,
        "high_hz": 2_310_000_000,
        "eirp_dbm": 30.0,
    }


def _elsrai_zone() -> dict[str, object]:
    return {
        "id": "zone-allow-1",
        "kind": "allowance",
        "low_hz": 2_300_000_000,
        "high_hz": 2_320_000_000,
        "ring": _RING,
        "max_eirp_dbm": 30.0,
        "validity_start": "2026-08-20T12:00:00+00:00",
        "validity_end": "2026-08-20T18:00:00+00:00",
        "mode": "scheduled",
        "source_id": "incumbent-x",
    }


def test_elsa1_is_protocol_adapter_with_network_consumer() -> None:
    protocol = elsa1_protocol_adapter()
    assert isinstance(protocol, ProtocolAdapter)
    assert protocol.protocol_id == ELSA1_PROTOCOL_ID
    assert "elsraiNotification" in protocol.procedure_names()
    envelope = {
        "procedure": "elsrGrantRequest",
        "transaction_id": "tx-1",
        "requested_at": "2026-08-20T12:00:00Z",
        "consumer": _consumer(),
    }
    inbound = protocol.decode(envelope, ManagedNetworkAdapter())
    assert inbound.operation is DomainOperation.REQUEST_SPECTRUM
    assert inbound.request.holder_id == "vsp-a/mfcn-1"
    assert inbound.availability_constraints == ()


def test_elsrai_notification_maps_availability_constraints() -> None:
    protocol = Elsa1ProtocolAdapter()
    envelope = {
        "procedure": "elsraiNotification",
        "transaction_id": "tx-2",
        "requested_at": "2026-08-20T12:00:00+00:00",
        "consumer": _consumer(),
        "elsrai": {
            "zones": [_elsrai_zone()],
            "event_kind": "updated",
            "event_id": "ev-1",
            "observed_at": "2026-08-20T12:00:01+00:00",
        },
    }
    inbound = protocol.decode(envelope, ManagedNetworkAdapter())
    assert inbound.operation is DomainOperation.APPLY_AVAILABILITY
    assert len(inbound.availability_constraints) == 1
    zone = inbound.availability_constraints[0]
    assert zone.zone_kind is AvailabilityZoneKind.ALLOWANCE
    assert zone.source_id == "incumbent-x"
    assert inbound.availability_event is not None
    assert inbound.availability_event.kind is AvailabilityEventKind.UPDATED


def test_elsa1_rejects_device_adapter_and_cbsd_keys() -> None:
    protocol = Elsa1ProtocolAdapter()
    envelope = {
        "procedure": "elsrGrantRequest",
        "transaction_id": "tx-3",
        "requested_at": "2026-08-20T12:00:00+00:00",
        "consumer": _consumer(),
    }
    with pytest.raises(ValueError, match="network ConsumerAdapter"):
        protocol.decode(envelope, MappingDeviceAdapter())
    bad = dict(envelope)
    bad["cbsdId"] = "cbsd-1"
    with pytest.raises(ValueError, match="rejects CBSD"):
        protocol.decode(bad, ManagedNetworkAdapter())


def test_elsa1_encode_decision_separate_from_consumer() -> None:
    protocol = Elsa1ProtocolAdapter()
    encoded = protocol.encode(
        Decision(
            request_id="tx-9",
            action=DecisionAction.KEEP,
            profile=_PROFILE,
            reason="availability ok",
        )
    )
    assert encoded["protocol_id"] == ELSA1_PROTOCOL_ID
    assert encoded["transaction_id"] == "tx-9"
    assert "cbsdId" not in encoded
    assert "grantId" not in encoded


def test_unknown_procedure_fail_closed() -> None:
    protocol = Elsa1ProtocolAdapter()
    with pytest.raises(ValueError, match="unsupported elsa1 procedure"):
        protocol.decode(
            {
                "procedure": "heartbeat",
                "transaction_id": "tx",
                "requested_at": "2026-08-20T12:00:00+00:00",
                "consumer": _consumer(),
            },
            ManagedNetworkAdapter(),
        )
