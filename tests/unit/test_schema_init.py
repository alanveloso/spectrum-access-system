"""Regression: fresh SQLite schema includes all required Admin/Registration tables."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

import database
from models.registry import REQUIRED_TABLES, load_all_models


def test_init_db_on_empty_sqlite_creates_admin_and_blacklist_tables(tmp_path: Path):
    """Empty file DB + init_db must materialize admin_injected_data and serial blacklist."""
    db_path = tmp_path / "fresh_schema.db"
    assert not db_path.exists()

    previous_url = str(database.engine.url)
    try:
        database.rebind_engine(f"sqlite:///{db_path}")
        database.init_db(retries=1, delay_seconds=0)

        assert db_path.exists()
        tables = set(inspect(database.engine).get_table_names())
        assert "admin_injected_data" in tables
        assert "blacklisted_fcc_id_serials" in tables
        assert REQUIRED_TABLES.issubset(tables)
        # Metadata registry stayed aligned with the physical schema.
        load_all_models()
        assert "admin_injected_data" in database.Base.metadata.tables
        assert "blacklisted_fcc_id_serials" in database.Base.metadata.tables
    finally:
        from tests.conftest import _safe_restore_engine

        _safe_restore_engine(previous_url)


def test_load_all_models_registers_required_metadata_without_engine():
    load_all_models()
    names = set(database.Base.metadata.tables)
    assert "admin_injected_data" in names
    assert "blacklisted_fcc_id_serials" in names
    assert REQUIRED_TABLES.issubset(names)
