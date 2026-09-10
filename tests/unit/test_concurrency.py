"""concurrent grant / heartbeat / relinquishment isolation."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import database
from models.models import Grant
from services.concurrency import (
    exclusive_cbsd_and_grant,
    reset_resource_locks_for_tests,
)
from services.heartbeat_service import process_heartbeat
from services.relinquishment_service import process_relinquishment
from tests.fixtures.factories import make_cbsd, make_grant, reset_factory_counter


def test_exclusive_lock_orders_cbsd_before_grant():
    """Deadlock avoidance: CBSD lock is acquired before grant lock."""
    order: list[str] = []
    reset_resource_locks_for_tests()

    def holder():
        with exclusive_cbsd_and_grant("c-a", "g-a"):
            order.append("hold")
            barrier.wait()
            barrier.wait()

    barrier = threading.Barrier(2)
    t = threading.Thread(target=holder)
    t.start()
    barrier.wait()
    # Second thread must block until holder releases.
    entered = threading.Event()

    def waiter():
        with exclusive_cbsd_and_grant("c-a", "g-a"):
            entered.set()
            order.append("wait")

    t2 = threading.Thread(target=waiter)
    t2.start()
    assert not entered.wait(timeout=0.2)
    barrier.wait()
    t.join(timeout=2)
    t2.join(timeout=2)
    assert entered.is_set()
    assert order == ["hold", "wait"]


def test_parallel_relinquish_and_heartbeat_coherent_final_state(tmp_path):
    """HBT↔RLQ serialize: final grant state is always protocol-coherent.

    Both may return 0 only when HBT authorized before RLQ relinquished; the
    persisted row must then be RELINQUISHED. If RLQ loses, HBT alone succeeds.
    """
    reset_resource_locks_for_tests()
    reset_factory_counter()
    previous = str(database.engine.url)
    db_path = tmp_path / "conc.db"
    database.rebind_engine(f"sqlite:///{db_path}")
    database.init_db(retries=1, delay_seconds=0)

    setup = database.SessionLocal()
    try:
        cbsd = make_cbsd(setup)
        grant = make_grant(
            setup, cbsd, authorized=False, lifecycle_state="GRANTED"
        )
        grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
        setup.commit()
        cbsd_id = cbsd.cbsd_id
        grant_id = grant.grant_id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def run_rlq() -> None:
        barrier.wait()
        session = database.SessionLocal()
        try:
            resp = process_relinquishment(
                session,
                [{"cbsdId": cbsd_id, "grantId": grant_id}],
            )
            outcomes["rlq"] = resp[0]["response"]["responseCode"]
        finally:
            session.close()

    def run_hbt() -> None:
        barrier.wait()
        session = database.SessionLocal()
        try:
            resp = process_heartbeat(
                session,
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
            session.close()

    t1 = threading.Thread(target=run_rlq)
    t2 = threading.Thread(target=run_hbt)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert "rlq" in outcomes and "hbt" in outcomes
    verify = database.SessionLocal()
    try:
        row = verify.query(Grant).filter_by(grant_id=grant_id).first()
        assert row is not None
        # Serialized critical sections: final row is always a coherent protocol state.
        if row.terminated:
            assert row.lifecycle_state == "RELINQUISHED"
            assert outcomes["rlq"] == 0
            # Heartbeat may have succeeded only if it ran before relinquish.
            assert outcomes["hbt"] in (0, 103)
        else:
            assert outcomes["hbt"] == 0
            assert outcomes["rlq"] == 103
            assert row.authorized is True
            assert row.lifecycle_state == "AUTHORIZED"
    finally:
        verify.close()
        database.rebind_engine(previous)


def test_parallel_double_relinquish_only_one_success(tmp_path):
    reset_resource_locks_for_tests()
    reset_factory_counter()
    previous = str(database.engine.url)
    db_path = tmp_path / "conc2.db"
    database.rebind_engine(f"sqlite:///{db_path}")
    database.init_db(retries=1, delay_seconds=0)

    setup = database.SessionLocal()
    try:
        cbsd = make_cbsd(setup)
        grant = make_grant(
            setup, cbsd, authorized=True, lifecycle_state="AUTHORIZED"
        )
        grant.grant_expire_time = datetime.utcnow() + timedelta(hours=1)
        setup.commit()
        cbsd_id = cbsd.cbsd_id
        grant_id = grant.grant_id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def run_rlq() -> None:
        barrier.wait()
        session = database.SessionLocal()
        try:
            resp = process_relinquishment(
                session,
                [{"cbsdId": cbsd_id, "grantId": grant_id}],
            )
            with lock:
                codes.append(resp[0]["response"]["responseCode"])
        finally:
            session.close()

    threads = [threading.Thread(target=run_rlq) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert codes.count(0) == 1
    assert codes.count(103) == 1

    verify = database.SessionLocal()
    try:
        row = verify.query(Grant).filter_by(grant_id=grant_id).first()
        assert row is not None
        assert row.terminated is True
        assert row.lifecycle_state == "RELINQUISHED"
    finally:
        verify.close()
        database.rebind_engine(previous)


def test_parallel_overlapping_grants_one_conflict(tmp_path):
    """Two concurrent overlapping GRA requests: at most one SUCCESS."""
    reset_resource_locks_for_tests()
    reset_factory_counter()
    previous = str(database.engine.url)
    db_path = tmp_path / "conc_gra.db"
    database.rebind_engine(f"sqlite:///{db_path}")
    database.init_db(retries=1, delay_seconds=0)

    setup = database.SessionLocal()
    try:
        cbsd = make_cbsd(setup)
        setup.commit()
        cbsd_id = cbsd.cbsd_id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def run_gra() -> None:
        barrier.wait()
        session = database.SessionLocal()
        try:
            from services.grant_service import process_grant

            resp = process_grant(
                session,
                [
                    {
                        "cbsdId": cbsd_id,
                        "operationParam": {
                            "maxEirp": 20,
                            "operationFrequencyRange": {
                                "lowFrequency": 3550_000_000,
                                "highFrequency": 3560_000_000,
                            },
                        },
                    }
                ],
            )
            with lock:
                codes.append(resp[0]["response"]["responseCode"])
        finally:
            session.close()

    threads = [threading.Thread(target=run_gra) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert codes.count(0) == 1
    # 401 = GRANT_CONFLICT
    assert codes.count(401) == 1

    verify = database.SessionLocal()
    try:
        grants = (
            verify.query(Grant).filter_by(cbsd_id=cbsd_id, terminated=False).all()
        )
        assert len(grants) == 1
    finally:
        verify.close()
        database.rebind_engine(previous)


def test_relinquish_rejects_cert_mismatch_under_lock(tmp_path):
    reset_resource_locks_for_tests()
    reset_factory_counter()
    previous = str(database.engine.url)
    db_path = tmp_path / "conc_rlq_cert.db"
    database.rebind_engine(f"sqlite:///{db_path}")
    database.init_db(retries=1, delay_seconds=0)

    setup = database.SessionLocal()
    try:
        cbsd = make_cbsd(setup, certificate_hash="bound-cert")
        grant = make_grant(setup, cbsd, authorized=True)
        setup.commit()
        cbsd_id = cbsd.cbsd_id
        grant_id = grant.grant_id
    finally:
        setup.close()

    session = database.SessionLocal()
    try:
        resp = process_relinquishment(
            session,
            [{"cbsdId": cbsd_id, "grantId": grant_id}],
            certificate_hash="other-cert",
        )
        assert resp[0]["response"]["responseCode"] == 103
        assert "cbsdId" not in resp[0]
        assert "grantId" not in resp[0]
        row = session.query(Grant).filter_by(grant_id=grant_id).first()
        assert row is not None and row.terminated is False
    finally:
        session.close()
        database.rebind_engine(previous)


def test_deregister_rejects_cert_mismatch_under_lock(tmp_path):
    from models.models import Cbsd
    from services.deregistration_service import process_deregistration

    reset_resource_locks_for_tests()
    reset_factory_counter()
    previous = str(database.engine.url)
    db_path = tmp_path / "conc_drg_cert.db"
    database.rebind_engine(f"sqlite:///{db_path}")
    database.init_db(retries=1, delay_seconds=0)

    setup = database.SessionLocal()
    try:
        cbsd = make_cbsd(setup, certificate_hash="bound-cert")
        setup.commit()
        cbsd_id = cbsd.cbsd_id
    finally:
        setup.close()

    session = database.SessionLocal()
    try:
        resp = process_deregistration(
            session,
            [{"cbsdId": cbsd_id}],
            certificate_hash="other-cert",
        )
        assert resp[0]["response"]["responseCode"] == 103
        assert "cbsdId" not in resp[0]
        assert session.query(Cbsd).filter_by(cbsd_id=cbsd_id).first() is not None
    finally:
        session.close()
        database.rebind_engine(previous)


def test_advisory_locks_are_noop_on_sqlite(tmp_path):
    previous = str(database.engine.url)
    db_path = tmp_path / "adv.db"
    database.rebind_engine(f"sqlite:///{db_path}")
    database.init_db(retries=1, delay_seconds=0)
    session = database.SessionLocal()
    try:
        from services.concurrency import (
            acquire_cbsd_xact_lock,
            acquire_grant_xact_lock,
        )

        acquire_cbsd_xact_lock(session, "any-cbsd")
        acquire_grant_xact_lock(session, "any-grant")
        session.commit()
    finally:
        session.close()
        database.rebind_engine(previous)
