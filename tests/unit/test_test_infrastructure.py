"""factories, isolated DB, frozen time, and suite layout."""

from __future__ import annotations

from datetime import datetime, timezone

import database
from database import rebind_engine
from models.models import Cbsd, Grant
from tests.fixtures.factories import (
    make_cbsd,
    make_dpa,
    make_esc_sensor,
    make_fss,
    make_grant,
    make_pal,
    make_peer_sas,
    make_ppa_zone,
)
from tests.support.repo import REPO_ROOT


def test_suite_layout_directories_exist():
    for name in (
        "unit",
        "integration",
        "contract",
        "security",
        "regression",
        "fixtures",
        "support",
    ):
        assert (REPO_ROOT / "tests" / name).is_dir()


def test_pytest_xdist_not_enabled_by_default():
    """xdist stays off until process-global engine/state is removed (plan )."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional_dev = data["project"]["optional-dependencies"]["dev"]
    assert all("xdist" not in dep for dep in optional_dev)
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = str(ini.get("addopts", ""))
    assert "-n" not in addopts.split()
    assert "xdist" not in addopts


def test_factories_persist_entities(db_session):
    cbsd = make_cbsd(db_session)
    grant = make_grant(db_session, cbsd)
    pal = make_pal(db_session)
    ppa = make_ppa_zone(db_session)
    dpa = make_dpa(db_session, active=True)
    fss = make_fss(db_session)
    esc = make_esc_sensor(db_session)
    peer = make_peer_sas(db_session)

    assert db_session.get(Cbsd, cbsd.id) is not None
    assert db_session.get(Grant, grant.id) is not None
    assert pal.pal_id
    assert ppa.kind == "zone"
    assert dpa.kind == "dpa_active"
    assert fss.kind == "fss"
    assert esc.record_id
    assert peer.certificate_hash
    # Factories must not embed official harness fixture identifiers.
    assert "cbsd_1" not in cbsd.cbsd_id.lower()
    assert "sas-cert" not in (peer.certificate_hash or "").lower()


def test_db_session_isolation_between_tests_a(db_session):
    make_cbsd(db_session, cbsd_id="isolation-marker-a")
    assert db_session.query(Cbsd).filter_by(cbsd_id="isolation-marker-a").count() == 1


def test_db_session_isolation_between_tests_b(db_session):
    assert db_session.query(Cbsd).filter_by(cbsd_id="isolation-marker-a").count() == 0


def test_frozen_time_is_deterministic(frozen_time):
    now = datetime.now(timezone.utc)
    assert now.year == 2026
    assert now.month == 8
    assert now.day == 5
    assert now.hour == 15


def test_make_grant_flushes_uncommitted_cbsd(db_session):
    cbsd = make_cbsd(db_session, commit=False)
    assert cbsd.id is None
    grant = make_grant(db_session, cbsd, commit=True)
    assert cbsd.id is not None
    assert grant.cbsd_pk == cbsd.id


def test_rebind_engine_disposes_and_replaces_module_engine(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    previous = database.engine
    dispose = MagicMock(wraps=previous.dispose)
    monkeypatch.setattr(previous, "dispose", dispose)
    rebind_engine(f"sqlite:///{tmp_path / 'rebind.db'}")
    dispose.assert_called_once()
    assert database.engine is not previous
    with database.engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    # Restore process default URL so later tests keep using the configured DB.
    rebind_engine(str(previous.url))


def test_coverage_config_present():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.coverage.run]" in text
    assert "pytest-cov" in text or "coverage" in (
        REPO_ROOT / "requirements-dev.txt"
    ).read_text(encoding="utf-8")
