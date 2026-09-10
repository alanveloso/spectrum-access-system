"""loader / plugin / profile trust hardening."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from adapters.device import MappingDeviceAdapter
from adapters.discovery import GROUP_DEVICE_ADAPTERS, GROUP_MECHANISMS, AdapterDiscovery
from adapters.plugin_names import validate_plugin_name
from profiles.errors import ProfilePathError, ProfileValidationError
from profiles.doctor import run_profile_doctor
from profiles.parse import (
    load_profile,
    load_profile_document,
    load_profile_with_provenance,
    parse_profile_document,
)
from profiles.trust import (
    ProfileTrustTier,
    assert_path_within,
    builtin_profiles_dir,
    validate_profile_id,
)


def test_profile_id_path_like_fail_closed() -> None:
    for bad in ("../etc/passwd", "foo/bar", r"foo\bar", "..", ".hidden", "BadId", "a..b"):
        with pytest.raises((ProfilePathError, ProfileValidationError)):
            validate_profile_id(bad)
        with pytest.raises(ProfileValidationError):
            load_profile(bad)


def test_builtin_load_rejects_metadata_id_mismatch() -> None:
    root = builtin_profiles_dir()
    src = root / "cbrs_winnforum.yaml"
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    raw["metadata"]["id"] = "not_cbrs_winnforum"
    from profiles.trust import assert_metadata_id_matches

    parsed = parse_profile_document(raw)
    with pytest.raises(ProfileValidationError, match="mismatch"):
        assert_metadata_id_matches(parsed, "cbrs_winnforum")


def test_builtin_provenance_and_path_allowlist() -> None:
    parsed, prov = load_profile_with_provenance("eu_elsa")
    assert parsed.metadata.id == "eu_elsa"
    assert ProfileTrustTier.BUILTIN.name == "BUILTIN"
    assert ProfileTrustTier.BUILTIN.value == "builtin"
    assert not hasattr(ProfileTrustTier, "BUILTIN_V2")
    assert prov.trust_tier is ProfileTrustTier.BUILTIN
    assert prov.as_dict()["trust_tier"] == "builtin"
    assert prov.profile_hash
    assert_path_within(Path(prov.source_path), builtin_profiles_dir())


def test_operator_path_rejects_non_yaml(tmp_path: Path) -> None:
    junk = tmp_path / "profile.json"
    junk.write_text("{}", encoding="utf-8")
    with pytest.raises(ProfileValidationError, match="unsupported profile format"):
        load_profile_document(junk)


def test_plugin_name_and_mechanisms_group_fail_closed() -> None:
    with pytest.raises(ValueError, match="plugin name"):
        validate_plugin_name("../evil")
    with pytest.raises(ValueError, match="plugin name"):
        validate_plugin_name("Bad_Name")
    discovery = AdapterDiscovery(list_entry_points=lambda _g: ())
    with pytest.raises(ValueError, match="MechanismDiscovery"):
        discovery.names(GROUP_MECHANISMS)
    with pytest.raises(ValueError, match="invalid plugin name"):
        AdapterDiscovery(
            overlays={GROUP_DEVICE_ADAPTERS: {"bad/name": MappingDeviceAdapter}},
            list_entry_points=lambda _g: (),
        ).names(GROUP_DEVICE_ADAPTERS)


def test_doctor_emits_trust_provenance() -> None:
    report = run_profile_doctor(profile_id="br_anatel_slp_3700", check_plugins=False)
    assert report.ok
    trust = [f for f in report.findings if f.section == "trust"]
    assert trust
    assert any("trust_tier=builtin" in f.detail for f in trust)
    assert any("hash=" in f.detail for f in trust)


def test_reference_profiles_still_load() -> None:
    for profile_id in (
        "cbrs_winnforum",
        "br_anatel_slp_3700",
        "eu_elsa",
        "us_tvws_15_711",
    ):
        parsed = load_profile(profile_id)
        assert parsed.metadata.id == profile_id
