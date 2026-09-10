"""Shared plugin name validation. Fail closed on path-like names."""

from __future__ import annotations

import re

_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_plugin_name(name: str) -> str:
    if not isinstance(name, str) or not name or not name.strip():
        raise ValueError("plugin name is required")
    if name != name.strip():
        raise ValueError(f"invalid plugin name {name!r}")
    if "\x00" in name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid plugin name {name!r}")
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid plugin name {name!r}")
    return name
