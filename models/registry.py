"""Central ORM model registration for ``Base.metadata``.

Importing this module (or calling ``load_all_models``) ensures every mapped
table is *defined* on ``models.base.Base``. Which tables a runtime *initializes*
is decided by ``models.persistence.PersistencePlan``, not by this closed list.

``REFERENCE_REQUIRED_TABLES`` / ``REQUIRED_TABLES`` name the full CBRS/WInnForum
reference product schema for tooling and backward-compatible defaults — they are
not "every profile requires these tables".
"""

from __future__ import annotations

from models.base import Base
from models.models import (
    AdminInjectedData,
    BlacklistedFccId,
    BlacklistedFccIdSerial,
    Cbsd,
    ConditionalRegistration,
    CpiUser,
    EscSensor,
    FadDump,
    FadFile,
    FccIdRecord,
    Grant,
    PalRecord,
    PeerFadRecord,
    PeerSas,
    UserIdRecord,
)
from models.persistence import CBRS_WINNFORUM_REFERENCE_TABLES

# Explicit registry: keep in sync when adding ORM tables.
MODEL_MODULES: tuple[object, ...] = (
    AdminInjectedData,
    BlacklistedFccId,
    BlacklistedFccIdSerial,
    Cbsd,
    ConditionalRegistration,
    CpiUser,
    EscSensor,
    FadDump,
    FadFile,
    FccIdRecord,
    Grant,
    PalRecord,
    PeerFadRecord,
    PeerSas,
    UserIdRecord,
)

# Full reference product schema (CBRS/WInnForum). Not a universal profile requirement.
REFERENCE_REQUIRED_TABLES: frozenset[str] = CBRS_WINNFORUM_REFERENCE_TABLES

# Backward-compatible alias — prefer REFERENCE_REQUIRED_TABLES or PersistencePlan.
REQUIRED_TABLES: frozenset[str] = REFERENCE_REQUIRED_TABLES


def load_all_models() -> None:
    """Force-import all mapped classes onto ``Base.metadata``."""
    if not MODEL_MODULES:
        raise RuntimeError("ORM model registry is empty")
    missing = REFERENCE_REQUIRED_TABLES - set(Base.metadata.tables)
    if missing:
        raise RuntimeError(
            "ORM metadata incomplete after model import; missing tables: "
            + ", ".join(sorted(missing))
        )


def expected_table_names() -> frozenset[str]:
    """All table names known to metadata after loading models (definition set)."""
    load_all_models()
    return frozenset(Base.metadata.tables)
