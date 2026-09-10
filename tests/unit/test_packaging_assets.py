"""Packaging: editable install exposes protection_data + spectrum profile assets."""

from __future__ import annotations

from importlib import resources

import protection_data
import profiles


def test_profiles_package_ships_canonical_yaml() -> None:
    root = resources.files(profiles)
    definitions = root.joinpath("definitions")
    assert not root.joinpath("profiles").joinpath("cbrs_winnforum.yaml").is_file()
    for name in (
        "cbrs_winnforum.yaml",
        "br_anatel_slp_3700.yaml",
        "eu_elsa.yaml",
        "us_tvws_15_711.yaml",
    ):
        assert definitions.joinpath(name).is_file(), name


def test_protection_data_package_ships_default_manifest() -> None:
    root = resources.files(protection_data)
    manifests = root.joinpath("manifests")
    assert manifests.joinpath("cbrs_winnforum_protection.yaml").is_file()
