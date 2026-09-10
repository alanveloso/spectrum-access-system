"""ANATEL SLP 3700–3800 requirements matrix integrity."""

from __future__ import annotations

from pathlib import Path

import yaml

from primitives.registry import builtin_mechanism_registry
from providers.contract import DATA_CAPABILITIES

_REPO = Path(__file__).resolve().parents[2]
_MATRIX_YAML = (
    _REPO / "compliance" / "anatel" / "slp_3700_3800_requirements_matrix.yaml"
)

_REQUIRED_REQ_KEYS = {
    "id",
    "source",
    "section",
    "summary",
    "profile_field",
    "mechanisms",
    "code_plugin",
    "test",
    "status",
}


def _load_matrix() -> dict:
    payload = yaml.safe_load(_MATRIX_YAML.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_matrix_yaml_exists() -> None:
    assert _MATRIX_YAML.is_file()
    doc = _load_matrix()
    assert doc["matrix_id"] == "anatel_slp_3700_requirements"
    assert doc["primary_instrument"]["id"] == "ATO_915_2024"
    assert doc["profile_id_target"] == "br_anatel_slp_3700"


def test_matrix_document_shape() -> None:
    doc = _load_matrix()
    assert doc["version"] == 1
    assert doc["matrix_id"] == "anatel_slp_3700_requirements"
    assert doc["profile_id_target"] == "br_anatel_slp_3700"
    assert doc["primary_instrument"]["id"] == "ATO_915_2024"
    assert "1920-ato-915" in doc["primary_instrument"]["url"]
    assert isinstance(doc["requirements"], list)
    assert len(doc["requirements"]) >= 20


def test_requirement_rows_complete_and_unique() -> None:
    doc = _load_matrix()
    source_ids = {item["id"] for item in doc["sources"]}
    allowed = set(doc["allowed_statuses"])
    seen: set[str] = set()
    for row in doc["requirements"]:
        assert _REQUIRED_REQ_KEYS <= set(row)
        rid = row["id"]
        assert rid.startswith("BR-SLP-3700-")
        assert rid not in seen
        seen.add(rid)
        assert row["source"] in source_ids
        assert row["status"] in allowed
        assert isinstance(row["section"], str) and row["section"].strip()
        assert isinstance(row["summary"], str) and len(row["summary"]) > 10
        assert isinstance(row["mechanisms"], list)


def test_mechanisms_are_builtin_catalog_ids() -> None:
    catalog = builtin_mechanism_registry().ids()
    doc = _load_matrix()
    for row in doc["requirements"]:
        for mid in row["mechanisms"]:
            assert mid in catalog, f"{row['id']}: unknown mechanism {mid!r}"
        for mid in row.get("forbid_mechanisms_as_keepalive") or ():
            assert mid in catalog


def test_data_capabilities_are_canonical() -> None:
    doc = _load_matrix()
    for row in doc["requirements"]:
        for cap in row.get("data_capabilities") or ():
            assert cap in DATA_CAPABILITIES, f"{row['id']}: bad capability {cap!r}"


def test_planned_yaml_rows_map_to_mechanisms_or_explicit_omit() -> None:
    """PLANNED_YAML must cite catalog mechanisms or an explicit omit/forbid rule."""
    doc = _load_matrix()
    for row in doc["requirements"]:
        if row["status"] != "PLANNED_YAML":
            continue
        has_mech = bool(row["mechanisms"])
        has_omit = bool(row.get("omit_profile_sections"))
        has_forbid = bool(row.get("forbid_mechanisms_as_keepalive"))
        assert has_mech or has_omit or has_forbid, row["id"]


def test_architecture_hypothesis_updates_present() -> None:
    doc = _load_matrix()
    updates = doc.get("architecture_hypothesis_updates") or []
    assert any(u.get("by") == "BR-SLP-3700-003" for u in updates)
    assert any(u.get("verdict") == "superseded" for u in updates)
