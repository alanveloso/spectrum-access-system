"""RF model discovery. Not a process singleton."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from adapters.discovery import GROUP_RF_MODELS
from adapters.plugin_names import validate_plugin_name
from rf.port import RF_API_VERSION, RF_MODEL_PATH_LOSS, RfPort


def _default_list_entry_points(group: str) -> Sequence[Any]:
    return tuple(entry_points(group=group))


@dataclass(frozen=True, slots=True)
class RfModelDiscovery:
    overlays: Mapping[str, Any] = field(default_factory=dict)
    list_entry_points: Callable[[str], Sequence[Any]] = _default_list_entry_points

    def names(self) -> frozenset[str]:
        return frozenset(self._index())

    def load(self, name: str) -> RfPort:
        validate_plugin_name(name)
        index = self._index()
        if name not in index:
            raise ValueError(f"unknown RF model {name!r}")
        try:
            plugin = _materialize(index[name])
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"RF model {name!r} failed to load") from exc
        if not isinstance(plugin, RfPort):
            raise ValueError(f"{name!r} is not an RF port")
        if plugin.api_version != RF_API_VERSION:
            raise ValueError(
                f"{name!r} api_version {plugin.api_version!r} incompatible with "
                f"{RF_API_VERSION!r}"
            )
        if plugin.model_id != RF_MODEL_PATH_LOSS:
            raise ValueError(f"{name!r} model_id must be {RF_MODEL_PATH_LOSS!r}")
        return plugin

    def _index(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for ep in self.list_entry_points(GROUP_RF_MODELS):
            ep_name = getattr(ep, "name", None)
            if not isinstance(ep_name, str) or not ep_name.strip():
                raise ValueError("entry point in rf_models is missing a name")
            validate_plugin_name(ep_name)
            if ep_name in found:
                raise ValueError(f"duplicate plugin name {ep_name!r}")
            found[ep_name] = ep.load
        for ov_name, factory in self.overlays.items():
            validate_plugin_name(ov_name)
            if ov_name in found:
                raise ValueError(f"duplicate plugin name {ov_name!r}")
            found[ov_name] = factory
        return found


def _materialize(factory: Any) -> Any:
    """Resolve entry points, classes, or zero-arg factory functions to instances."""
    target = factory
    for _ in range(3):
        if isinstance(target, type):
            target = target()
            continue
        if callable(target) and not hasattr(target, "api_version"):
            target = target()
            continue
        break
    return target
