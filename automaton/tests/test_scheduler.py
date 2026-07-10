"""Scheduler tests.

What we prove here:
  1. Only one scheduler instance holds the lock at a time.
  2. A due cron trigger fires exactly once even when two schedulers contend.
  3. next_fire_at advances to a future time after firing (skip-on-miss).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from automaton import db as _db
from automaton import engine
from automaton import scheduler as sched


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _register_noop_workflow(conn, target: Path):
    spec = {
        "name": "tick",
        "steps": [
            {"name": "log", "type": "file_append", "path": str(target), "text": "tick"},
        ],
    }
    engine.register_workflow(conn, spec)


def test_lock_is_exclusive(store):
    assert sched.acquire_lock(store, "A", lease_seconds=5)
    assert not sched.acquire_lock(store, "B", lease_seconds=5)
    sched.release_lock(store, "A")
    assert sched.acquire_lock(store, "B", lease_seconds=5)


def test_lock_expires(store):
    assert sched.acquire_lock(store, "A", lease_seconds=1)
    time.sleep(1.2)
    # A's lease has expired - B can take over.
    assert sched.acquire_lock(store, "B", lease_seconds=5)


def test_holder_can_renew_own_lock(store):
    assert sched.acquire_lock(store, "A", lease_seconds=5)
    # Same holder re-acquires - extends the lease.
    assert sched.acquire_lock(store, "A", lease_seconds=5)


def test_register_cron_advances_next_fire(store, tmp_path):
    target = tmp_path / "out.log"
    _register_noop_workflow(store, target)
    # Every minute - well in the future.
    tid = sched.register_cron(store, "tick", "*/1 * * * *")
    rows = sched.list_crons(store)
    assert len(rows) == 1
    assert rows[0]["id"] == tid
    # next_fire_at should be in the future
    nf = rows[0]["next_fire_at"]
    now_iso = sched._iso(sched._utcnow())
    assert nf >= now_iso  # string compare works for ISO format


def test_fire_due_crons_skips_when_nothing_due(store, tmp_path):
    target = tmp_path / "out.log"
    _register_noop_workflow(store, target)
    sched.register_cron(store, "tick", "*/1 * * * *")
    fired = sched.fire_due_crons(store)
    assert fired == 0


def test_fire_due_crons_fires_overdue(store, tmp_path):
    target = tmp_path / "out.log"
    _register_noop_workflow(store, target)
    sched.register_cron(store, "tick", "*/1 * * * *")
    # Backdate the trigger so it's due.
    store.execute(
        "UPDATE cron_trigger SET next_fire_at = ?",
        (sched._iso(sched._utcnow() - timedelta(minutes=1)),),
    )
    fired = sched.fire_due_crons(store)
    assert fired == 1

    # next_fire_at should have advanced to a future time.
    row = sched.list_crons(store)[0]
    assert row["next_fire_at"] > sched._iso(sched._utcnow())
    assert row["last_fire_at"] is not None

    # A run should have been created.
    runs = engine.list_runs(store)
    assert len(runs) == 1
    assert runs[0]["workflow"] == "tick"


def test_two_schedulers_one_run(tmp_path):
    """Two schedulers racing on the same overdue trigger - exactly one run.

    We open two independent connections (the real-world setup), both pointed
    at the same SQLite file. They contend for the leader row, and only one
    is allowed to fire the trigger.
    """
    db_path = tmp_path / "test.db"
    setup = _db.connect(db_path)
    _db.migrate(setup)
    _register_noop_workflow(setup, tmp_path / "out.log")
    # Yearly cadence: after the overdue fire, the next legitimate fire is
    # ~a year away. With */1 the 1s test window could straddle a real
    # minute boundary on a slow runner and fire a second (legitimate) run.
    sched.register_cron(setup, "tick", "0 0 1 1 *")
    setup.execute(
        "UPDATE cron_trigger SET next_fire_at = ?",
        (sched._iso(sched._utcnow() - timedelta(minutes=1)),),
    )

    # Launch two short scheduler loops in threads.
    def run_sched(name):
        c = _db.connect(db_path)
        sched.scheduler_loop(c, holder=name, lease_seconds=5,
                             poll_interval=0.05, stop_after_seconds=1.0)

    t1 = threading.Thread(target=run_sched, args=("S1",))
    t2 = threading.Thread(target=run_sched, args=("S2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    runs = engine.list_runs(setup)
    assert len(runs) == 1, f"expected 1 run, got {len(runs)}"
