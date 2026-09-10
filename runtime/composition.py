"""Compose Spectrum Profile + DeploymentConfig + plugins into a runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from adapters.device import ConsumerAdapter
from adapters.protocol import ProtocolAdapter
from profiles.context import profile_context_from_document, profile_hash
from profiles.negotiate import negotiate_profile_plugins
from profiles.schema import ProfileDocument
from providers.contract import DataProvider, providers_meet_requirements
from rf.port import RfPort
from runtime.capabilities import RuntimeRequirements, required_capabilities
from runtime.deployment import DeploymentConfig, deployment_hash
from runtime.errors import (
    IncompatiblePluginError,
    MissingCapabilityError,
    RuntimeCompositionError,
)
from runtime.registry import PluginRegistry


@dataclass(frozen=True, slots=True)
class CompositionProvenance:
    """Audit metadata for a composed runtime (no secrets)."""

    profile_id: str
    profile_version: str
    profile_hash: str
    deployment_id: str
    deployment_version: str
    deployment_hash: str
    required_capabilities: tuple[str, ...]
    resolved: Mapping[str, str]
    selected_plugins: Mapping[str, str]
    unsatisfied_data_capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "deployment_id": self.deployment_id,
            "deployment_version": self.deployment_version,
            "deployment_hash": self.deployment_hash,
            "required_capabilities": list(self.required_capabilities),
            "resolved": dict(self.resolved),
            "selected_plugins": dict(self.selected_plugins),
            "unsatisfied_data_capabilities": list(self.unsatisfied_data_capabilities),
        }


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """Validated runtime dependencies for one profile + deployment pair."""

    profile: ProfileDocument
    profile_context: Any
    requirements: RuntimeRequirements
    deployment: DeploymentConfig
    protocol_adapter: ProtocolAdapter | None
    device_adapter: ConsumerAdapter | None
    network_adapter: ConsumerAdapter | None
    providers: tuple[DataProvider, ...]
    rf: RfPort | None
    provenance: CompositionProvenance

    def provider_for(self, capability: str) -> DataProvider:
        """Return the first composed provider advertising ``capability``.

        Domain code depends on capability tokens, not plugin names.
        """
        token = capability.removeprefix("data.")
        for provider in self.providers:
            if token in provider.advertised_capabilities():
                return provider
        raise MissingCapabilityError(
            f"no composed provider advertises capability {token!r}",
            profile_id=self.profile.metadata.id,
            capability=token,
            category="provider",
            reason="not_composed",
        )


def compose_runtime(
    profile: ProfileDocument,
    deployment: DeploymentConfig,
    registry: PluginRegistry,
    *,
    require_data_plugins: bool = True,
) -> RuntimeComposition:
    """Validate and resolve deployment selections against profile requirements.

    Fail closed for mandatory capabilities including data capabilities.
    ``require_data_plugins=False`` is reserved for explicit test fixtures that
    intentionally compose incomplete deployments; production bootstrap must
    never pass False.
    """
    profile_id = profile.metadata.id
    requirements = required_capabilities(profile)
    resolved: dict[str, str] = {}
    selected: dict[str, str] = {}
    unsatisfied_data: tuple[str, ...] = ()

    protocol_adapter: ProtocolAdapter | None = None
    if deployment.protocol is not None:
        protocol_adapter = registry.load_protocol(
            deployment.protocol.plugin, profile_id=profile_id
        )
        selected["protocol"] = deployment.protocol.plugin
        resolved["protocol"] = deployment.protocol.plugin

    device_adapter: ConsumerAdapter | None = None
    if requirements.device_capabilities:
        if deployment.device is None:
            raise MissingCapabilityError(
                "profile requires device capabilities but deployment selected none",
                profile_id=profile_id,
                capability="device",
                category="device",
                reason="missing_selection",
            )
        device_adapter = registry.load_device(
            deployment.device.plugin, profile_id=profile_id
        )
        missing = sorted(
            cap
            for cap in requirements.device_capabilities
            if cap not in device_adapter.advertised_capabilities()
        )
        if missing:
            raise IncompatiblePluginError(
                f"device plugin missing required capabilities: {missing}",
                profile_id=profile_id,
                plugin=deployment.device.plugin,
                category="device",
                capability=",".join(missing),
                reason="missing_capabilities",
            )
        selected["device"] = deployment.device.plugin
        for cap in sorted(requirements.device_capabilities):
            resolved[f"device.{cap}"] = deployment.device.plugin
    elif deployment.device is not None:
        device_adapter = registry.load_device(
            deployment.device.plugin, profile_id=profile_id
        )
        selected["device"] = deployment.device.plugin

    network_adapter: ConsumerAdapter | None = None
    if requirements.network_capabilities:
        if deployment.network is None:
            raise MissingCapabilityError(
                "profile requires network capabilities but deployment selected none",
                profile_id=profile_id,
                capability="network",
                category="network",
                reason="missing_selection",
            )
        network_adapter = registry.load_network(
            deployment.network.plugin, profile_id=profile_id
        )
        missing = sorted(
            cap
            for cap in requirements.network_capabilities
            if cap not in network_adapter.advertised_capabilities()
        )
        if missing:
            raise IncompatiblePluginError(
                f"network plugin missing required capabilities: {missing}",
                profile_id=profile_id,
                plugin=deployment.network.plugin,
                category="network",
                capability=",".join(missing),
                reason="missing_capabilities",
            )
        selected["network"] = deployment.network.plugin
        for cap in sorted(requirements.network_capabilities):
            resolved[f"network.{cap}"] = deployment.network.plugin
    elif deployment.network is not None:
        network_adapter = registry.load_network(
            deployment.network.plugin, profile_id=profile_id
        )
        selected["network"] = deployment.network.plugin

    rf_port: RfPort | None = None
    if requirements.rf_required:
        if deployment.rf is None:
            raise MissingCapabilityError(
                "profile requires RF propagation but deployment selected none",
                profile_id=profile_id,
                capability=f"rf.{requirements.rf_propagation_model}",
                category="rf",
                reason="missing_selection",
            )
        rf_port = registry.load_rf(deployment.rf.plugin, profile_id=profile_id)
        expected = requirements.rf_propagation_model
        if rf_port.model_id != expected:
            raise IncompatiblePluginError(
                f"RF plugin model_id {rf_port.model_id!r} does not satisfy {expected!r}",
                profile_id=profile_id,
                plugin=deployment.rf.plugin,
                category="rf",
                capability=f"rf.{expected}",
                reason="model_mismatch",
            )
        selected["rf"] = deployment.rf.plugin
        resolved[f"rf.{expected}"] = deployment.rf.plugin
    elif deployment.rf is not None:
        rf_port = registry.load_rf(deployment.rf.plugin, profile_id=profile_id)
        selected["rf"] = deployment.rf.plugin

    providers: list[DataProvider] = []
    for index, selection in enumerate(deployment.providers):
        provider = registry.load_provider(selection.plugin, profile_id=profile_id)
        providers.append(provider)
        selected[f"provider[{index}]"] = selection.plugin
        for cap in sorted(provider.advertised_capabilities()):
            resolved.setdefault(f"data.{cap}", selection.plugin)

    if requirements.data_capabilities:
        have = set()
        for provider in providers:
            have |= set(provider.advertised_capabilities())
        missing_data = tuple(
            sorted(cap for cap in requirements.data_capabilities if cap not in have)
        )
        if missing_data:
            if require_data_plugins:
                raise MissingCapabilityError(
                    f"data providers missing required capabilities: {list(missing_data)}",
                    profile_id=profile_id,
                    capability=",".join(missing_data),
                    category="provider",
                    reason="missing_capabilities",
                )
            unsatisfied_data = missing_data
        else:
            try:
                providers_meet_requirements(
                    tuple(providers), tuple(sorted(requirements.data_capabilities))
                )
            except ValueError as exc:
                raise MissingCapabilityError(
                    str(exc),
                    profile_id=profile_id,
                    category="provider",
                    reason="missing_capabilities",
                ) from exc

    consumer_adapter = device_adapter or network_adapter
    try:
        negotiate_profile_plugins(
            profile,
            consumer_adapter=consumer_adapter,
            providers=tuple(providers),
            rf_port=rf_port,
        )
    except ValueError as exc:
        # When data is advisory, negotiate may still fail on missing data.
        if (
            not require_data_plugins
            and unsatisfied_data
            and "data providers missing" in str(exc)
        ):
            pass
        else:
            raise RuntimeCompositionError(
                f"capability negotiation failed: {exc}",
                profile_id=profile_id,
                reason=str(exc),
            ) from exc

    context = profile_context_from_document(profile)
    provenance = CompositionProvenance(
        profile_id=profile_id,
        profile_version=profile.metadata.version,
        profile_hash=profile_hash(profile),
        deployment_id=deployment.id,
        deployment_version=deployment.version,
        deployment_hash=deployment_hash(deployment),
        required_capabilities=requirements.as_sorted_tokens(),
        resolved=dict(sorted(resolved.items())),
        selected_plugins=dict(sorted(selected.items())),
        unsatisfied_data_capabilities=unsatisfied_data,
    )
    return RuntimeComposition(
        profile=profile,
        profile_context=context,
        requirements=requirements,
        deployment=deployment,
        protocol_adapter=protocol_adapter,
        device_adapter=device_adapter,
        network_adapter=network_adapter,
        providers=tuple(providers),
        rf=rf_port,
        provenance=provenance,
    )
