"""PostgreSQL-authoritative Phase D persistence modularization proofs."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import database
from models.migration_domains import (
    CBRS_HISTORICAL_REVISION,
    GENERIC_REVISION,
)
from models.persistence import (
    CBRS_WINNFORUM_ONLY_TABLES,
    GENERIC_TABLES,
    PersistencePlan,
    reference_persistence_plan,
)
from models.registry import REFERENCE_REQUIRED_TABLES, load_all_models
from services.migrations import apply_schema, current_revision, upgrade_to
from services.schema_fingerprint import schema_fingerprint


def _pg_url() -> str | None:
    return os.environ.get("SAS_TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    env = _pg_url()
    if env:
        yield env
        return
    candidate = "postgresql+psycopg2://sas:sas_test@127.0.0.1:55432/sas"
    try:
        eng = create_engine(candidate)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
    except Exception:
        pytest.skip(
            "PostgreSQL unavailable: set SAS_TEST_DATABASE_URL or start "
            "postgres on 127.0.0.1:55432 (sas/sas_test/sas)"
        )
        return
    yield candidate


@pytest.fixture
def isolated_pg_database(postgres_url: str, monkeypatch) -> Iterator[str]:
    """Create a throwaway database for virgin schema experiments."""
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    db_name = f"sas_phase_d_{uuid4().hex[:10]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = postgres_url.rsplit("/", 1)[0] + f"/{db_name}"
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin.dispose()


def test_pg_virgin_cbrs_schema(isolated_pg_database: str):
    load_all_models()
    eng = create_engine(isolated_pg_database)
    try:
        plan = reference_persistence_plan()
        apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
        present = set(inspect(eng).get_table_names())
        assert REFERENCE_REQUIRED_TABLES <= present
        assert current_revision(database_url=isolated_pg_database) == CBRS_HISTORICAL_REVISION
        # Smoke: app-level init against same URL.
        previous = str(database.engine.url)
        database.rebind_engine(isolated_pg_database)
        try:
            database.init_db(retries=2, delay_seconds=0.2, persistence_plan=plan)
        finally:
            database.rebind_engine(previous)
    finally:
        eng.dispose()


def test_pg_virgin_generic_excludes_cbrs(isolated_pg_database: str):
    load_all_models()
    eng = create_engine(isolated_pg_database)
    try:
        plan = PersistencePlan(
            contribution_ids=("generic",),
            table_names=GENERIC_TABLES,
        )
        apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
        present = set(inspect(eng).get_table_names())
        present.discard("alembic_version")
        assert GENERIC_TABLES <= present
        assert present & CBRS_WINNFORUM_ONLY_TABLES == set()
        assert current_revision(database_url=isolated_pg_database) == GENERIC_REVISION
        with eng.connect() as conn:
            row = conn.execute(text("SELECT to_regclass('public.cbsds')")).scalar()
            assert row is None
    finally:
        eng.dispose()


def test_pg_legacy_cbrs_upgrade_preserves_data(isolated_pg_database: str):
    load_all_models()
    upgrade_to(database_url=isolated_pg_database, revision=CBRS_HISTORICAL_REVISION)
    eng = create_engine(isolated_pg_database)
    Session = sessionmaker(bind=eng)
    session = Session()
    try:
        from models.models import Cbsd, Grant

        cbsd = Cbsd(
            cbsd_id="fcc/serial-pg-d",
            fcc_id="fcc",
            user_id="user",
            cbsd_serial_number="serial-pg-d",
            lifecycle_state="REGISTERED",
            registration_json="{}",
        )
        session.add(cbsd)
        session.flush()
        session.add(
            Grant(
                grant_id="g-pg-d-1",
                cbsd_pk=cbsd.id,
                cbsd_id=cbsd.cbsd_id,
                channel_type="GAA",
                low_frequency=3550_000_000,
                high_frequency=3560_000_000,
                grant_expire_time=datetime(2026, 9, 4, tzinfo=timezone.utc),
                heartbeat_interval=60,
                authorized=False,
                meas_report_requested=False,
                terminated=False,
                lifecycle_state="GRANTED",
                grant_json="{}",
            )
        )
        session.commit()
    finally:
        session.close()

    plan = reference_persistence_plan()
    apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
    assert current_revision(database_url=isolated_pg_database) == CBRS_HISTORICAL_REVISION
    session = Session()
    try:
        from models.models import Cbsd, Grant

        assert session.query(Cbsd).filter_by(cbsd_id="fcc/serial-pg-d").one()
        assert session.query(Grant).filter_by(grant_id="g-pg-d-1").one()
        fp = schema_fingerprint(eng)
        assert "cbsds" in fp and "grants" in fp
    finally:
        session.close()
        eng.dispose()
