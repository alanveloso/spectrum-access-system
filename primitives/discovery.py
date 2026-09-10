"""Mechanism catalog discovery via entry points.

Built-in contracts seed the catalog; third-party packages publish
``spectrum_access.mechanisms`` without editing ``primitives/registry.py``.
This is semantic registration, not regulatory execution dispatch.

Primitives must not import adapters; plugin-name validation is duplicated
here with the same grammar as ``adapters.plugin_names``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Literal

from primitives.registry import MechanismContract

GROUP_MECHANISMS = "spectrum_access.mechanisms"

MechanismOriginKind = Literal["builtin", "plugin"]

_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_plugin_name(name: str) -> str:
    if not isinstance(name, str) or not name or not name.strip():
        raise ValueError("plugin name is required")
    if name != name.strip():
        raise ValueError(f"invalid plugin name {name!r}")
    if "\x00" in name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid plugin name {name!r}")
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid plugin name {name!r}")
    return name


def _default_list_entry_points(group: str) -> Sequence[Any]:
    return tuple(entry_points(group=group))


@dataclass(frozen=True, slots=True)
class MechanismProvenance:
    """Where a semantic mechanism contract was registered."""

    mechanism_id: str
    origin: MechanismOriginKind
    plugin_id: str = ""


def _materialize(factory: Any) -> Any:
    """Resolve entry points, classes, or zero-arg factory functions to instances."""
    target = factory
    for _ in range(3):
        if isinstance(target, type):
            target = target()
            continue
        if callable(target) and not isinstance(target, MechanismContract):
            target = target()
            continue
        break
    return target


def _validate_contract(plugin_id: str, plugin: Any) -> MechanismContract:
    if not isinstance(plugin, MechanismContract):
        raise ValueError(
            f"mechanism plugin {plugin_id!r} must materialize to MechanismContract"
        )
    if not plugin.mechanism_id.strip():
        raise ValueError(f"mechanism plugin {plugin_id!r} has empty mechanism_id")
    try:
        _validate_plugin_name(plugin.mechanism_id)
    except ValueError as exc:
        raise ValueError(
            f"mechanism plugin {plugin_id!r} has invalid mechanism_id "
            f"{plugin.mechanism_id!r}"
        ) from exc
    return plugin


@dataclass(frozen=True, slots=True)
class MechanismDiscovery:
    """Per-call discovery of external mechanism contracts. Not a process singleton."""

    overlays: Mapping[str, Any] = field(default_factory=dict)
    list_entry_points: Callable[[str], Sequence[Any]] = _default_list_entry_points

    def names(self) -> frozenset[str]:
        return frozenset(self._index())

    def load(self, name: str) -> MechanismContract:
        _validate_plugin_name(name)
        index = self._index()
        if name not in index:
            raise ValueError(f"unknown mechanism plugin {name!r}")
        try:
            plugin = _materialize(index[name])
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"mechanism plugin {name!r} failed to load") from exc
        return _validate_contract(name, plugin)

    def load_all(self) -> tuple[MechanismContract, ...]:
        """Load every discovered contract; fail closed on duplicates/malformed."""
        contracts: list[MechanismContract] = []
        seen_ids: set[str] = set()
        for name in sorted(self._index()):
            contract = self.load(name)
            if contract.mechanism_id in seen_ids:
                raise ValueError(
                    f"duplicate mechanism identity {contract.mechanism_id!r} "
                    f"among discovered plugins"
                )
            seen_ids.add(contract.mechanism_id)
            contracts.append(contract)
        return tuple(contracts)

    def provenance(self) -> tuple[MechanismProvenance, ...]:
        return tuple(
            MechanismProvenance(
                mechanism_id=contract.mechanism_id,
                origin="plugin",
                plugin_id=name,
            )
            for name in sorted(self._index())
            for contract in (self.load(name),)
        )

    def _index(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for ep in self.list_entry_points(GROUP_MECHANISMS):
            ep_name = getattr(ep, "name", None)
            if not isinstance(ep_name, str) or not ep_name.strip():
                raise ValueError("entry point in mechanisms is missing a name")
            _validate_plugin_name(ep_name)
            if ep_name in found:
                raise ValueError(f"duplicate plugin name {ep_name!r} in mechanisms")
            found[ep_name] = ep.load
        for ov_name, factory in self.overlays.items():
            _validate_plugin_name(ov_name)
            if ov_name in found:
                raise ValueError(f"duplicate plugin name {ov_name!r} in mechanisms")
            found[ov_name] = factory
        return found
