"""Alembic helpers for process-bound engines.

Transactional isolation (documented):
- SQLAlchemy ``SessionLocal`` uses ``autocommit=False`` / ``autoflush=False``.
- Request sessions (``get_db``) roll back on exception and close in ``finally``.
- Default isolation is the database default (PostgreSQL ``READ COMMITTED``,
  SQLite serializable-ish file lock). Protocol mutations that must be atomic
  commit explicitly inside the domain service or route.
- Admin ``/reset`` drops/recreates schema under ``_init_lock``; callers must
  not hold an open request Session across that boundary.

Schema application:
- Production: upgrade to the **explicit** Alembic revision derived from
  ``PersistencePlan`` (never bare ``head`` / ``heads`` for selective plans).
- Fast path for local pytest: set ``SAS_SCHEMA_VIA_CREATE_ALL=1`` to use
  ``Base.metadata.create_all`` then stamp the plan's revision.
- Worker processes must consume schema, not migrate it.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from models.migration_domains import (
    CBRS_HISTORICAL_REVISION,
    HEAD_REVISION,
    next_upgrade_revision,
    resolve_migration_targets,
    revision_satisfies_plan,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def schema_script_location() -> Path:
    """Locate Alembic script directory (repo tree or installed package)."""
    import schema_alembic

    packaged = Path(schema_alembic.__file__).resolve().parent
    if (packaged / "versions").is_dir() and (packaged / "env.py").is_file():
        return packaged
    repo = _REPO_ROOT / "schema_alembic"
    if (repo / "versions").is_dir() and (repo / "env.py").is_file():
        return repo
    raise RuntimeError(
        "schema_alembic migration scripts not found; package data missing "
        "from this installation"
    )


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI) if _ALEMBIC_INI.is_file() else None)
    if not _ALEMBIC_INI.is_file():
        # Installed wheel may omit alembic.ini; configure programmatically.
        cfg.set_main_option("script_location", str(schema_script_location()))
    else:
        cfg.set_main_option("script_location", str(schema_script_location()))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_to(*, database_url: str, revision: str) -> None:
    """Upgrade to an explicit revision id (never ambiguous ``head``)."""
    if revision in {"head", "heads"}:
        raise RuntimeError(
            "refusing ambiguous alembic target "
            f"{revision!r}; resolve an explicit revision from PersistencePlan"
        )
    command.upgrade(alembic_config(database_url), revision)


def upgrade_head(*, database_url: str) -> None:
    """Upgrade to the historical CBRS reference head (tooling / full reference)."""
    upgrade_to(database_url=database_url, revision=HEAD_REVISION)


def downgrade_base(*, database_url: str) -> None:
    command.downgrade(alembic_config(database_url), "base")


def stamp_revision(*, database_url: str, revision: str) -> None:
    if revision in {"head", "heads"}:
        raise RuntimeError(
            "refusing ambiguous alembic stamp "
            f"{revision!r}; use an explicit revision id"
        )
    command.stamp(alembic_config(database_url), revision)


def stamp_head(*, database_url: str) -> None:
    """Stamp the historical CBRS reference head."""
    stamp_revision(database_url=database_url, revision=HEAD_REVISION)


def current_revision(*, database_url: str) -> str | None:
    """Return the current alembic revision id, or None if unversioned."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            return context.get_current_revision()
    finally:
        engine.dispose()


def current_revisions(*, database_url: str) -> tuple[str, ...]:
    """Return all current revision ids (supports multi-head DBs)."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            heads = context.get_current_heads()
            return tuple(sorted(heads))
    finally:
        engine.dispose()


def _schema_via_create_all() -> bool:
    return os.environ.get("SAS_SCHEMA_VIA_CREATE_ALL", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def database_url_for_alembic(engine: Engine) -> str:
    """Return a migration URL that keeps credentials.

    ``str(engine.url)`` redacts passwords as ``***``, which breaks PostgreSQL
    auth when Alembic opens a second connection.
    """
    return engine.url.render_as_string(hide_password=False)


def _create_selected_tables(engine: Engine, tables: tuple | list | None) -> None:
    from models.base import Base

    if tables is not None:
        Base.metadata.create_all(bind=engine, tables=list(tables))
    else:
        Base.metadata.create_all(bind=engine)


def _materialize_plan_extras(engine: Engine, *, plan, covered: frozenset[str]) -> None:
    """create_all for plan tables not covered by the selected Alembic domain."""
    extras = plan.table_names - covered
    if not extras:
        return
    from models.base import Base

    selected = [
        table
        for table in Base.metadata.sorted_tables
        if table.name in extras
    ]
    if selected:
        Base.metadata.create_all(bind=engine, tables=selected)


def apply_schema(
    engine: Engine,
    *,
    tables: tuple | list | None = None,
    persistence_plan=None,
) -> None:
    """Materialize schema for ``persistence_plan`` (or full reference default).

    - Empty DB → upgrade to the plan's explicit revision (or create_all+stamp).
    - Legacy DB with all *plan* tables and no ``alembic_version`` → stamp.
    - Legacy DB missing plan tables → selective ``create_all`` then stamp.
    - Versioned DB → upgrade only when the plan requires a newer domain.

    Never falls back to bare ``alembic upgrade head`` / ``heads`` for a
    selective PersistencePlan.
    """
    from models.persistence import PersistencePlan, reference_persistence_plan

    plan: PersistencePlan
    if persistence_plan is not None:
        plan = persistence_plan
    elif tables is not None:
        # Infer a plan from selected tables (legacy callers).
        names = frozenset(t.name for t in tables)
        from models.persistence import (
            CBRS_WINNFORUM_REFERENCE_TABLES,
            GENERIC_TABLES,
        )

        if names >= CBRS_WINNFORUM_REFERENCE_TABLES or (
            "cbsds" in names and "grants" in names
        ):
            plan = reference_persistence_plan()
        elif names <= GENERIC_TABLES or names == GENERIC_TABLES:
            plan = PersistencePlan(
                contribution_ids=("generic",),
                table_names=names or GENERIC_TABLES,
            )
        else:
            plan = PersistencePlan(
                contribution_ids=("generic",),
                table_names=names,
            )
    else:
        plan = reference_persistence_plan()

    targets = resolve_migration_targets(plan)
    selected_tables = tables if tables is not None else plan.tables()
    url = database_url_for_alembic(engine)
    inspector = inspect(engine)
    present = set(inspector.get_table_names())

    if "alembic_version" not in present and present:
        required = plan.table_names
        if required <= present:
            stamp_revision(database_url=url, revision=targets.stamp_revision)
            return
        # Incomplete pre-Alembic DB: materialize missing selected tables, then stamp.
        _create_selected_tables(engine, selected_tables)
        stamp_revision(database_url=url, revision=targets.stamp_revision)
        return

    if not present and _schema_via_create_all():
        _create_selected_tables(engine, selected_tables)
        stamp_revision(database_url=url, revision=targets.stamp_revision)
        return

    if not present:
        upgrade_to(database_url=url, revision=targets.upgrade_revision)
        _materialize_plan_extras(
            engine, plan=plan, covered=targets.covered_table_names
        )
        return

    # Versioned database.
    current = current_revision(database_url=url)
    if revision_satisfies_plan(current, plan):
        _materialize_plan_extras(
            engine, plan=plan, covered=targets.covered_table_names
        )
        return

    target = next_upgrade_revision(current, plan)
    if target is None:
        _materialize_plan_extras(
            engine, plan=plan, covered=targets.covered_table_names
        )
        return
    upgrade_to(database_url=url, revision=target)
    _materialize_plan_extras(
        engine, plan=plan, covered=targets.covered_table_names
    )


def describe_persistence_migration_state(
    *,
    database_url: str | None = None,
    persistence_plan=None,
) -> dict:
    """Doctor/tooling snapshot: selected domains vs DB revision (no secrets)."""
    from models.persistence import reference_persistence_plan

    plan = persistence_plan or reference_persistence_plan()
    targets = resolve_migration_targets(plan)
    actual: str | None = None
    if database_url:
        try:
            actual = current_revision(database_url=database_url)
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            actual = f"<unavailable: {type(exc).__name__}>"
    return {
        "contribution_ids": list(plan.contribution_ids),
        "migration_domains": list(targets.domain_ids),
        "expected_revision": targets.upgrade_revision,
        "stamp_revision": targets.stamp_revision,
        "actual_revision": actual,
        "cbrs_historical_revision": CBRS_HISTORICAL_REVISION,
        "satisfies_plan": (
            revision_satisfies_plan(actual, plan)
            if isinstance(actual, str) or actual is None
            else False
        ),
    }


# Re-export for callers that imported HEAD_REVISION from this module.
__all__ = [
    "HEAD_REVISION",
    "alembic_config",
    "apply_schema",
    "current_revision",
    "current_revisions",
    "database_url_for_alembic",
    "describe_persistence_migration_state",
    "downgrade_base",
    "schema_script_location",
    "stamp_head",
    "stamp_revision",
    "upgrade_head",
    "upgrade_to",
]
