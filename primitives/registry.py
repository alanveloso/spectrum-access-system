"""In-process named mechanism catalog with open discovery.

Contracts are identity + axis + version. They do not embed regulatory callables
or YAML. Unknown ids fail closed. Access may be omitted (D10).

Built-in contracts are an open registry *seed*. External packages register
additional contracts via ``spectrum_access.mechanisms`` entry points without
editing this module. See ``primitives.discovery.MechanismDiscovery``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.discovery import MechanismDiscovery, MechanismProvenance


class MechanismAxis(StrEnum):
    SPECTRUM = "spectrum"
    ACCESS = "access"
    AUTHORIZATION = "authorization"
    GEOGRAPHY = "geography"
    POWER = "power"
    TEMPORAL = "temporal"
    PROTECTION = "protection"
    COORDINATION = "coordination"
    RF = "rf"


@dataclass(frozen=True, slots=True)
class MechanismContract:
    mechanism_id: str
    axis: MechanismAxis
    version: str
    required_capabilities: tuple[str, ...] = ()
    slot: str = ""

    def __post_init__(self) -> None:
        if not self.mechanism_id.strip():
            raise ValueError("mechanism_id is required")
        if not self.version.strip():
            raise ValueError("version is required")
        if self.slot not in {"", "rf_policy", "rf_model"}:
            raise ValueError("unsupported mechanism slot")


_BUILTIN: tuple[MechanismContract, ...] = (
    MechanismContract("frequency_ranges", MechanismAxis.SPECTRUM, "1.0.0"),
    MechanismContract("fixed_width_channelization", MechanismAxis.SPECTRUM, "1.0.0"),
    MechanismContract("duplex_mode", MechanismAxis.SPECTRUM, "1.0.0"),
    MechanismContract("max_assignment_bandwidth", MechanismAxis.SPECTRUM, "1.0.0"),
    MechanismContract("ordered_classes", MechanismAxis.ACCESS, "1.0.0"),
    MechanismContract("preemption", MechanismAxis.ACCESS, "1.0.0"),
    MechanismContract("forbidden_device_roles", MechanismAxis.ACCESS, "1.0.0"),
    MechanismContract("protection_entitlement", MechanismAxis.PROTECTION, "1.0.0"),
    MechanismContract("channel_exclusion", MechanismAxis.PROTECTION, "1.0.0"),
    MechanismContract("distance_exclusion", MechanismAxis.PROTECTION, "1.0.0"),
    MechanismContract("single_link_threshold", MechanismAxis.PROTECTION, "1.0.0"),
    MechanismContract("aggregate_linear_power", MechanismAxis.PROTECTION, "1.0.0"),
    MechanismContract("dynamic_lease", MechanismAxis.AUTHORIZATION, "1.0.0"),
    MechanismContract("fixed_window", MechanismAxis.AUTHORIZATION, "1.0.0"),
    MechanismContract("static_authorization", MechanismAxis.AUTHORIZATION, "1.0.0"),
    MechanismContract("availability_constraint", MechanismAxis.AUTHORIZATION, "1.0.0"),
    MechanismContract("point_radius", MechanismAxis.GEOGRAPHY, "1.0.0"),
    MechanismContract("authorized_area", MechanismAxis.GEOGRAPHY, "1.0.0"),
    MechanismContract("exclusion_zone", MechanismAxis.GEOGRAPHY, "1.0.0"),
    MechanismContract("max_power", MechanismAxis.POWER, "1.0.0"),
    MechanismContract("rule_table", MechanismAxis.POWER, "1.0.0"),
    MechanismContract("antenna_height_limit", MechanismAxis.POWER, "1.0.0"),
    MechanismContract("periodic", MechanismAxis.TEMPORAL, "1.0.0"),
    MechanismContract("snapshot_evaluate_apply", MechanismAxis.COORDINATION, "1.0.0"),
    MechanismContract(
        "path_loss_plus_aggregate", MechanismAxis.RF, "1.0.0", slot="rf_policy"
    ),
    MechanismContract("path_loss", MechanismAxis.RF, "1.0.0", slot="rf_model"),
)


class MechanismRegistry:
    def __init__(self, contracts: tuple[MechanismContract, ...] = ()) -> None:
        self._by_id: dict[str, MechanismContract] = {}
        for contract in contracts:
            self.register(contract)

    def register(self, contract: MechanismContract) -> None:
        if contract.mechanism_id in self._by_id:
            raise ValueError(f"duplicate mechanism {contract.mechanism_id!r}")
        self._by_id[contract.mechanism_id] = contract

    def get(self, mechanism_id: str) -> MechanismContract:
        try:
            return self._by_id[mechanism_id]
        except KeyError:
            raise ValueError(f"unknown mechanism {mechanism_id!r}") from None

    def require(self, mechanism_ids: tuple[str, ...]) -> tuple[MechanismContract, ...]:
        return tuple(self.get(item) for item in mechanism_ids)

    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def on_axis(self, axis: MechanismAxis, mechanism_id: str) -> MechanismContract:
        contract = self.get(mechanism_id)
        if contract.axis != axis:
            raise ValueError(
                f"mechanism {mechanism_id!r} is {contract.axis}, expected {axis}"
            )
        return contract


def builtin_mechanism_contracts() -> tuple[MechanismContract, ...]:
    """Immutable built-in seed contracts (open catalog seed, not closed-world)."""
    return _BUILTIN


def builtin_mechanism_registry() -> MechanismRegistry:
    """Fresh in-memory catalog of built-in seed only; not a process-wide singleton.

    Prefer :func:`discovered_mechanism_registry` for profile/runtime availability.
    """
    return MechanismRegistry(_BUILTIN)


def discovered_mechanism_registry(
    discovery: MechanismDiscovery | None = None,
) -> MechanismRegistry:
    """Compose built-in seed + entry-point mechanism contracts.

    Discovery runs at this composition boundary (not per request). Duplicate
    semantic ``mechanism_id`` values fail closed.
    """
    from primitives.discovery import MechanismDiscovery as _Discovery

    active = discovery if discovery is not None else _Discovery()
    registry = MechanismRegistry(builtin_mechanism_contracts())
    for contract in active.load_all():
        registry.register(contract)
    return registry


def mechanism_catalog_provenance(
    discovery: MechanismDiscovery | None = None,
) -> tuple[MechanismProvenance, ...]:
    """Builtin + discovered provenance for diagnostics."""
    from primitives.discovery import MechanismDiscovery as _Discovery
    from primitives.discovery import MechanismProvenance

    active = discovery if discovery is not None else _Discovery()
    builtin = tuple(
        MechanismProvenance(mechanism_id=c.mechanism_id, origin="builtin")
        for c in builtin_mechanism_contracts()
    )
    return builtin + active.provenance()


def select_optional_access(
    registry: MechanismRegistry, mechanism_id: str | None
) -> MechanismContract | None:
    if mechanism_id is None:
        return None
    return registry.on_axis(MechanismAxis.ACCESS, mechanism_id)
