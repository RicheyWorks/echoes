"""Cancel and validation tests."""
from __future__ import annotations

import threading
import time

import pytest

from automaton import db as _db
from automaton import engine


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


# --- cancel ---

def test_cancel_running_run(store):
    """An in-flight run can be cancelled; its pending step transitions to
    cancelled, its queue row is removed, and run.status is cancelled."""
    engine.register_workflow(store, {
        "name": "long",
        "steps": [
            {"name": "park", "type": "wait_for_signal",
             "signal": "never", "poll_seconds": 999, "timeout_seconds": 9999},
        ],
    })
    run_id = engine.trigger_run(store, "long")

    # Cancel before any worker leases the step
    assert engine.cancel_run(store, run_id, reason="operator override") is True

    run = store.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "cancelled"

    steps = store.execute(
        "SELECT name, status FROM step WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert all(s["status"] == "cancelled" for s in steps)

    queue_rows = store.execute(
        "SELECT COUNT(*) AS c FROM queue WHERE step_id IN "
        "(SELECT id FROM step WHERE run_id = ?)", (run_id,)
    ).fetchone()["c"]
    assert queue_rows == 0


def test_cancel_already_terminal_returns_false(store, tmp_path):
    engine.register_workflow(store, {
        "name": "quick",
        "steps": [{"name": "noop", "type": "file_append",
                   "path": str(tmp_path / "noop.log"), "text": "x"}],
    })
    rid = engine.trigger_run(store, "quick")
    engine.worker_loop(store, stop_when_idle=True)
    # Already completed; cancel returns False.
    assert engine.cancel_run(store, rid) is False
    # And a second cancel of an already-cancelled run also returns False.
    engine.register_workflow(store, {
        "name": "park",
        "steps": [{"name": "p", "type": "wait_for_signal",
                   "signal": "x", "poll_seconds": 999}],
    })
    rid2 = engine.trigger_run(store, "park")
    assert engine.cancel_run(store, rid2) is True
    assert engine.cancel_run(store, rid2) is False


def test_cancel_unknown_run_returns_false(store):
    assert engine.cancel_run(store, 9999) is False


def test_cancel_event_logged(store):
    engine.register_workflow(store, {
        "name": "park2",
        "steps": [{"name": "p", "type": "wait_for_signal",
                   "signal": "x", "poll_seconds": 999}],
    })
    rid = engine.trigger_run(store, "park2")
    engine.cancel_run(store, rid, reason="testing")
    kinds = [r["kind"] for r in store.execute(
        "SELECT kind FROM event_log WHERE run_id = ? ORDER BY id", (rid,)
    )]
    assert "run.cancelled" in kinds


# --- validation ---

def test_validate_missing_name():
    with pytest.raises(ValueError, match="missing 'name'"):
        engine.validate_spec({"steps": [{"name": "x", "type": "file_append"}]})


def test_validate_missing_steps():
    with pytest.raises(ValueError, match="missing 'steps'"):
        engine.validate_spec({"name": "wf"})


def test_validate_empty_steps():
    with pytest.raises(ValueError, match="no steps"):
        engine.validate_spec({"name": "wf", "steps": []})


def test_validate_step_missing_type():
    with pytest.raises(ValueError, match="missing 'type'"):
        engine.validate_spec({"name": "wf", "steps": [{"name": "x"}]})


def test_validate_duplicate_step_names():
    with pytest.raises(ValueError, match="duplicate step name"):
        engine.validate_spec({
            "name": "wf",
            "steps": [
                {"name": "x", "type": "file_append"},
                {"name": "x", "type": "file_append"},
            ],
        })


def test_validate_broken_needs():
    with pytest.raises(ValueError, match="unknown step 'gone'"):
        engine.validate_spec({
            "name": "wf",
            "steps": [
                {"name": "a", "type": "file_append"},
                {"name": "b", "type": "file_append", "needs": ["gone"]},
            ],
        })


def test_validate_cycle():
    with pytest.raises(ValueError, match="cycle"):
        engine.validate_spec({
            "name": "wf",
            "steps": [
                {"name": "a", "type": "file_append", "needs": ["b"]},
                {"name": "b", "type": "file_append", "needs": ["a"]},
            ],
        })


def test_validate_valid_dag_passes():
    # Should NOT raise
    engine.validate_spec({
        "name": "wf",
        "steps": [
            {"name": "a", "type": "file_append"},
            {"name": "b", "type": "file_append", "needs": ["a"]},
            {"name": "c", "type": "file_append", "needs": ["a", "b"]},
        ],
    })


def test_register_workflow_calls_validation(store):
    """register_workflow refuses bad specs without writing them to the DB."""
    bad = {"name": "wf"}  # missing steps
    with pytest.raises(ValueError):
        engine.register_workflow(store, bad)
    # Nothing should have been inserted
    count = store.execute(
        "SELECT COUNT(*) AS c FROM workflow_def WHERE name = 'wf'"
    ).fetchone()["c"]
    assert count == 0
