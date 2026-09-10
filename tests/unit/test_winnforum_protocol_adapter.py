"""WInnForum REST protocol adapter over existing v1.2 envelope keys."""

from __future__ import annotations

import pytest

from adapters.discovery import GROUP_PROTOCOL_ADAPTERS, AdapterDiscovery
from adapters.protocol import ProtocolAdapter
from adapters.winnforum_rest import (
    WINNFORUM_REST_PROTOCOL_ID,
    WinnForumRestProtocolAdapter,
    winnforum_rest_protocol_adapter,
)
from services.cbsd_version import PROCEDURE_SPECS


def test_envelope_keys_match_existing_cbsd_sas_contract():
    protocol = WinnForumRestProtocolAdapter()
    assert protocol.protocol_id == WINNFORUM_REST_PROTOCOL_ID
    assert isinstance(protocol, ProtocolAdapter)
    assert protocol.classify("/v1.2/grant", "POST") == "grant"
    assert protocol.request_key("grant") == "grantRequest"
    assert protocol.response_key("heartbeat") == "heartbeatResponse"
    assert protocol.unwrap_request_items("grant", {"grantRequest": [1]}) == [1]
    assert protocol.wrap_response_items("grant", [{"response": {"responseCode": 0}}]) == {
        "grantResponse": [{"response": {"responseCode": 0}}]
    }
    for name, spec in PROCEDURE_SPECS.items():
        assert protocol.request_key(name) == spec.request_key
        assert protocol.response_key(name) == spec.response_key
        assert protocol.spec(name).echo_fields == spec.echo_fields
        assert (
            protocol.spec(name).include_past_transmit_expire
            == spec.include_past_transmit_expire
        )


def test_decode_requires_procedure_array():
    protocol = WinnForumRestProtocolAdapter()
    with pytest.raises(ValueError, match="missing a procedure array"):
        protocol.decode({}, consumer_adapter=None)  # type: ignore[arg-type]


def test_discovery_loads_winnforum_rest_factory():
    discovery = AdapterDiscovery(
        overlays={GROUP_PROTOCOL_ADAPTERS: {"winnforum_rest": winnforum_rest_protocol_adapter}},
        list_entry_points=lambda _g: (),
    )
    loaded = discovery.load(GROUP_PROTOCOL_ADAPTERS, "winnforum_rest")
    assert loaded.protocol_id == WINNFORUM_REST_PROTOCOL_ID
