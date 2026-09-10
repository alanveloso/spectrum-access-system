"""Alembic migrations, UTC datetimes, backup/restore."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from models.models import FccIdRecord
from models.registry import REQUIRED_TABLES
from services.clock import ensure_utc
from services.db_backup import backup_database, restore_database
from services.migrations import (
    HEAD_REVISION,
    current_revision,
    downgrade_base,
    upgrade_head,
)


def test_alembic_upgrade_downgrade_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)
    db_path = tmp_path / "mig.db"
    url = f"sqlite:///{db_path}"
    upgrade_head(database_url=url)
    assert current_revision(database_url=url) == HEAD_REVISION

    engine = create_engine(url)
    try:
        present = set(inspect(engine).get_table_names())
        assert REQUIRED_TABLES <= present
        assert "alembic_version" in present
        # Unique / partial constraints from initial migration.
        fad_indexes = {idx["name"] for idx in inspect(engine).get_indexes("fad_dumps")}
        assert "uq_fad_dumps_one_published" in fad_indexes
        grant_cols = {c["name"]: c for c in inspect(engine).get_columns("grants")}
        assert grant_cols["grant_expire_time"]["type"].timezone is True or str(
            grant_cols["grant_expire_time"]["type"]
        ).upper().startswith("DATETIME")
    finally:
        engine.dispose()

    downgrade_base(database_url=url)
    assert current_revision(database_url=url) is None
    engine = create_engine(url)
    try:
        remaining = set(inspect(engine).get_table_names())
        # alembic_version may remain empty or absent after downgrade to base.
        assert not (REQUIRED_TABLES & remaining)
    finally:
        engine.dispose()

    upgrade_head(database_url=url)
    assert current_revision(database_url=url) == HEAD_REVISION


def test_utc_datetime_roundtrip_aware(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)
    db_path = tmp_path / "utc.db"
    url = f"sqlite:///{db_path}"
    upgrade_head(database_url=url)
    engine = create_engine(url)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        from models.models import Cbsd, Grant

        cbsd = Cbsd(
            cbsd_id="fcc/serial-utc",
            fcc_id="fcc",
            user_id="user",
            cbsd_serial_number="serial-utc",
            lifecycle_state="REGISTERED",
            registration_json="{}",
        )
        session.add(cbsd)
        session.flush()
        aware = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
        grant = Grant(
            grant_id="g-utc-1",
            cbsd_pk=cbsd.id,
            cbsd_id=cbsd.cbsd_id,
            channel_type="GAA",
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            grant_expire_time=aware,
            heartbeat_interval=60,
            authorized=False,
            meas_report_requested=False,
            terminated=False,
            lifecycle_state="GRANTED",
            grant_json="{}",
        )
        session.add(grant)
        session.commit()
        session.expire_all()
        loaded = session.query(Grant).filter_by(grant_id="g-utc-1").one()
        assert loaded.grant_expire_time.tzinfo is not None
        assert ensure_utc(loaded.grant_expire_time) == aware
        assert cbsd.created_at.tzinfo is not None
    finally:
        session.close()
        engine.dispose()


def test_sqlite_backup_restore_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)
    src = tmp_path / "src.db"
    url = f"sqlite:///{src}"
    upgrade_head(database_url=url)
    engine = create_engine(url)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        session.add(FccIdRecord(fcc_id="backup-fcc", fcc_max_eirp=37.0))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    bak = tmp_path / "backup.db"
    backup_database(url, bak)
    assert bak.is_file()

    restored = tmp_path / "restored.db"
    restore_url = f"sqlite:///{restored}"
    restore_database(restore_url, bak)
    engine = create_engine(restore_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        row = session.query(FccIdRecord).filter_by(fcc_id="backup-fcc").one()
        assert row.fcc_max_eirp == 37.0
    finally:
        session.close()
        engine.dispose()


def test_init_db_uses_alembic(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)
    import database
    from tests.conftest import _safe_restore_engine

    previous = str(database.engine.url)
    db_path = tmp_path / "init.db"
    url = f"sqlite:///{db_path}"
    database.rebind_engine(url)
    try:
        database.init_db(retries=1, delay_seconds=0)
        assert current_revision(database_url=url) == HEAD_REVISION
        present = set(inspect(database.engine).get_table_names())
        assert REQUIRED_TABLES <= present
    finally:
        _safe_restore_engine(previous)


def test_init_db_fast_path_stamps_head(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAS_SCHEMA_VIA_CREATE_ALL", "1")
    import database
    from tests.conftest import _safe_restore_engine

    previous = str(database.engine.url)
    db_path = tmp_path / "fast.db"
    url = f"sqlite:///{db_path}"
    database.rebind_engine(url)
    try:
        database.init_db(retries=1, delay_seconds=0)
        assert current_revision(database_url=url) == HEAD_REVISION
    finally:
        _safe_restore_engine(previous)


def test_alembic_url_preserves_postgres_password():
    """Regression: str(engine.url) redacts passwords and breaks Alembic auth."""
    from sqlalchemy import create_engine

    from services.migrations import database_url_for_alembic

    eng = create_engine("postgresql+psycopg2://sas:s3cret@127.0.0.1:5432/sas")
    try:
        assert "***" in str(eng.url)
        rendered = database_url_for_alembic(eng)
        assert "s3cret" in rendered
        assert "***" not in rendered
    finally:
        eng.dispose()


def test_apply_schema_does_not_stamp_incomplete_legacy(tmp_path: Path, monkeypatch):
    """Legacy DBs missing required tables must not be stamped as head-only."""
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)
    from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

    from services.migrations import apply_schema, current_revision

    db_path = tmp_path / "legacy_partial.db"
    url = f"sqlite:///{db_path}"
    eng = create_engine(url)
    try:
        meta = MetaData()
        Table(
            "fcc_ids",
            meta,
            Column("id", Integer, primary_key=True),
            Column("fcc_id", String(64)),
            Column("fcc_max_eirp", Integer),
        )
        meta.create_all(eng)
        before = set(inspect(eng).get_table_names())
        assert "fcc_ids" in before
        assert "cbsds" not in before
        apply_schema(eng)
        present = set(inspect(eng).get_table_names())
        assert REQUIRED_TABLES <= present
        assert current_revision(database_url=url) == HEAD_REVISION
    finally:
        eng.dispose()
