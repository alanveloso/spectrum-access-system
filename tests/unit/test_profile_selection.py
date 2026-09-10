"""Shared profile-id selection and canonical active-document API."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from config import clear_settings_cache
from profiles.selection import (
    DEFAULT_PROFILE_ID,
    active_profile_id,
    clear_profile_override,
    set_profile_override,
)
from profiles import (
    ProfileDocument,
    get_active_profile_document,
    set_active_profile_document,
)
from profiles.context import active_profile_id as canonical_active_profile_id


_SELECTION_PATH = Path(__file__).resolve().parents[2] / "profiles" / "selection.py"


@pytest.fixture(autouse=True)
def _reset_selection(monkeypatch: pytest.MonkeyPatch):
    clear_profile_override()
    monkeypatch.delenv("SAS_PROFILE", raising=False)
    clear_settings_cache()
    yield
    clear_profile_override()
    clear_settings_cache()


def test_default_active_id_is_cbrs() -> None:
    assert DEFAULT_PROFILE_ID == "cbrs_winnforum"
    assert active_profile_id() == "cbrs_winnforum"


def test_sas_profile_env_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAS_PROFILE", "eu_elsa")
    clear_settings_cache()
    assert active_profile_id() == "eu_elsa"


def test_blank_sas_profile_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAS_PROFILE", "   ")
    clear_settings_cache()
    assert active_profile_id() == DEFAULT_PROFILE_ID


def test_override_and_clear() -> None:
    set_profile_override("br_anatel_slp_3700")
    assert active_profile_id() == "br_anatel_slp_3700"
    clear_profile_override()
    assert active_profile_id() == DEFAULT_PROFILE_ID


def test_selection_module_has_no_loader_or_schema_deps() -> None:
    tree = ast.parse(_SELECTION_PATH.read_text(encoding="utf-8"))
    forbidden = {
        "spectrum_profiles",
        "profiles.loader",
        "profiles.schema",
        "profiles.context",
        "profiles.v2",
        "SpectrumProfile",
        "ProfileDocument",
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            for alias in node.names:
                found.add(alias.name)
    assert not (found & forbidden)


def test_canonical_active_document_shares_selection() -> None:
    set_active_profile_document("cbrs_winnforum")
    assert get_active_profile_document().metadata.id == "cbrs_winnforum"
    assert canonical_active_profile_id() == "cbrs_winnforum"
    clear_profile_override()
    assert active_profile_id() == DEFAULT_PROFILE_ID
    assert canonical_active_profile_id() == DEFAULT_PROFILE_ID


@pytest.mark.parametrize(
    "profile_id",
    (
        "cbrs_winnforum",
        "br_anatel_slp_3700",
        "eu_elsa",
        "us_tvws_15_711",
    ),
)
def test_canonical_set_active_profile_document(profile_id: str) -> None:
    doc = set_active_profile_document(profile_id)
    assert isinstance(doc, ProfileDocument)
    assert doc.metadata.id == profile_id
    assert get_active_profile_document().metadata.id == profile_id
    assert active_profile_id() == profile_id
