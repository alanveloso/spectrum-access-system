"""Derive runtime capability requirements from a Spectrum Profile.

Derivation is schema/mechanism driven — never by profile id or country.
"""

from __future__ import annotations

from dataclasses import dataclass

from profiles.schema import ProfileDocument


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    """Technical contracts the runtime must satisfy for one profile document."""

    device_capabilities: frozenset[str]
    network_capabilities: frozenset[str]
    data_capabilities: frozenset[str]
    # Propagation model id required on RfPort.model_id when RF is mandatory.
    rf_propagation_model: str | None

    @property
    def rf_required(self) -> bool:
        return self.rf_propagation_model is not None

    def as_sorted_tokens(self) -> tuple[str, ...]:
        """Stable capability tokens for provenance / diagnostics."""
        tokens: list[str] = []
        for cap in sorted(self.device_capabilities):
            tokens.append(f"device.{cap}")
        for cap in sorted(self.network_capabilities):
            tokens.append(f"network.{cap}")
        for cap in sorted(self.data_capabilities):
            tokens.append(f"data.{cap}")
        if self.rf_propagation_model is not None:
            tokens.append(f"rf.{self.rf_propagation_model}")
        return tuple(tokens)


def required_capabilities(profile: ProfileDocument) -> RuntimeRequirements:
    """Canonical, deterministic requirement derivation from a ProfileDocument."""
    device: frozenset[str] = frozenset()
    network: frozenset[str] = frozenset()
    if profile.requirements is not None:
        device = frozenset(profile.requirements.device_capabilities)
        network = frozenset(profile.requirements.network_capabilities)

    data: frozenset[str] = frozenset()
    if profile.data is not None:
        data = frozenset(profile.data.required_capabilities)

    rf_model: str | None = None
    if profile.rf is not None and profile.rf.required:
        if not profile.rf.propagation_model:
            raise ValueError("rf.required requires propagation_model")
        rf_model = profile.rf.propagation_model

    return RuntimeRequirements(
        device_capabilities=device,
        network_capabilities=network,
        data_capabilities=data,
        rf_propagation_model=rf_model,
    )
