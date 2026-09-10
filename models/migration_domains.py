"""Map PersistencePlan contributions to Alembic revision targets.

Selection is contribution-id based (composition ownership), never profile-id
or jurisdiction dispatch. Historical CBRS installs remain on
``20260808_0001``; selective virgin installs use the generic / from-generic
chain.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.persistence import (
    CBRS_WINNFORUM_REFERENCE_TABLES,
    GENERIC_TABLES,
    PersistencePlan,
)

# Historical monolithic CBRS/WInnForum reference head (immutable applied history).
CBRS_HISTORICAL_REVISION = "20260808_0001"

# Selective domains (new roots; do not rewrite 20260808_0001).
GENERIC_REVISION = "20260904_generic_0001"
CBRS_FROM_GENERIC_REVISION = "20260904_cbrs_0001"

# Back-compat alias used by tooling/tests that expect a single CBRS head id.
HEAD_REVISION = CBRS_HISTORICAL_REVISION

# Contributions that ship Alembic domains in this repository.
_BUILTIN_MIGRATION_DOMAINS: frozenset[str] = frozenset(
    {
        "generic",
        "cbrs_winnforum_reference",
    }
)


@dataclass(frozen=True, slots=True)
class MigrationTargetSet:
    """Deterministic Alembic target derived from a PersistencePlan."""

    domain_ids: tuple[str, ...]
    # Explicit revision for virgin upgrade (never bare ``head`` / ``heads``).
    upgrade_revision: str
    # Revision stamped after create_all fast-path.
    stamp_revision: str
    # Tables materialized by the Alembic path for these domains.
    covered_table_names: frozenset[str]


def resolve_migration_targets(plan: PersistencePlan) -> MigrationTargetSet:
    """Resolve Alembic targets for ``plan``.

    Fail closed when a contribution claims a builtin migration domain id that
    we cannot map, or when no migratable domain is selected and the plan is
    empty of tables.
    """
    ids = plan.contribution_ids
    if not ids:
        raise RuntimeError(
            "persistence plan has no contributions; cannot resolve migration targets"
        )

    id_set = set(ids)

    # Plugin-only extras are allowed alongside builtins; they use create_all.
    selected_domains = tuple(i for i in ids if i in _BUILTIN_MIGRATION_DOMAINS)
    if not selected_domains and plan.table_names:
        # Only plugin contributions: no Alembic domain — refuse full-schema fallback.
        raise RuntimeError(
            "persistence plan requires tables but no migration domain is mapped for "
            f"contributions={list(ids)}; refuse silent full-schema fallback"
        )

    if "cbrs_winnforum_reference" in id_set:
        return MigrationTargetSet(
            domain_ids=selected_domains,
            upgrade_revision=CBRS_HISTORICAL_REVISION,
            stamp_revision=CBRS_HISTORICAL_REVISION,
            covered_table_names=CBRS_WINNFORUM_REFERENCE_TABLES,
        )

    if "generic" in id_set:
        return MigrationTargetSet(
            domain_ids=selected_domains,
            upgrade_revision=GENERIC_REVISION,
            stamp_revision=GENERIC_REVISION,
            covered_table_names=GENERIC_TABLES,
        )

    raise RuntimeError(
        "unable to resolve migration targets for persistence contributions: "
        + ", ".join(ids)
    )


def revision_satisfies_plan(current: str | None, plan: PersistencePlan) -> bool:
    """Return True when ``current`` Alembic revision already covers ``plan``."""
    if current is None:
        return False
    id_set = set(plan.contribution_ids)
    if "cbrs_winnforum_reference" in id_set:
        return current in {
            CBRS_HISTORICAL_REVISION,
            CBRS_FROM_GENERIC_REVISION,
        }
    if "generic" in id_set:
        return current in {
            GENERIC_REVISION,
            CBRS_FROM_GENERIC_REVISION,
            CBRS_HISTORICAL_REVISION,  # legacy full install ⊇ generic
        }
    return False


def next_upgrade_revision(current: str | None, plan: PersistencePlan) -> str | None:
    """Return the revision to upgrade to, or None if already satisfied.

    Raises RuntimeError when the DB is on an incompatible graph for the plan
    (no silent full-schema fallback).
    """
    targets = resolve_migration_targets(plan)
    if revision_satisfies_plan(current, plan):
        return None

    id_set = set(plan.contribution_ids)
    needs_cbrs = "cbrs_winnforum_reference" in id_set

    if current is None:
        return targets.upgrade_revision

    if needs_cbrs and current == GENERIC_REVISION:
        return CBRS_FROM_GENERIC_REVISION

    if needs_cbrs and current == CBRS_HISTORICAL_REVISION:
        return None

    if not needs_cbrs and current in {
        GENERIC_REVISION,
        CBRS_FROM_GENERIC_REVISION,
        CBRS_HISTORICAL_REVISION,
    }:
        # Extra legacy tables are allowed; do not downgrade.
        return None

    raise RuntimeError(
        "database alembic revision "
        f"{current!r} is incompatible with persistence contributions "
        f"{list(plan.contribution_ids)}; refuse silent full-schema fallback"
    )
