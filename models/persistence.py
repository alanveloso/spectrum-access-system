"""Persistence ownership: contributions + plan resolution.

Generic database infrastructure initializes tables from a resolved
``PersistencePlan``. Model classes remain regime-specific; this module only
decides *which* tables a runtime activates.

Selection is semantic (composition capabilities / adapters / mechanisms),
never profile-id or jurisdiction branching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from sqlalchemy import Table

from models.base import Base

if TYPE_CHECKING:
    from runtime.composition import RuntimeComposition

# --- Table name sets (ownership) -------------------------------------------------

GENERIC_TABLES: frozenset[str] = frozenset({"admin_injected_data"})

# Full CBRS / WInnForum reference product schema (activation, not universal default).
CBRS_WINNFORUM_REFERENCE_TABLES: frozenset[str] = frozenset(
    {
        "admin_injected_data",
        "blacklisted_fcc_ids",
        "blacklisted_fcc_id_serials",
        "cbsds",
        "conditional_registrations",
        "cpi_users",
        "esc_sensors",
        "fad_dumps",
        "fad_files",
        "fcc_ids",
        "grants",
        "pal_records",
        "peer_fad_records",
        "peer_sas",
        "user_ids",
    }
)

# Reference-only tables (absent from a generic-only plan).
CBRS_WINNFORUM_ONLY_TABLES: frozenset[str] = (
    CBRS_WINNFORUM_REFERENCE_TABLES - GENERIC_TABLES
)

# Mechanisms that own CBRS/WInnForum *reference product* regulatory state in
# this repository (IAP / CBSD-oriented persistence). Shared spectrum-management
# mechanisms (protection_entitlement, channel_exclusion, snapshot_evaluate_apply,
# …) are deliberately excluded — they are regime-generic.
_CBRS_REFERENCE_MECHANISMS: frozenset[str] = frozenset(
    {
        "aggregate_linear_power",
        "single_link_threshold",
    }
)


@dataclass(frozen=True, slots=True)
class PersistenceContribution:
    """Named ownership of a set of SQLAlchemy tables.

    ``matches`` decides activation from a composed runtime (semantic, not id).
    """

    contribution_id: str
    table_names: frozenset[str]
    matches: Callable[[RuntimeComposition], bool]
    origin: str = "builtin"  # builtin | plugin


@dataclass(frozen=True, slots=True)
class PersistencePlan:
    """Resolved tables required for one runtime installation."""

    contribution_ids: tuple[str, ...]
    table_names: frozenset[str]

    def tables(self) -> tuple[Table, ...]:
        """SQLAlchemy Table objects for selective create_all / validation."""
        missing = self.table_names - set(Base.metadata.tables)
        if missing:
            raise RuntimeError(
                "persistence plan references unknown tables: "
                + ", ".join(sorted(missing))
            )
        # Preserve FK dependency order via metadata.sorted_tables.
        selected = [
            table
            for table in Base.metadata.sorted_tables
            if table.name in self.table_names
        ]
        return tuple(selected)


def _generic_matches(_composition: RuntimeComposition) -> bool:
    return True


def _cbrs_winnforum_reference_matches(composition: RuntimeComposition) -> bool:
    """Activate full reference schema only for reference-owned runtime state.

    Positive evidence (any one is sufficient):

    * composed ProtocolAdapter advertises the WInnForum REST semantic
      ``protocol_id`` (CBSD/Grant/FAD/CPI product surface), or
    * profile selects a CBRS-reference-specific mechanism
      (``aggregate_linear_power`` / ``single_link_threshold``).

    Explicitly *not* evidence: DeviceAdapter/NetworkAdapter presence,
    geolocation, or shared protection/coordination mechanisms.
    """
    from adapters.winnforum_rest import WINNFORUM_REST_PROTOCOL_ID
    from profiles.context import selected_mechanism_ids

    adapter = composition.protocol_adapter
    if adapter is not None:
        protocol_id = getattr(adapter, "protocol_id", None)
        if protocol_id == WINNFORUM_REST_PROTOCOL_ID:
            return True

    mechanisms = frozenset(selected_mechanism_ids(composition.profile))
    return bool(mechanisms & _CBRS_REFERENCE_MECHANISMS)


def builtin_persistence_contributions() -> tuple[PersistenceContribution, ...]:
    return (
        PersistenceContribution(
            contribution_id="generic",
            table_names=GENERIC_TABLES,
            matches=_generic_matches,
            origin="builtin",
        ),
        PersistenceContribution(
            contribution_id="cbrs_winnforum_reference",
            table_names=CBRS_WINNFORUM_REFERENCE_TABLES,
            matches=_cbrs_winnforum_reference_matches,
            origin="builtin",
        ),
    )


_extra_contributions: list[PersistenceContribution] = []


def register_persistence_contribution(contribution: PersistenceContribution) -> None:
    """Register an additional contribution (tests / future packages).

    Does not require editing ``database.py``. Duplicate ``contribution_id`` fails.
    """
    known = {c.contribution_id for c in builtin_persistence_contributions()}
    known |= {c.contribution_id for c in _extra_contributions}
    if contribution.contribution_id in known:
        raise ValueError(
            f"duplicate persistence contribution {contribution.contribution_id!r}"
        )
    _extra_contributions.append(contribution)


def clear_extra_persistence_contributions() -> None:
    """Test helper: remove contributions registered via ``register_…``."""
    _extra_contributions.clear()


def all_persistence_contributions() -> tuple[PersistenceContribution, ...]:
    return builtin_persistence_contributions() + tuple(_extra_contributions)


def resolve_persistence_plan(
    composition: RuntimeComposition,
) -> PersistencePlan:
    """Select contributions for ``composition`` and union their tables."""
    selected: list[PersistenceContribution] = []
    for contribution in all_persistence_contributions():
        if contribution.matches(composition):
            selected.append(contribution)
    if not selected:
        raise RuntimeError("persistence plan resolved to zero contributions")
    names: set[str] = set()
    for contribution in selected:
        names |= set(contribution.table_names)
    return PersistencePlan(
        contribution_ids=tuple(c.contribution_id for c in selected),
        table_names=frozenset(names),
    )


def reference_persistence_plan() -> PersistencePlan:
    """Full CBRS/WInnForum reference schema (tests, admin reset, tooling)."""
    return PersistencePlan(
        contribution_ids=("generic", "cbrs_winnforum_reference"),
        table_names=CBRS_WINNFORUM_REFERENCE_TABLES,
    )
