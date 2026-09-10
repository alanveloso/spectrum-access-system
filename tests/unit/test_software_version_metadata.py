"""Software version metadata consistency (not WInnForum API versions)."""

from __future__ import annotations

import tomllib

from tests.support.repo import REPO_ROOT as ROOT


def test_fastapi_app_version_matches_pyproject_software_version():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = data["project"]["version"]
    assert expected == "1.0.0"
    # Import without binding runtime side effects beyond module load.
    import main as main_mod

    assert main_mod.SAS_SOFTWARE_VERSION == expected
    assert main_mod.app.version == expected
