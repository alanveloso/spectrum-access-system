"""Generic device, network, and protocol adapter surface.

Regime-specific codecs live in dedicated modules, not this barrel.
"""

from adapters.device import (
    ADAPTER_API_VERSION,
    AdapterKind,
    ConsumerAdapter,
    ConsumerView,
    MappingDeviceAdapter,
    MappingNetworkAdapter,
    consumer_meets_requirements,
)
from adapters.discovery import (
    ADAPTER_GROUPS,
    GROUP_DEVICE_ADAPTERS,
    GROUP_NETWORK_ADAPTERS,
    GROUP_PROTOCOL_ADAPTERS,
    AdapterDiscovery,
)
from adapters.elsa1 import ELSA1_PROTOCOL_ID, Elsa1ProtocolAdapter, elsa1_protocol_adapter
from adapters.managed_consumer import ManagedNetworkAdapter, managed_network_adapter
from adapters.protocol import (
    GENERIC_JSON_PROTOCOL_ID,
    PROTOCOL_API_VERSION,
    DomainOperation,
    GenericJsonProtocolAdapter,
    ProtocolAdapter,
    ProtocolInbound,
)

__all__ = [
    "ADAPTER_API_VERSION",
    "ADAPTER_GROUPS",
    "PROTOCOL_API_VERSION",
    "ELSA1_PROTOCOL_ID",
    "GENERIC_JSON_PROTOCOL_ID",
    "GROUP_DEVICE_ADAPTERS",
    "GROUP_NETWORK_ADAPTERS",
    "GROUP_PROTOCOL_ADAPTERS",
    "AdapterDiscovery",
    "AdapterKind",
    "ConsumerAdapter",
    "ConsumerView",
    "DomainOperation",
    "Elsa1ProtocolAdapter",
    "GenericJsonProtocolAdapter",
    "ManagedNetworkAdapter",
    "MappingDeviceAdapter",
    "MappingNetworkAdapter",
    "ProtocolAdapter",
    "ProtocolInbound",
    "consumer_meets_requirements",
    "elsa1_protocol_adapter",
    "managed_network_adapter",
]
