"""Ensure the repository root is importable and provide shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

# Fast schema path for unit tests: create_all + alembic stamp (see migrations.py).
# Explicit Alembic upgrade/downgrade coverage lives in test_p8_002_migrations.py.
os.environ.setdefault("SAS_SCHEMA_VIA_CREATE_ALL", "1")

# Explicit test-mode auth for in-process TestClient (Phase B fail-closed).
# Certification/production security tests must monkeypatch SAS_EXECUTION_MODE
# and clear_settings_cache(); they must not rely on transport-shape inference.
# Preserve an explicitly set mode when SAS_PYTEST_PRESERVE_EXECUTION_MODE=1.
if os.environ.get("SAS_PYTEST_PRESERVE_EXECUTION_MODE") != "1":
    os.environ["SAS_EXECUTION_MODE"] = "test"

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Prefer the pinned WInnForum harness checkout when present so IAP/ESC coupling
# unit paths (and grant-time admission) can resolve reference_models locally.
_PINNED_HARNESS = (
    _ROOT
    / ".cache"
    / "winnforum-harness-928c3150adf7b31e53a96b695bf1fbdd3284ecb2"
    / "src"
    / "harness"
)
if _PINNED_HARNESS.is_dir():
    os.environ.setdefault("SAS_HARNESS_DIR", str(_PINNED_HARNESS))

# Import after sys.path fix.
import database  # noqa: E402
from database import init_db, rebind_engine  # noqa: E402
from tests.fixtures.factories import reset_factory_counter  # noqa: E402

# Snapshot at import time (normally local sqlite). Used when a prior test left the
# process engine on a dead/ephemeral PostgreSQL URL.
_RESTORE_URL = str(database.engine.url)
if _RESTORE_URL.startswith("postgresql"):
    _RESTORE_URL = f"sqlite:///{_ROOT / '.pytest_engine_restore.db'}"


def _safe_restore_engine(preferred: str) -> None:
    """Rebind to ``preferred`` when usable; otherwise fall back to sqlite restore."""
    target = preferred
    if target.startswith("postgresql"):
        target = _RESTORE_URL
    rebind_engine(target)
    init_db(retries=1, delay_seconds=0)


@pytest.fixture
def repo_root() -> Path:
    return _ROOT


@pytest.fixture
def frozen_time():
    """Deterministic clock for tests (freezegun; prefer UTC-aware assertions)."""
    freezegun = pytest.importorskip("freezegun")
    with freezegun.freeze_time("2026-08-05T15:00:00+00:00"):
        yield


@pytest.fixture(autouse=True)
def _deterministic_haat_provider() -> Iterator[None]:
    """Unit/integration default: flat terrain (norm HAAT = 0) without NED tiles.

    Certification / REG.7 must inject or resolve the real NED-backed provider
    via ``SAS_TERRAIN_DIR``; this fixture keeps local pytest independent of
    the multi-GB USGS dataset.
    """
    from services.terrain import DeterministicHaatProvider, reset_haat_provider, set_haat_provider

    set_haat_provider(DeterministicHaatProvider(default_norm_haat_m=0.0))
    try:
        yield
    finally:
        reset_haat_provider()


@pytest.fixture(autouse=True)
def _bind_reference_runtime_composition(request: pytest.FixtureRequest) -> Iterator[None]:
    """Bind packaged reference RuntimeComposition for unit/integration tests.

    Ensures IAP/BPR use composed RfPort rather than obsolete Settings selectors.
    Also attaches composition to ``main.app.state`` and process-local authority
    for TestClient / CPAS paths that do not invoke ``on_startup``.
    Tests may still pass explicit ``rf_port`` / ``path_loss_model`` overrides.

    Opt out with ``@pytest.mark.no_runtime_composition`` for lifecycle isolation.
    """
    if request.node.get_closest_marker("no_runtime_composition"):
        from runtime.bootstrap import _reset_process_runtime_composition_for_tests

        _reset_process_runtime_composition_for_tests()
        yield
        _reset_process_runtime_composition_for_tests()
        return

    from profiles import load_profile
    from runtime import PluginRegistry, compose_runtime, load_deployment
    from runtime.bootstrap import (
        _reset_process_runtime_composition_for_tests,
        initialize_process_runtime_composition,
    )
    from runtime.context import runtime_composition_scope
    from runtime.deployment import DEFAULT_DEPLOYMENT_PATH

    _reset_process_runtime_composition_for_tests()
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
    )
    initialize_process_runtime_composition(composition=composition)
    try:
        import main as main_mod

        previous = getattr(main_mod.app.state, "runtime_composition", None)
        main_mod.app.state.runtime_composition = composition
    except Exception:  # noqa: BLE001 — app may be unavailable in some contexts
        previous = None
        main_mod = None

    with runtime_composition_scope(composition):
        try:
            yield
        finally:
            if main_mod is not None:
                if previous is None:
                    if hasattr(main_mod.app.state, "runtime_composition"):
                        delattr(main_mod.app.state, "runtime_composition")
                else:
                    main_mod.app.state.runtime_composition = previous
            _reset_process_runtime_composition_for_tests()


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    """Isolated SQLite database rebound for a single test."""
    previous_url = str(database.engine.url)
    reset_factory_counter()
    db_path = tmp_path / "sas_test.db"
    url = f"sqlite:///{db_path}"
    rebind_engine(url)
    init_db(retries=1, delay_seconds=0)
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        _safe_restore_engine(previous_url)
