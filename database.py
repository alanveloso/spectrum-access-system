"""SQLAlchemy engine and session helpers (PostgreSQL in production, SQLite locally).

Transactional isolation: sessions are non-autocommit; ``get_db`` rolls
back on error. Schema changes go through Alembic (``services.migrations``).
See ``services/migrations.py`` module docstring for isolation notes.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from config import get_settings
from models.base import Base

# Re-export for callers that historically imported Base from database.
__all__ = ["Base", "SessionLocal", "engine", "get_db", "init_db", "rebind_engine", "reset_db"]

logger = logging.getLogger(__name__)

_settings = get_settings()

_engine_kwargs: dict = {
    "echo": _settings.db_echo,
}
if _settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs.update(
        {
            "pool_size": _settings.db_pool_size,
            "max_overflow": _settings.db_max_overflow,
            "pool_timeout": _settings.db_pool_timeout,
            "pool_recycle": _settings.db_pool_recycle,
            "pool_pre_ping": _settings.db_pool_pre_ping,
        }
    )

engine = create_engine(_settings.database_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

_init_lock = threading.Lock()


def rebind_engine(database_url: str, *, echo: bool = False) -> None:
    """Replace the process-wide engine/session factory.

    Intended for isolated test databases. Callers that did
    ``from database import SessionLocal`` keep the old binding; prefer
    ``database.SessionLocal`` after rebind, or use the ``db_session`` fixture.
    """
    global engine, SessionLocal

    kwargs: dict = {"echo": echo}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            {
                "pool_size": _settings.db_pool_size,
                "max_overflow": _settings.db_max_overflow,
                "pool_timeout": _settings.db_pool_timeout,
                "pool_recycle": _settings.db_pool_recycle,
                "pool_pre_ping": _settings.db_pool_pre_ping,
            }
        )
    with _init_lock:
        previous = engine
        engine = create_engine(database_url, **kwargs)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        previous.dispose()


def get_db():
    # Resolve SessionLocal via module attribute so rebind_engine is visible.
    import database as database_module

    db = database_module.SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _assert_required_tables(bind, required: frozenset[str]) -> None:
    """Fail fast when schema init did not materialize the planned tables."""
    present = set(inspect(bind).get_table_names())
    missing = required - present
    if missing:
        raise RuntimeError(
            "Database schema incomplete after init_db; missing tables: "
            + ", ".join(sorted(missing))
        )


def init_db(
    *,
    retries: int = 10,
    delay_seconds: float = 2.0,
    persistence_plan=None,
) -> None:
    """Apply schema for ``persistence_plan`` (default: full reference schema).

    Pass an explicit ``PersistencePlan`` from startup/composition. Omitting the
    plan keeps the historical reference product schema for tests and tooling.
    """
    from models.persistence import PersistencePlan, reference_persistence_plan
    from models.registry import load_all_models
    from services.migrations import apply_schema

    load_all_models()
    plan: PersistencePlan = persistence_plan or reference_persistence_plan()
    selected = plan.tables()

    with _init_lock:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                apply_schema(
                    engine, tables=selected, persistence_plan=plan
                )
                _ensure_lifecycle_columns(engine)
                _ensure_fad_published_column(engine)
                _ensure_peer_sas_fad_generation_column(engine)
                _assert_required_tables(engine, plan.table_names)
                return
            except OperationalError as exc:
                last_exc = exc
                message = str(exc).lower()
                # Concurrent schema create can race on SQLite ("table already exists").
                if "already exists" in message:
                    _ensure_lifecycle_columns(engine)
                    _ensure_fad_published_column(engine)
                    _ensure_peer_sas_fad_generation_column(engine)
                    _assert_required_tables(engine, plan.table_names)
                    return
                logger.warning(
                    "init_db attempt %s/%s failed: %s", attempt, retries, exc
                )
                if attempt < retries:
                    time.sleep(delay_seconds)
        if last_exc is not None:
            raise last_exc


def _ensure_lifecycle_columns(bind) -> None:
    """Add lifecycle_state columns on legacy DBs stamped without them."""
    from sqlalchemy import text

    inspector = inspect(bind)
    statements: list[str] = []
    if "cbsds" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("cbsds")}
        if "lifecycle_state" not in cols:
            statements.append(
                "ALTER TABLE cbsds ADD COLUMN lifecycle_state VARCHAR(32) "
                "DEFAULT 'REGISTERED' NOT NULL"
            )
    if "grants" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("grants")}
        if "lifecycle_state" not in cols:
            statements.append(
                "ALTER TABLE grants ADD COLUMN lifecycle_state VARCHAR(32) "
                "DEFAULT 'GRANTED' NOT NULL"
            )
    if not statements:
        return
    with bind.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    logger.info("Applied lifecycle schema patches: %s", statements)


def _ensure_fad_published_column(bind) -> None:
    """Add FadDump.published + unique partial index on legacy DBs."""
    from sqlalchemy import text

    inspector = inspect(bind)
    tables = inspector.get_table_names()
    if "fad_dumps" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("fad_dumps")}
    dialect = bind.dialect.name
    statements: list[str] = []

    if "published" not in cols:
        if dialect == "postgresql":
            statements.append(
                "ALTER TABLE fad_dumps ADD COLUMN published BOOLEAN "
                "DEFAULT FALSE NOT NULL"
            )
        else:
            statements.append(
                "ALTER TABLE fad_dumps ADD COLUMN published BOOLEAN DEFAULT 0 NOT NULL"
            )

    index_names = {idx["name"] for idx in inspector.get_indexes("fad_dumps")}
    if "uq_fad_dumps_one_published" not in index_names:
        if dialect == "postgresql":
            statements.append(
                "CREATE UNIQUE INDEX uq_fad_dumps_one_published "
                "ON fad_dumps (published) WHERE published IS TRUE"
            )
        else:
            statements.append(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_fad_dumps_one_published "
                "ON fad_dumps (published) WHERE published IS TRUE"
            )

    if statements:
        with bind.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
            if "published" not in cols:
                conn.execute(
                    text(
                        "UPDATE fad_dumps SET published = TRUE WHERE ready = TRUE"
                    )
                )
                if "fad_files" in tables:
                    conn.execute(
                        text(
                            "UPDATE fad_dumps SET ready = TRUE "
                            "WHERE id IN (SELECT DISTINCT dump_id FROM fad_files)"
                        )
                    )
        logger.info("Applied FAD published schema patches: %s", statements)


def _ensure_peer_sas_fad_generation_column(bind) -> None:
    """Add PeerSas.last_fad_generation on legacy DBs."""
    from sqlalchemy import text

    inspector = inspect(bind)
    if "peer_sas" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("peer_sas")}
    if "last_fad_generation" in cols:
        return
    stmt = "ALTER TABLE peer_sas ADD COLUMN last_fad_generation VARCHAR(32)"
    with bind.begin() as conn:
        conn.execute(text(stmt))
    logger.info("Applied peer_sas.last_fad_generation schema patch")


def reset_db() -> None:
    """Drop and recreate the full reference schema (admin/reset)."""
    from models.persistence import reference_persistence_plan
    from models.registry import load_all_models
    from services.migrations import apply_schema

    load_all_models()
    plan = reference_persistence_plan()
    selected = plan.tables()

    with _init_lock:
        Base.metadata.drop_all(bind=engine)
        # Drop alembic_version if present so schema apply recreates from scratch.
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        apply_schema(engine, tables=selected, persistence_plan=plan)
        _assert_required_tables(engine, plan.table_names)
