"""Logical database backup / restore for certification and ops.

SQLite: file copy (hot backup under the process lock used by tests).
PostgreSQL / generic: JSON dump of all ORM tables (portable, no pg_dump binary).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from models.base import Base
from models.registry import load_all_models


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def backup_database(database_url: str, dest: Path) -> Path:
    """Write a backup artifact to ``dest`` (file path). Returns ``dest``."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if database_url.startswith("sqlite:///"):
        src = Path(database_url.removeprefix("sqlite:///"))
        if not src.is_file():
            raise FileNotFoundError(f"sqlite database not found: {src}")
        shutil.copy2(src, dest)
        return dest
    return _logical_backup(database_url, dest)


def restore_database(database_url: str, source: Path) -> None:
    """Restore from a backup produced by :func:`backup_database`."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    if database_url.startswith("sqlite:///"):
        target = Path(database_url.removeprefix("sqlite:///"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return
    _logical_restore(database_url, source)


def _logical_backup(database_url: str, dest: Path) -> Path:
    load_all_models()
    engine = create_engine(database_url)
    try:
        payload: dict[str, Any] = {
            "format": "sas-core-logical-v1",
            "tables": {},
        }
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        try:
            for table in Base.metadata.sorted_tables:
                rows = session.execute(table.select()).mappings().all()
                payload["tables"][table.name] = [dict(r) for r in rows]
        finally:
            session.close()
        dest.write_text(
            json.dumps(payload, default=_json_default, indent=2) + "\n",
            encoding="utf-8",
        )
        return dest
    finally:
        engine.dispose()


def _logical_restore(database_url: str, source: Path) -> None:
    load_all_models()
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("format") != "sas-core-logical-v1":
        raise ValueError("unsupported backup format")
    engine = create_engine(database_url)
    try:
        from services.migrations import upgrade_head

 # Ensure schema exists, then replace row data.
        if not inspect(engine).get_table_names():
            upgrade_head(database_url=database_url)
        SessionLocal = sessionmaker(bind=engine)
        session: Session = SessionLocal()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
            for table in Base.metadata.sorted_tables:
                rows = data.get("tables", {}).get(table.name) or []
                if not rows:
                    continue
                session.execute(table.insert(), rows)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    finally:
        engine.dispose()


def engine_table_row_counts(engine: Engine) -> dict[str, int]:
    """Helper for tests: map table name → row count."""
    from sqlalchemy import func, select

    load_all_models()
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            out[table.name] = int(
                conn.execute(select(func.count()).select_from(table)).scalar_one()
            )
    return out


__all__ = [
    "backup_database",
    "engine_table_row_counts",
    "restore_database",
]
