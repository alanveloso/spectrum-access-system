"""concurrency behaviors against real PostgreSQL (not SQLite evidence)."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from sqlalchemy import text

import database
from models.models import Cbsd, Grant
from services.clock import ensure_utc
from services.concurrency import (
    _supports_advisory_lock,
    _supports_row_lock,
    acquire_cbsd_xact_lock,
    acquire_grant_xact_lock,
    exclusive_cbsd_and_grant,
    lock_cbsd_row,
    lock_grant_row,
    reset_resource_locks_for_tests,
)
from services.grant_renewal import apply_renewal
from services.heartbeat_service import process_heartbeat
from services.registration_service import process_registration
from services.relinquishment_service import process_relinquishment
from services.deregistration_service import process_deregistration
from tests.fixtures.factories import (
    cat_a_install,
    make_cbsd,
    make_fcc_id,
    make_grant,
    make_user_id,
    reset_factory_counter,
)


def _pg_url() -> str | None:
    return os.environ.get("SAS_TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    env = _pg_url()
    if env:
        yield env
        return
    # Prefer the container started on 55432 if present.
    candidate = "postgresql+psycopg2://sas:sas_test@127.0.0.1:55432/sas"
    try:
        from sqlalchemy import create_engine

        eng = create_engine(candidate)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        yield candidate
        return
    except Exception:
        pass
    pytest.skip(
        "PostgreSQL unavailable: set SAS_TEST_DATABASE_URL or start "
        "postgres on 127.0.0.1:55432 (sas/sas_test/sas)"
    )


@pytest.fixture
def pg_session(postgres_url: str) -> Iterator:
    previous = str(database.engine.url)
    reset_factory_counter()
    reset_resource_locks_for_tests()
    database.rebind_engine(postgres_url)
    database.init_db(retries=5, delay_seconds=0.5)
    # Isolate each test in a dedicated schema-free DB by truncating core tables.
    session = database.SessionLocal()
    try:
        for table in (
            "grants",
            "cbsds",
            "fcc_ids",
            "user_ids",
            "admin_injected_data",
            "blacklisted_fcc_ids",
            "blacklisted_fcc_id_serials",
            "conditional_registrations",
            "cpi_users",
            "pal_records",
        ):
            try:
                session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
            except Exception:
                session.rollback()
        session.commit()
        yield session
    finally:
        session.close()
        database.rebind_engine(previous)
        reset_resource_locks_for_tests()


def test_postgres_dialect_enables_advisory_and_row_locks(pg_session):
    assert _supports_advisory_lock(pg_session) is True
    assert _supports_row_lock(pg_session) is True
    acquire_cbsd_xact_lock(pg_session, "probe-cbsd")
    acquire_grant_xact_lock(pg_session, "probe-grant")
    # Confirm advisory lock is held in this transaction.
    row = pg_session.execute(
        text(
            "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND granted = true"
        )
    ).scalar()
    assert int(row) >= 1
    pg_session.commit()


def test_postgres_for_update_and_advisory_block_peer_session(pg_session):
    """Prove DB locks serialize without relying on process-local RLocks."""
    cbsd = make_cbsd(pg_session, certificate_hash="cert-a")
    pg_session.commit()
    cbsd_id = cbsd.cbsd_id

    barrier = threading.Barrier(2)
    peer_blocked = threading.Event()
    peer_acquired = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        s = database.SessionLocal()
        try:
            # Intentionally skip process RLock — exercise PostgreSQL locks only.
            acquire_cbsd_xact_lock(s, cbsd_id)
            row = lock_cbsd_row(s, cbsd_id)
            assert row is not None
            barrier.wait()
            release_holder.wait(timeout=5.0)
            s.commit()
        finally:
            s.close()

    def waiter() -> None:
        barrier.wait()
        s = database.SessionLocal()
        try:
            s.execute(text("SET LOCAL lock_timeout = '400ms'"))
            try:
                acquire_cbsd_xact_lock(s, cbsd_id)
                lock_cbsd_row(s, cbsd_id)
                peer_acquired.set()
            except Exception:
                peer_blocked.set()
            finally:
                release_holder.set()
                s.rollback()
        finally:
            s.close()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert peer_blocked.is_set()
    assert not peer_acquired.is_set()


def test_postgres_parallel_registration_no_duplicate(pg_session):
    make_fcc_id(pg_session, fcc_id="fcc-pg-reg")
    make_user_id(pg_session, user_id="user-pg-reg")
    pg_session.commit()

    payload = {
        "fccId": "fcc-pg-reg",
        "userId": "user-pg-reg",
        "cbsdSerialNumber": "serial-pg-reg",
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": cat_a_install(),
        "measCapability": ["RECEIVED_POWER_WITHOUT_GRANT"],
    }
    barrier = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def run() -> None:
        barrier.wait()
        s = database.SessionLocal()
        try:
            resp = process_registration(
                s, [payload], certificate_hash="cert-reg-pg"
            )
            with lock:
                codes.append(resp[0]["response"]["responseCode"])
        finally:
            s.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert codes.count(0) >= 1
    # At most one success; loser may be 0 (re-register) or 103 (IntegrityError).
    assert len(codes) == 2
    verify = database.SessionLocal()
    try:
        rows = verify.query(Cbsd).filter_by(
            fcc_id="fcc-pg-reg", cbsd_serial_number="serial-pg-reg"
        ).all()
        assert len(rows) == 1
    finally:
        verify.close()


def test_postgres_concurrent_renew_monotonic(pg_session):
    cbsd = make_cbsd(pg_session)
    grant = make_grant(pg_session, cbsd, authorized=True, lifecycle_state="AUTHORIZED")
    base_expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
    grant.grant_expire_time = base_expire
    grant.grant_json = '{"auth_context":{"channelType":"GAA"}}'
    pg_session.commit()
    grant_id = grant.grant_id
    cbsd_id = cbsd.cbsd_id

    barrier = threading.Barrier(2)
    expires: list[datetime] = []
    lock = threading.Lock()

    def run() -> None:
        barrier.wait()
        s = database.SessionLocal()
        try:
            with exclusive_cbsd_and_grant(cbsd_id, grant_id):
                acquire_cbsd_xact_lock(s, cbsd_id)
                acquire_grant_xact_lock(s, grant_id)
                row = lock_grant_row(s, grant_id, cbsd_id)
                assert row is not None
                outcome = apply_renewal(s, row)
                assert outcome.ok
                s.commit()
                with lock:
                    expires.append(
                        ensure_utc(row.grant_expire_time).replace(microsecond=0)
                    )
        finally:
            s.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(expires) == 2
    base = ensure_utc(base_expire)
    assert all(e >= base for e in expires)
    verify = database.SessionLocal()
    try:
        row = verify.query(Grant).filter_by(grant_id=grant_id).one()
        final = ensure_utc(row.grant_expire_time).replace(microsecond=0)
        assert final >= max(expires)
        assert final > base
    finally:
        verify.close()


def test_postgres_rlq_cert_recheck_under_lock(pg_session):
    cbsd = make_cbsd(pg_session, certificate_hash="bound-pg")
    grant = make_grant(pg_session, cbsd, authorized=True)
    pg_session.commit()

    resp = process_relinquishment(
        pg_session,
        [{"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id}],
        certificate_hash="other-pg",
    )
    assert resp[0]["response"]["responseCode"] == 103
    assert "cbsdId" not in resp[0]
    row = pg_session.query(Grant).filter_by(grant_id=grant.grant_id).one()
    assert row.terminated is False


def test_postgres_drg_cert_recheck_under_lock(pg_session):
    cbsd = make_cbsd(pg_session, certificate_hash="bound-pg-drg")
    pg_session.commit()
    cbsd_id = cbsd.cbsd_id

    resp = process_deregistration(
        pg_session,
        [{"cbsdId": cbsd_id}],
        certificate_hash="other-pg-drg",
    )
    assert resp[0]["response"]["responseCode"] == 103
    assert "cbsdId" not in resp[0]
    assert pg_session.query(Cbsd).filter_by(cbsd_id=cbsd_id).first() is not None


def test_postgres_hbt_rlq_serialized(pg_session):
    cbsd = make_cbsd(pg_session)
    grant = make_grant(
        pg_session, cbsd, authorized=False, lifecycle_state="GRANTED"
    )
    grant.grant_expire_time = datetime.now(timezone.utc) + timedelta(hours=1)
    pg_session.commit()
    cbsd_id = cbsd.cbsd_id
    grant_id = grant.grant_id

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def run_rlq() -> None:
        barrier.wait()
        s = database.SessionLocal()
        try:
            resp = process_relinquishment(
                s, [{"cbsdId": cbsd_id, "grantId": grant_id}]
            )
            outcomes["rlq"] = resp[0]["response"]["responseCode"]
        finally:
            s.close()

    def run_hbt() -> None:
        barrier.wait()
        s = database.SessionLocal()
        try:
            resp = process_heartbeat(
                s,
                [
                    {
                        "cbsdId": cbsd_id,
                        "grantId": grant_id,
                        "operationState": "GRANTED",
                    }
                ],
            )
            outcomes["hbt"] = resp[0]["response"]["responseCode"]
        finally:
            s.close()

    t1 = threading.Thread(target=run_rlq)
    t2 = threading.Thread(target=run_hbt)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    verify = database.SessionLocal()
    try:
        row = verify.query(Grant).filter_by(grant_id=grant_id).one()
        if row.terminated:
            assert row.lifecycle_state == "RELINQUISHED"
            assert outcomes["rlq"] == 0
        else:
            assert outcomes["hbt"] == 0
            assert row.lifecycle_state == "AUTHORIZED"
    finally:
        verify.close()
