"""Canonical profile API, shared errors, and root package exports."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import profiles
from profiles import errors as shared_errors
from profiles.errors import (
    ProfileError,
    ProfileNotFoundError,
    ProfilePathError,
    ProfileValidationError,
)
from profiles import (
    ProfileDocument,
    load_profile,
    load_profile_document,
    load_profile_document_with_provenance,
    load_profile_with_provenance,
    parse_profile_document,
    profile_context_from_document,
    profile_hash,
)
from profiles.trust import ProfileTrustTier, builtin_profiles_dir

_ROOT_INIT = Path(__file__).resolve().parents[2] / "profiles" / "__init__.py"


def test_shared_error_classes_from_errors_module() -> None:
    assert ProfileError is shared_errors.ProfileError
    assert ProfileNotFoundError is shared_errors.ProfileNotFoundError
    assert ProfileValidationError is shared_errors.ProfileValidationError
    assert ProfilePathError is shared_errors.ProfilePathError


def test_profile_document_loads() -> None:
    for profile_id in (
        "cbrs_winnforum",
        "br_anatel_slp_3700",
        "eu_elsa",
        "us_tvws_15_711",
    ):
        doc = load_profile(profile_id)
        assert isinstance(doc, ProfileDocument)
        assert doc.metadata.id == profile_id


def test_canonical_load_parse_hash_and_provenance() -> None:
    left = load_profile("cbrs_winnforum")
    right = load_profile("cbrs_winnforum")
    assert left == right
    assert profile_hash(left) == profile_hash(right)
    assert profile_context_from_document(left) == profile_context_from_document(right)

    payload = left.model_dump(mode="json", exclude_none=True)
    assert parse_profile_document(payload) == parse_profile_document(payload)

    path = builtin_profiles_dir() / "cbrs_winnforum.yaml"
    assert load_profile_document(path) == load_profile_document(path)
    with_prov = load_profile_with_provenance("cbrs_winnforum")
    again = load_profile_with_provenance("cbrs_winnforum")
    assert with_prov[0] == again[0]
    assert with_prov[1] == again[1]
    assert with_prov[1].trust_tier is ProfileTrustTier.BUILTIN
    assert with_prov[1].trust_tier.value == "builtin"
    assert with_prov[1].as_dict()["trust_tier"] == "builtin"

    path_prov = load_profile_document_with_provenance(path)
    assert path_prov[0].metadata.id == "cbrs_winnforum"
    assert path_prov[1].trust_tier is ProfileTrustTier.OPERATOR_EXPLICIT


def test_canonical_load_fail_closed_shared_errors() -> None:
    with pytest.raises(ProfileValidationError):
        load_profile("../etc/passwd")
    with pytest.raises(ProfileNotFoundError):
        load_profile("does_not_exist_profile_xyz")
    with pytest.raises(ProfileValidationError):
        parse_profile_document({"not": "a profile"})
    missing = Path("/tmp/spectrum_access_missing_profile_step1a.yaml")
    with pytest.raises(ProfileNotFoundError):
        load_profile_document(missing)


def test_root_load_profile_returns_canonical_document() -> None:
    doc = profiles.load_profile("cbrs_winnforum")
    assert isinstance(doc, profiles.ProfileDocument)
    assert doc.metadata.id == "cbrs_winnforum"
    assert doc.metadata.version == "2.0.0"
    assert profiles.primary_spectrum_range(doc).low_hz == 3_550_000_000


def test_root_load_profile_matches_v2_namespace() -> None:
    root_doc = profiles.load_profile("cbrs_winnforum")
    ns_doc = load_profile("cbrs_winnforum")
    assert root_doc.metadata.id == ns_doc.metadata.id
    assert root_doc.metadata.version == ns_doc.metadata.version
    assert profiles.profile_hash(root_doc) == profile_hash(ns_doc)


def test_root_exports_canonical_symbols() -> None:
    for name in (
        "ProfileDocument",
        "ProfileError",
        "ProfileValidationError",
        "DEFAULT_PROFILE_ID",
        "active_profile_id",
        "get_active_profile_document",
        "set_active_profile_document",
        "reload_active_profile_document",
        "primary_spectrum_range",
        "canonical_profile_json",
        "profile_hash",
        "parse_profile_document",
    ):
        assert hasattr(profiles, name), name
        assert name in profiles.__all__


def test_root_does_not_export_v1_or_historical_v2_symbols() -> None:
    banned = (
        "SpectrumProfile",
        "BandPlan",
        "ProtectionRule",
        "EntityParams",
        "get_active_profile",
        "set_active_profile",
        "reload_active_profile",
        "project_v1_to_v2_document",
        "DEFAULT_PROFILES_DIR",
        "get_profiles_dir",
        "set_profiles_dir",
        "clear_profile_cache",
        "load_profile_v2",
        "ProfileV2SpectrumDocument",
        "builtin_v2_profiles_dir",
        "profile_hash_v2",
        "diagnose_profile_v2",
    )
    for name in banned:
        assert not hasattr(profiles, name), name
        assert name not in profiles.__all__


def test_legacy_spectrum_profiles_namespace_is_not_importable() -> None:
    import importlib.util

    assert importlib.util.find_spec("spectrum_profiles") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("spectrum_profiles")


def test_legacy_v2_subnamespace_is_not_importable() -> None:
    import importlib.util

    assert importlib.util.find_spec("profiles.v2") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("profiles.v2")


def test_root_init_does_not_import_deleted_namespaces() -> None:
    tree = ast.parse(_ROOT_INIT.read_text(encoding="utf-8"))
    forbidden = {
        "spectrum_profiles",
        "profiles.loader",
        "profiles.v2",
        "profiles.v2.migrate",
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    assert not (found & forbidden)


def test_root_import_smoke_subprocess() -> None:
    script = (
        "import profiles as sp\n"
        "doc = sp.load_profile('cbrs_winnforum')\n"
        "assert isinstance(doc, sp.ProfileDocument)\n"
        "assert doc.metadata.id == 'cbrs_winnforum'\n"
        "assert sp.get_active_profile_document().metadata.id\n"
        "print('PASS')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert proc.returncode == 0, proc.stderr
    assert "PASS" in proc.stdout
