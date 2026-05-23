"""Pruning tests.

Cover:
  1. Old terminal runs are deleted; their step/event/signal/queue rows go too.
  2. Recent runs (within threshold) are preserved.
  3. In-flight runs (pending/running) are NEVER deleted, regardless of age.
  4. dry_run reports counts without deleting anything.
  5. older_than_days=0 prunes everything terminal that exists right now.
"""
from __future__ import annotations

import pytest

from automaton import db as _db
from automaton import engine
from automaton import prune


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _seed_completed_run(conn, name="wf", days_ago=0):
    """Create a completed run + step + event_log, with timestamps pushed back."""
    engine.register_workflow(conn, {
        "name": name,
        "steps": [{"name": "noop", "type": "file_append", "path": "/dev/null"}],
    })
    run_id = engine.trigger_run(conn, name)
    engine.worker_loop(conn, stop_when_idle=True)
    # Re-time the run and its events to days_ago. We rely on text comparison
    # against the cutoff, which uses the same format.
    if days_ago > 0:
        ts = conn.execute(
            "SELECT datetime('now', ?) AS t", (f"-{days_ago} days",)
        ).fetchone()["t"]
        conn.execute("UPDATE run SET started_at = ?, finished_at = ? "
                     "WHERE id = ?", (ts, ts, run_id))
    return run_id


def test_old_runs_pruned(store):
    rid = _seed_completed_run(store, "old", days_ago=100)
    assert _row_count(store, "run", rid) == 1
    assert _row_count(store, "step WHERE run_id =", rid) == 1
    assert _row_count(store, "event_log WHERE run_id =", rid) >= 2

    summary = prune.prune(store, older_than_days=30)
    assert summary["runs"] == 1
    assert summary["steps"] >= 1
    assert summary["events"] >= 2
    assert _row_count(store, "run", rid) == 0
    assert _row_count(store, "step WHERE run_id =", rid) == 0
    assert _row_count(store, "event_log WHERE run_id =", rid) == 0


def test_recent_runs_kept(store):
    rid = _seed_completed_run(store, "fresh", days_ago=1)
    summary = prune.prune(store, older_than_days=30)
    assert summary["runs"] == 0
    assert _row_count(store, "run", rid) == 1


def test_inflight_runs_never_pruned(store):
    """A pending/running run, no matter how old, stays."""
    engine.register_workflow(store, {
        "name": "stuck",
        "steps": [{"name": "p", "type": "wait_for_signal", "signal": "x",
                   "poll_seconds": 999}],
    })
    rid = engine.trigger_run(store, "stuck")
    # Force its started_at to long ago
    store.execute("UPDATE run SET started_at = datetime('now', '-365 days') "
                  "WHERE id = ?", (rid,))
    summary = prune.prune(store, older_than_days=30)
    assert summary["runs"] == 0
    assert _row_count(store, "run", rid) == 1


def test_dry_run_reports_without_deleting(store):
    rid = _seed_completed_run(store, "old", days_ago=100)
    summary = prune.prune(store, older_than_days=30, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["runs"] == 1
    # Still there - dry run didn't delete
    assert _row_count(store, "run", rid) == 1


def test_threshold_zero_prunes_terminal_now(store):
    _seed_completed_run(store, "now1", days_ago=0)
    _seed_completed_run(store, "now2", days_ago=0)
    # Move 'now' timestamps slightly into the past so the strict < comparison works
    store.execute("UPDATE run SET finished_at = datetime('now', '-1 seconds')")
    summary = prune.prune(store, older_than_days=0)
    assert summary["runs"] == 2


def test_signals_cascade(store):
    """A pruned run's signals are also deleted (FK would otherwise complain)."""
    engine.register_workflow(store, {
        "name": "sigwf",
        "steps": [{"name": "park", "type": "wait_for_signal",
                   "signal": "go", "poll_seconds": 0.01}],
    })
    rid = engine.trigger_run(store, "sigwf")
    engine.send_signal(store, rid, "go", {"x": 1})
    engine.worker_loop(store, stop_when_idle=True, poll_interval=0.01)
    # Backdate
    store.execute("UPDATE run SET finished_at = datetime('now', '-100 days')")

    assert _row_count(store, "signal WHERE run_id =", rid) >= 1
    summary = prune.prune(store, older_than_days=30)
    assert summary["signals"] >= 1
    assert _row_count(store, "signal WHERE run_id =", rid) == 0


def _row_count(conn, where: str, rid=None):
    """Tiny helper: count rows. 'where' can be a table name or 'table WHERE col =' (then rid is bound)."""
    if rid is None:
        q = f"SELECT COUNT(*) AS c FROM {where} WHERE id = ?"
        # Treat 'where' as table name; rid is bound to id
        return -1  # not used in this file
    if " WHERE " not in where:
        q = f"SELECT COUNT(*) AS c FROM {where} WHERE id = ?"
    else:
        q = f"SELECT COUNT(*) AS c FROM {where} ?"
    return conn.execute(q, (rid,)).fetchone()["c"]
