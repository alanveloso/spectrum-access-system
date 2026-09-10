"""FAD publication concurrency against real PostgreSQL (not SQLite)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import text

import database
from models.models import FadDump, FadFile
from services.concurrency import (
    _supports_advisory_lock,
    acquire_fad_publish_xact_lock,
)
from services.fad_service import (
    create_full_activity_dump,
    get_published_dump,
    verify_ready_dump_integrity,
)

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def _pg_url() -> str | None:
    return os.environ.get("SAS_TEST_DATABASE_URL") or None


def _start_ephemeral_postgres() -> tuple[str, str]:
    if shutil.which("docker") is None:
        raise RuntimeError("docker executable not found")

    container = f"sas-fad-postgres-{os.getpid()}"
    user, password, db = "sas", "sas_test", "sas"
    host_port = str(55442 + (os.getpid() % 1000))
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={db}",
            "-p",
            f"127.0.0.1:{host_port}:5432",
            "postgres:15-alpine",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        detail = (run.stderr or run.stdout or "").strip()
        raise RuntimeError(f"docker run postgres failed: {detail}")

    url = f"postgresql+psycopg2://{user}:{password}@127.0.0.1:{host_port}/{db}"
    probe_code = (
        "import sqlalchemy as sa\n"
        f"e = sa.create_engine({url!r})\n"
        "with e.connect() as c:\n"
        "    c.exec_driver_sql('SELECT 1')\n"
    )
    deadline = time.time() + 90
    last_err = ""
    while time.time() < deadline:
        probe = subprocess.run(
            [PYTHON, "-c", probe_code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if probe.returncode == 0:
            return url, container
        last_err = (probe.stderr or probe.stdout or "").strip()
        time.sleep(1.0)

    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    raise RuntimeError(f"ephemeral postgres not ready: {last_err}")


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    env = _pg_url()
    if env:
        yield env
        return

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

    if shutil.which("docker") is None:
        pytest.skip(
            "PostgreSQL unavailable: set SAS_TEST_DATABASE_URL or install Docker"
        )

    try:
        url, container = _start_ephemeral_postgres()
    except RuntimeError as exc:
        pytest.fail(str(exc))

    try:
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


@pytest.fixture
def pg_session(postgres_url: str) -> Iterator:
    previous = str(database.engine.url)
    database.rebind_engine(postgres_url)
    database.init_db(retries=5, delay_seconds=0.5)
    session = database.SessionLocal()
    try:
        for table in ("fad_files", "fad_dumps"):
            try:
                session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
            except Exception:
                session.rollback()
        session.commit()
        yield session
    finally:
        session.close()
        database.rebind_engine(previous)


def test_postgres_fad_advisory_lock_blocks_peer_session(pg_session):
    assert _supports_advisory_lock(pg_session) is True
    barrier = threading.Barrier(2)
    peer_blocked = threading.Event()
    peer_acquired = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        s = database.SessionLocal()
        try:
            acquire_fad_publish_xact_lock(s)
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
                acquire_fad_publish_xact_lock(s)
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


def test_postgres_concurrent_fad_publish_one_current(pg_session):
    """Two independent DB sessions publish without process-local RLock.

    On PostgreSQL, ``create_full_activity_dump`` skips ``_fad_publish_lock``;
    serialization is ``pg_advisory_xact_lock`` + unique ``published`` index.
    """
    barrier = threading.Barrier(2)
    results: list[int | str] = []
    lock = threading.Lock()

    def worker() -> None:
        s = database.SessionLocal()
        try:
            barrier.wait(timeout=10)
            dump = create_full_activity_dump(s)
            with lock:
                results.append(dump.id)
        except Exception as exc:  # noqa: BLE001 — collect for assertion
            with lock:
                results.append(f"err:{type(exc).__name__}:{exc}")
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2
    assert all(isinstance(r, int) for r in results), results
    assert results[0] != results[1]

    verify = database.SessionLocal()
    try:
        published = verify.query(FadDump).filter_by(published=True).all()
        assert len(published) == 1
        ready = verify.query(FadDump).filter_by(ready=True).all()
        assert len(ready) == 2
        for dump in ready:
            report = verify_ready_dump_integrity(verify, dump)
            assert report["ok"] is True, report
        current = get_published_dump(verify)
        assert current is not None
        assert current.id in results
        orphans = (
            verify.query(FadFile)
            .outerjoin(FadDump, FadFile.dump_id == FadDump.id)
            .filter(FadDump.id.is_(None))
            .count()
        )
        assert orphans == 0
    finally:
        verify.close()


def test_postgres_multiprocess_fad_publish_one_current(postgres_url: str):
    """True multi-process workers (no shared ``_fad_publish_lock`` instance)."""
    previous = str(database.engine.url)
    database.rebind_engine(postgres_url)
    database.init_db(retries=5, delay_seconds=0.5)
    setup = database.SessionLocal()
    try:
        for table in ("fad_files", "fad_dumps"):
            setup.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        setup.commit()
    finally:
        setup.close()

    worker_code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import database
from services.fad_service import create_full_activity_dump
database.rebind_engine({postgres_url!r})
s = database.SessionLocal()
try:
    dump = create_full_activity_dump(s)
    print(dump.id)
finally:
    s.close()
"""
    procs = [
        subprocess.Popen(
            [PYTHON, "-c", worker_code],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    ids: list[int] = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        ids.append(int(out.strip().splitlines()[-1]))

    assert ids[0] != ids[1]
    verify = database.SessionLocal()
    try:
        assert verify.query(FadDump).filter_by(published=True).count() == 1
        assert verify.query(FadDump).filter_by(ready=True).count() == 2
        for dump in verify.query(FadDump).all():
            assert verify_ready_dump_integrity(verify, dump)["ok"] is True
    finally:
        verify.close()
        database.rebind_engine(previous)


def test_postgres_publish_failure_preserves_previous(pg_session, monkeypatch):
    first = create_full_activity_dump(pg_session)
    first_id = first.id
    from services import fad_service

    def _fail_publish(db, dump=None):
        raise RuntimeError("inject publish failure")

    monkeypatch.setattr(fad_service, "verify_ready_dump_integrity", _fail_publish)
    with pytest.raises(RuntimeError, match="inject publish failure"):
        create_full_activity_dump(pg_session)

    other = database.SessionLocal()
    try:
        current = get_published_dump(other)
        assert current is not None
        assert current.id == first_id
        assert other.query(FadDump).filter_by(published=True).count() == 1
        assert other.query(FadDump).count() == 1
        assert verify_ready_dump_integrity(other, current)["ok"] is True
    finally:
        other.close()
