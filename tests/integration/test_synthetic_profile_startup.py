"""Synthetic runtime startup test.

Composition alone is not enough: the application startup path must also complete
for a profile that requests no CBRS/WInnForum capability, and must not make
CBRS-only subsystems mandatory.

Only the profile/deployment *sources* are substituted. The startup body itself
runs unmodified, so anything unconditional in it will fail this test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import database
import profiles as profiles_pkg
import protection_data.loader as protection_loader
import runtime as runtime_pkg
from profiles import load_profile
from runtime import load_deployment
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH
from tests.fixtures.synthetic_profile import (
    SYNTHETIC_PROFILE_ID,
    synthetic_deployment,
    synthetic_profile,
)


@pytest.fixture
def startup_database(tmp_path: Path) -> Iterator[None]:
    """Isolated sqlite so ``on_startup`` can run ``init_db`` for real."""
    previous = str(database.engine.url)
    database.rebind_engine(f"sqlite:///{tmp_path / 'synthetic_startup.db'}")
    try:
        yield
    finally:
        database.rebind_engine(previous)
        database.init_db(retries=1, delay_seconds=0)


class _ProtectionDataSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, bundle_id: str, **kwargs: object) -> None:
        self.calls.append(bundle_id)


@pytest.fixture
def protection_data_spy(monkeypatch: pytest.MonkeyPatch) -> _ProtectionDataSpy:
    spy = _ProtectionDataSpy()
    monkeypatch.setattr(protection_loader, "assert_protection_data_ready", spy)
    monkeypatch.setattr("providers.protection_bundle.assert_protection_data_ready", spy)
    return spy


def _run_startup(
    monkeypatch: pytest.MonkeyPatch, *, profile, deployment
):
    import main as main_mod
    from runtime.bootstrap import _reset_process_runtime_composition_for_tests

    # Process-local authority is initialize-once; tests that substitute Profile
    # must clear the autouse reference composition before on_startup.
    _reset_process_runtime_composition_for_tests()
    monkeypatch.setattr(profiles_pkg, "get_active_profile_document", lambda: profile)
    monkeypatch.setattr(
        runtime_pkg, "load_deployment_for_startup", lambda **_kwargs: deployment
    )
    main_mod.on_startup()
    return main_mod.app.state.runtime_composition


def test_synthetic_profile_startup_completes(
    monkeypatch: pytest.MonkeyPatch,
    startup_database: None,
    protection_data_spy: _ProtectionDataSpy,
):
    composition = _run_startup(
        monkeypatch,
        profile=synthetic_profile(),
        deployment=synthetic_deployment(),
    )
    assert composition.provenance.profile_id == SYNTHETIC_PROFILE_ID
    assert composition.network_adapter is not None


def test_synthetic_startup_requires_no_cbrs_subsystem(
    monkeypatch: pytest.MonkeyPatch,
    startup_database: None,
    protection_data_spy: _ProtectionDataSpy,
):
    composition = _run_startup(
        monkeypatch,
        profile=synthetic_profile(),
        deployment=synthetic_deployment(),
    )
    assert composition.device_adapter is None
    assert composition.protocol_adapter is None
    assert composition.rf is None
    assert composition.providers == ()
    # No CBRS/WInnForum protection bundle was made mandatory.
    assert protection_data_spy.calls == []


def test_cbrs_startup_still_requires_protection_bundle(
    monkeypatch: pytest.MonkeyPatch,
    startup_database: None,
    protection_data_spy: _ProtectionDataSpy,
):
    """Control: the dataset gate is capability-driven, not removed."""
    composition = _run_startup(
        monkeypatch,
        profile=load_profile("cbrs_winnforum"),
        deployment=load_deployment(DEFAULT_DEPLOYMENT_PATH),
    )
    assert composition.requirements.data_capabilities
    assert "cbrs_winnforum_protection" in protection_data_spy.calls
