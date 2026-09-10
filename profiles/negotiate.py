"""Capability negotiation: profile requirements vs plugins.

The profile lists capability tokens and mechanism ids, never vendor/plugin names.
"""

from __future__ import annotations

from adapters.device import (
    AdapterKind,
    ConsumerAdapter,
    ConsumerView,
    consumer_meets_requirements,
)
from adapters.discovery import AdapterDiscovery
from providers.contract import DataProvider, providers_meet_requirements
from rf.port import RfPort
from profiles.schema import ProfileDocument


def _device_caps(profile: ProfileDocument) -> tuple[str, ...]:
    if profile.requirements is None:
        return ()
    return profile.requirements.device_capabilities


def _network_caps(profile: ProfileDocument) -> tuple[str, ...]:
    if profile.requirements is None:
        return ()
    return profile.requirements.network_capabilities


def _data_caps(profile: ProfileDocument) -> tuple[str, ...]:
    if profile.data is None:
        return ()
    return profile.data.required_capabilities


def negotiate_profile_plugins(
    profile: ProfileDocument,
    *,
    consumer: ConsumerView | None = None,
    consumer_adapter: ConsumerAdapter | None = None,
    providers: tuple[DataProvider, ...] = (),
    rf_port: RfPort | None = None,
) -> None:
    """Fail closed when offered plugins do not meet profile capabilities."""
    required_device = _device_caps(profile)
    required_network = _network_caps(profile)
    if required_device or required_network:
        if consumer_adapter is None and consumer is None:
            raise ValueError(
                "profile requires consumer capabilities but no consumer was offered"
            )
    if required_device:
        if consumer_adapter is not None:
            missing = [
                cap
                for cap in required_device
                if cap not in consumer_adapter.advertised_capabilities()
            ]
            if missing:
                raise ValueError(f"consumer adapter missing required capabilities: {missing}")
        if consumer is not None:
            consumer_meets_requirements(consumer, required_device)
    if required_network:
        if consumer_adapter is not None:
            if getattr(consumer_adapter, "kind", None) is not None:
                kind = consumer_adapter.kind
                if kind is not AdapterKind.NETWORK:
                    raise ValueError(
                        "profile requires network capabilities but adapter kind is not network"
                    )
            missing = [
                cap
                for cap in required_network
                if cap not in consumer_adapter.advertised_capabilities()
            ]
            if missing:
                raise ValueError(f"network adapter missing required capabilities: {missing}")
        if consumer is not None:
            consumer_meets_requirements(consumer, required_network)

    required_data = _data_caps(profile)
    if required_data:
        providers_meet_requirements(providers, required_data)

    rf = profile.rf
    if rf is None or not rf.required:
        return
    if rf_port is None:
        raise ValueError("profile requires RF but no RF port was offered")
    expected = rf.propagation_model
    if expected is None:
        raise ValueError("rf.required requires propagation_model")
    if rf_port.model_id != expected:
        raise ValueError(
            f"RF port model_id {rf_port.model_id!r} does not satisfy {expected!r}"
        )


def adapters_satisfying_device_capabilities(
    discovery: AdapterDiscovery,
    group: str,
    required: tuple[str, ...],
) -> tuple[str, ...]:
    """Discovery names that meet capabilities. Profile must not hardcode these names."""
    matched: list[str] = []
    for name in sorted(discovery.names(group)):
        plugin = discovery.load(group, name)
        caps = plugin.advertised_capabilities()
        if all(cap in caps for cap in required):
            matched.append(name)
    if required and not matched:
        raise ValueError("no installed adapter satisfies required device capabilities")
    return tuple(matched)


def adapters_satisfying_network_capabilities(
    discovery: AdapterDiscovery,
    group: str,
    required: tuple[str, ...],
) -> tuple[str, ...]:
    """Like device helper; fail closed when no network adapter meets tokens."""
    matched: list[str] = []
    for name in sorted(discovery.names(group)):
        plugin = discovery.load(group, name)
        caps = plugin.advertised_capabilities()
        if all(cap in caps for cap in required):
            matched.append(name)
    if required and not matched:
        raise ValueError("no installed adapter satisfies required network capabilities")
    return tuple(matched)
