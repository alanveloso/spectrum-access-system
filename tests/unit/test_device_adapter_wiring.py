"""Architecture guards for composed device adapter wiring."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_MIGRATED_GEO_PATHS = (
    "services/grant_service.py:_cbsd_location",
    "services/registration_service.py:process_registration",
)


def test_grant_location_helper_uses_adapter_semantics():
    source = (REPO / "services/grant_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cbsd_location":
            body = ast.get_source_segment(source, node) or ""
            assert "installationParam" not in body
            assert "cbsd_geo_coordinates" in body
            return
    pytest.fail("_cbsd_location not found")


def test_registration_geo_path_uses_consumer_view():
    source = (REPO / "services/registration_service.py").read_text(encoding="utf-8")
    assert "consumer_view_from_payload" in source
    assert "geo_coordinates(consumer)" in source
    # Quiet-zone gate must not read installationParam latitude directly anymore.
    quiet_idx = source.index("registration_blocked_by_quiet_zone")
    snippet = source[quiet_idx : quiet_idx + 400]
    assert "installation.get(\"latitude\")" not in snippet
    assert 'installation["latitude"]' not in snippet
