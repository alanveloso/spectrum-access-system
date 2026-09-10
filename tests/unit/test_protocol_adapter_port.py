"""protocol adapter is separate from device/network consumer mapping."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from adapters.device import MappingDeviceAdapter, MappingNetworkAdapter
from adapters.protocol import (
    GENERIC_JSON_PROTOCOL_ID,
    DomainOperation,
    GenericJsonProtocolAdapter,
    ProtocolAdapter,
)
from primitives.decision import Decision, DecisionAction
from primitives.power import PowerDbm
from primitives.profile_context import ProfileContext

_BANNED = (
    "cbsd",
    "cbrs",
    "pal",
    "gaa",
    "grant",
    "heartbeat",
    "winnforum",
    "fcc",
)

_PROFILE = ProfileContext(
    profile_id="ref",
    profile_version="1",
    profile_hash="abc",
)


def _device_envelope() -> dict[str, object]:
    return {
        "operation": "request_spectrum",
        "request_id": "r1",
        "requested_at": "2026-08-16T12:00:00+00:00",
        "consumer": {
            "holder_id": "h1",
            "latitude_deg": 39.0,
            "longitude_deg": -77.0,
            "low_hz": 1000,
            "high_hz": 2000,
            "eirp_dbm": 23.0,
        },
    }


def test_generic_json_decode_uses_injected_consumer_adapter():
    protocol = GenericJsonProtocolAdapter()
    assert protocol.protocol_id == GENERIC_JSON_PROTOCOL_ID
    assert isinstance(protocol, ProtocolAdapter)
    inbound = protocol.decode(_device_envelope(), MappingDeviceAdapter())
    assert inbound.operation is DomainOperation.REQUEST_SPECTRUM
    assert inbound.request.holder_id == "h1"
    assert inbound.request.request_id == "r1"
    assert len(inbound.request.footprints) == 1


def test_same_protocol_accepts_network_consumer_adapter():
    protocol = GenericJsonProtocolAdapter()
    envelope = {
        "operation": "request_spectrum",
        "request_id": "r2",
        "requested_at": "2026-08-16T12:00:00Z",
        "consumer": {
            "holder_id": "net-1",
            "ring": [[0, 0], [1, 0], [1, 1], [0, 0]],
            "low_hz": 1000,
            "high_hz": 2000,
            "eirp_dbm": 30.0,
        },
    }
    inbound = protocol.decode(envelope, MappingNetworkAdapter())
    assert inbound.request.holder_id == "net-1"


def test_unknown_operation_and_missing_consumer_fail_closed():
    protocol = GenericJsonProtocolAdapter()
    device = MappingDeviceAdapter()
    bad = dict(_device_envelope())
    bad["operation"] = "register"
    with pytest.raises(ValueError, match="unknown operation"):
        protocol.decode(bad, device)
    missing = dict(_device_envelope())
    del missing["consumer"]
    with pytest.raises(ValueError, match="consumer"):
        protocol.decode(missing, device)


def test_encode_decision_does_not_embed_consumer_kind():
    protocol = GenericJsonProtocolAdapter()
    decision = Decision(
        request_id="r1",
        action=DecisionAction.REDUCE_POWER,
        profile=_PROFILE,
        reason="cap",
        authorized_power=PowerDbm(20.0),
    )
    body = protocol.encode(decision)
    assert body["action"] == "reduce_power"
    assert body["authorized_power_dbm"] == pytest.approx(20.0)
    assert "kind" not in body
    assert "protocol_id" not in body


def test_protocol_module_has_no_regime_nouns_or_service_imports():
    path = Path(__file__).resolve().parents[2] / "adapters" / "protocol.py"
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()
    for token in _BANNED:
        assert token not in lowered, f"protocol.py contains banned token {token!r}"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("services")
                assert not alias.name.startswith("models")
                assert not alias.name.startswith("routes")
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("services")
            assert not node.module.startswith("models")
            assert not node.module.startswith("routes")
