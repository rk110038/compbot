"""Slow-disk handling: SQLite waits on a busy NAS pool are reported clearly instead of hanging or giving a 500."""
import base64
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, jobs
from app.db import Event, JobRun, init_db, log_event, session_scope

H = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode(), "Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _clean_disk_state(monkeypatch):
    init_db()
    monkeypatch.setattr(db, "_last_slow", None)
    db._inflight.clear()
    yield
    db._inflight.clear()


def test_writes_use_wal_without_a_sync_per_commit():
    with db._engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL
    assert db.disk_status() is None


def test_slow_commit_is_reported(monkeypatch):
    monkeypatch.setattr(db, "SLOW_WRITE_S", 1e-9)  # every real commit counts as slow
    with session_scope() as s:
        log_event(s, "system", "slow write test")
    st = db.disk_status()
    assert st and not st["waiting"] and "was slow at" in st["message"] and "a database write took" in st["message"]


def test_stuck_write_blocks_new_runs_and_shows_banner():
    import threading

    db._inflight[threading.get_ident() + 1] = time.monotonic() - 45  # another thread's commit, stuck for 45 s
    st = db.disk_status()
    assert st["waiting"] and "has been waiting 45 s" in st["message"] and db.disk_stalled()
    # Scheduled runs don't start (and don't queue a write behind the stuck one)...
    with session_scope() as s:
        before = s.scalar(select(JobRun.id).order_by(JobRun.id.desc()))
    assert jobs.run_entries().startswith("enter not started: The disk holding compbot's data")
    with session_scope() as s:
        assert s.scalar(select(JobRun.id).order_by(JobRun.id.desc())) == before
    # ...nor do manual ones, and every page says why.
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/run/enter", headers=H, follow_redirects=False)
        assert r.status_code == 303 and "not started" in r.headers["location"].replace("+", " ")
        page = c.get("/log", headers=H).text
        assert "a database write has been waiting" in page and "🐢" in page
        assert c.get("/healthz").json()["disk_slow"] is True


def test_manual_run_logs_from_the_job_thread(monkeypatch):
    monkeypatch.setattr(jobs, "_maintenance", lambda: "nothing to do")
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/run/maintenance", headers=H, follow_redirects=False)
        assert r.status_code == 303 and "Maintenance started" in r.headers["location"].replace("+", " ")
    for _ in range(100):
        if not jobs.is_running("maintenance"):
            break
        time.sleep(0.05)
    with session_scope() as s:
        assert s.scalars(select(Event).where(Event.message == "Manual run requested: maintenance")).first()
        assert s.scalars(select(JobRun).where(JobRun.job == "maintenance").order_by(JobRun.id.desc())).first() \
            .summary == "nothing to do"


def test_locked_database_fails_fast_with_a_clear_message(monkeypatch):
    from app.main import app
    monkeypatch.setattr(db, "BUSY_TIMEOUT_S", 0.3)
    with TestClient(app) as c:  # starts (and writes its "started" event) before the lock is taken
        holder = sqlite3.connect(get_db_path(), timeout=1)
        holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock, like one stuck waiting on the disk
        try:
            t0 = time.monotonic()
            with pytest.raises(Exception) as err:
                with session_scope() as s:
                    log_event(s, "system", "blocked write")
            assert db.is_busy_error(err.value) and time.monotonic() - t0 < 5
            assert "gave up after waiting" in db.disk_status()["message"]
            # A web request that hits the lock gets an explanation, not a bare 500.
            r = c.post("/settings", data={"p_first_name": "X"}, headers=H, follow_redirects=False)
            assert r.status_code == 503 and "disk holding its data is responding slowly" in r.text
            # A job that hits it stops (releasing its slot) and says why.
            assert jobs._run("inbox", lambda: "unused").startswith("error: Stopped: compbot's database is busy")
            assert not jobs.is_running("inbox")
        finally:
            holder.rollback()
            holder.close()
    monkeypatch.undo()
    init_db()


def get_db_path() -> str:
    from app.config import get_config
    return str(get_config().db_path)
