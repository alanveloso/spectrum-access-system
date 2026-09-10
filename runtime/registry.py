"""Thin registry view over existing adapter/provider/RF discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from adapters.discovery import (
    GROUP_DEVICE_ADAPTERS,
    GROUP_NETWORK_ADAPTERS,
    GROUP_PROTOCOL_ADAPTERS,
    AdapterDiscovery,
)
from adapters.device import AdapterKind, ConsumerAdapter
from adapters.protocol import ProtocolAdapter
from profiles.doctor import (
    package_bootstrap_adapter_discovery,
    package_bootstrap_rf_discovery,
)
from providers.contract import DataProvider
from providers.discovery import DataProviderDiscovery
from rf.discovery import RfModelDiscovery
from rf.port import RfPort
from runtime.errors import IncompatiblePluginError, UnknownPluginError

CATEGORY_PROTOCOL = "protocol"
CATEGORY_DEVICE = "device"
CATEGORY_NETWORK = "network"
CATEGORY_RF = "rf"
CATEGORY_PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class PluginRegistry:
    """Normalization/query over existing discovery — does not rediscover."""

    adapters: AdapterDiscovery
    providers: DataProviderDiscovery
    rf_models: RfModelDiscovery

    @classmethod
    def from_discovery(
        cls,
        *,
        adapters: AdapterDiscovery | None = None,
        providers: DataProviderDiscovery | None = None,
        rf_models: RfModelDiscovery | None = None,
    ) -> PluginRegistry:
        return cls(
            adapters=adapters or package_bootstrap_adapter_discovery(),
            providers=providers or DataProviderDiscovery(),
            rf_models=rf_models or package_bootstrap_rf_discovery(),
        )

    def has(self, category: str, name: str) -> bool:
        if category == CATEGORY_PROTOCOL:
            return name in self.adapters.names(GROUP_PROTOCOL_ADAPTERS)
        if category == CATEGORY_DEVICE:
            return name in self.adapters.names(GROUP_DEVICE_ADAPTERS)
        if category == CATEGORY_NETWORK:
            return name in self.adapters.names(GROUP_NETWORK_ADAPTERS)
        if category == CATEGORY_RF:
            return name in self.rf_models.names()
        if category == CATEGORY_PROVIDER:
            return name in self.providers.names()
        raise ValueError(f"unknown plugin category: {category}")

    def load_protocol(self, name: str, *, profile_id: str | None = None) -> ProtocolAdapter:
        return cast(
            ProtocolAdapter,
            self._load_adapter(
                GROUP_PROTOCOL_ADAPTERS,
                name,
                category=CATEGORY_PROTOCOL,
                profile_id=profile_id,
                expected=ProtocolAdapter,
            ),
        )

    def load_device(self, name: str, *, profile_id: str | None = None) -> ConsumerAdapter:
        plugin = cast(
            ConsumerAdapter,
            self._load_adapter(
                GROUP_DEVICE_ADAPTERS,
                name,
                category=CATEGORY_DEVICE,
                profile_id=profile_id,
                expected=ConsumerAdapter,
            ),
        )
        kind = getattr(plugin, "kind", None)
        if kind is not None and kind is not AdapterKind.DEVICE:
            raise IncompatiblePluginError(
                "selected device plugin has wrong adapter kind",
                profile_id=profile_id,
                plugin=name,
                category=CATEGORY_DEVICE,
                reason="wrong_kind",
            )
        return plugin

    def load_network(self, name: str, *, profile_id: str | None = None) -> ConsumerAdapter:
        plugin = cast(
            ConsumerAdapter,
            self._load_adapter(
                GROUP_NETWORK_ADAPTERS,
                name,
                category=CATEGORY_NETWORK,
                profile_id=profile_id,
                expected=ConsumerAdapter,
            ),
        )
        kind = getattr(plugin, "kind", None)
        if kind is not None and kind is not AdapterKind.NETWORK:
            raise IncompatiblePluginError(
                "selected network plugin has wrong adapter kind",
                profile_id=profile_id,
                plugin=name,
                category=CATEGORY_NETWORK,
                reason="wrong_kind",
            )
        return plugin

    def load_rf(self, name: str, *, profile_id: str | None = None) -> RfPort:
        if name not in self.rf_models.names():
            raise UnknownPluginError(
                f"unknown RF plugin {name!r}",
                profile_id=profile_id,
                plugin=name,
                category=CATEGORY_RF,
                reason="not_found",
            )
        try:
            return self.rf_models.load(name)
        except ValueError as exc:
            raise IncompatiblePluginError(
                f"RF plugin {name!r} failed validation",
                profile_id=profile_id,
                plugin=name,
                category=CATEGORY_RF,
                reason=str(exc),
            ) from exc

    def load_provider(self, name: str, *, profile_id: str | None = None) -> DataProvider:
        if name not in self.providers.names():
            raise UnknownPluginError(
                f"unknown data provider {name!r}",
                profile_id=profile_id,
                plugin=name,
                category=CATEGORY_PROVIDER,
                reason="not_found",
            )
        try:
            return self.providers.load(name)
        except ValueError as exc:
            raise IncompatiblePluginError(
                f"data provider {name!r} failed validation",
                profile_id=profile_id,
                plugin=name,
                category=CATEGORY_PROVIDER,
                reason=str(exc),
            ) from exc

    def _load_adapter(
        self,
        group: str,
        name: str,
        *,
        category: str,
        profile_id: str | None,
        expected: type,
    ) -> Any:
        if name not in self.adapters.names(group):
            raise UnknownPluginError(
                f"unknown {category} plugin {name!r}",
                profile_id=profile_id,
                plugin=name,
                category=category,
                reason="not_found",
            )
        try:
            plugin = self.adapters.load(group, name)
        except ValueError as exc:
            raise IncompatiblePluginError(
                f"{category} plugin {name!r} failed validation",
                profile_id=profile_id,
                plugin=name,
                category=category,
                reason=str(exc),
            ) from exc
        if not isinstance(plugin, expected):
            raise IncompatiblePluginError(
                f"{category} plugin {name!r} does not implement expected interface",
                profile_id=profile_id,
                plugin=name,
                category=category,
                reason="wrong_interface",
            )
        return plugin
