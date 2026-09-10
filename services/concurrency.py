"""Per-CBSD / per-grant concurrency control for CBSD↔SAS mutations.

Combines:

- process-local keyed ``RLock``s (threaded Uvicorn / SQLite tests);
- SQL ``SELECT … FOR UPDATE`` when the dialect supports it;
- PostgreSQL ``pg_advisory_xact_lock`` so create-or-mutate races (no row yet)
 still serialize across workers sharing one database.

Callers must hold the matching exclusive context, acquire transaction advisory
locks, load/mutate rows, then ``commit`` **per item while still holding** the
process lock so peer sessions observe the write before the next waiter proceeds.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from models.models import Cbsd, Grant

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}

# Distinct namespaces so CBSD, grant, FAD, CPAS and IAP advisory keys never collide.
_ADVISORY_NS_CBSD = b"cbsd\0"
_ADVISORY_NS_GRANT = b"grant\0"
_ADVISORY_NS_FAD = b"fad\0"
_ADVISORY_NS_CPAS = b"cpas\0"
_ADVISORY_NS_IAP = b"iap\0"
_FAD_PUBLISH_LOCK_NAME = "publish"
_CPAS_PIPELINE_LOCK_NAME = "pipeline"
_IAP_ADMISSION_LOCK_NAME = "admission"
_IAP_ADMISSION_PROCESS_KEY = "iap:admission"


def _lock_for(key: str) -> threading.RLock:
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def cbsd_lock_key(cbsd_id: str) -> str:
    return f"cbsd:{cbsd_id}"


def grant_lock_key(grant_id: str) -> str:
    return f"grant:{grant_id}"


def reset_resource_locks_for_tests() -> None:
    """Drop process-local locks (isolated unit tests only)."""
    with _registry_guard:
        _locks.clear()


@contextmanager
def exclusive_iap_admission() -> Iterator[None]:
    """Serialize IAP grant-admission evaluate→persist across CBSDs (same process).

 Held together with ``acquire_iap_admission_xact_lock`` so concurrent
 proposals cannot both observe the same residual headroom.

 Also held by authorization-baseline writers (peer FAD apply, IAP-relevant
 protection injection, CPAS apply/stamp) so Grant admission cannot race them.
 """
    with _lock_for(_IAP_ADMISSION_PROCESS_KEY):
        yield


@contextmanager
def iap_admission_critical(db: Session) -> Iterator[None]:
    """Process IAP admission lock + PostgreSQL transaction advisory lock.

 Canonical first lock in the IAP authorization-state domain. Callers that
 also need CPAS / FAD / CBSD locks must acquire those *after* entering this
 context (or after ``exclusive_iap_admission`` + ``acquire_iap_admission_xact_lock``).

 Lock order (never invert)::

 IAP admission → CPAS pipeline → FAD publish → CBSD
 """
    with exclusive_iap_admission():
        acquire_iap_admission_xact_lock(db)
        yield


@contextmanager
def exclusive_cbsd(cbsd_id: str) -> Iterator[None]:
    """Serialize mutations that target a single CBSD identity (same process)."""
    with _lock_for(cbsd_lock_key(cbsd_id)):
        yield


@contextmanager
def exclusive_grant(grant_id: str) -> Iterator[None]:
    """Serialize mutations that target a single grant identity (same process)."""
    with _lock_for(grant_lock_key(grant_id)):
        yield


@contextmanager
def exclusive_cbsd_and_grant(cbsd_id: str, grant_id: str) -> Iterator[None]:
    """Acquire CBSD then grant locks (fixed order to avoid deadlocks)."""
    with exclusive_cbsd(cbsd_id):
        with exclusive_grant(grant_id):
            yield


def _supports_row_lock(db: Session) -> bool:
    bind = db.get_bind()
    if bind is None:
        return False
    # SQLite accepts FOR UPDATE syntactically in some modes but does not provide
    # the PostgreSQL-style row lock semantics we rely on.
    return bind.dialect.name != "sqlite"


def _supports_advisory_lock(db: Session) -> bool:
    bind = db.get_bind()
    if bind is None:
        return False
    return bind.dialect.name == "postgresql"


def _advisory_key(namespace: bytes, name: str) -> int:
    digest = hashlib.blake2b(namespace + name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def acquire_cbsd_xact_lock(db: Session, cbsd_id: str) -> None:
    """Transaction-scoped advisory lock for a CBSD id (PostgreSQL only)."""
    if not _supports_advisory_lock(db):
        return
    key = _advisory_key(_ADVISORY_NS_CBSD, cbsd_id)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def acquire_grant_xact_lock(db: Session, grant_id: str) -> None:
    """Transaction-scoped advisory lock for a grant id (PostgreSQL only)."""
    if not _supports_advisory_lock(db):
        return
    key = _advisory_key(_ADVISORY_NS_GRANT, grant_id)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def acquire_fad_publish_xact_lock(db: Session) -> None:
    """Serialize Full Activity Dump publication across PostgreSQL workers.

 Transaction-scoped; released on commit/rollback. No-op on non-PostgreSQL.
 """
    if not _supports_advisory_lock(db):
        return
    key = _advisory_key(_ADVISORY_NS_FAD, _FAD_PUBLISH_LOCK_NAME)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def acquire_cpas_pipeline_xact_lock(db: Session) -> None:
    """Serialize CPAS apply+FAD critical section across PostgreSQL workers."""
    if not _supports_advisory_lock(db):
        return
    key = _advisory_key(_ADVISORY_NS_CPAS, _CPAS_PIPELINE_LOCK_NAME)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def acquire_iap_admission_xact_lock(db: Session) -> None:
    """Serialize IAP grant admission across PostgreSQL workers.

 Transaction-scoped; released on commit/rollback. No-op on non-PostgreSQL.
 Pair with ``exclusive_iap_admission`` for process-local coverage.
 """
    if not _supports_advisory_lock(db):
        return
    key = _advisory_key(_ADVISORY_NS_IAP, _IAP_ADMISSION_LOCK_NAME)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def iap_admission_advisory_key() -> int:
    """Stable PG advisory key for IAP admission (tests / diagnostics)."""
    return _advisory_key(_ADVISORY_NS_IAP, _IAP_ADMISSION_LOCK_NAME)


def lock_cbsd_row(db: Session, cbsd_id: str) -> Cbsd | None:
    """Load a CBSD row, taking a DB row lock when the dialect supports it."""
    query = db.query(Cbsd).filter_by(cbsd_id=cbsd_id)
    if _supports_row_lock(db):
        query = query.with_for_update()
    return query.first()


def lock_grant_row(
    db: Session, grant_id: str, cbsd_id: str | None = None
) -> Grant | None:
    """Load a Grant row, taking a DB row lock when the dialect supports it."""
    query = db.query(Grant).filter_by(grant_id=grant_id)
    if cbsd_id is not None:
        query = query.filter_by(cbsd_id=cbsd_id)
    if _supports_row_lock(db):
        query = query.with_for_update()
    return query.first()


def lock_grants_for_cbsd(db: Session, cbsd_id: str) -> list[Grant]:
    """Load all grants for a CBSD, locking rows when the dialect supports it."""
    query = db.query(Grant).filter_by(cbsd_id=cbsd_id)
    if _supports_row_lock(db):
        query = query.with_for_update()
    return list(query.all())
