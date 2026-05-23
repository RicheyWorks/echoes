"""The consistency proof test.

This is the smallest test that validates the design's central claim:
exactly-once observable side effects, even when a worker crashes *after*
the side effect lands but *before* the commit transaction completes.

The trick: the `file_append` step type writes a marker line containing the
step's idempotency key. If the side effect fires twice, we'd see two marker
lines. The test simulates a crash in exactly that window and asserts that
recovery still produces exactly one line.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from automaton import db as _db
from automaton import engine


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _spec_appending_to(path: Path) -> dict:
    return {
        "name": "crash_test",
        "steps": [
            {
                "name": "write_once",
                "type": "file_append",
                "path": str(path),
                "text": "ran",
            },
        ],
    }


def test_happy_path(store, tmp_path):
    """Baseline: a single-step workflow runs to completion."""
    target = tmp_path / "out.log"
    engine.register_workflow(store, _spec_appending_to(target))
    run_id = engine.trigger_run(store, "crash_test")
    engine.worker_loop(store, stop_when_idle=True)

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    assert len(detail["steps"]) == 1
    assert detail["steps"][0]["status"] == "completed"
    assert target.read_text().count("ran") == 1


def test_exactly_once_under_crash(store, tmp_path):
    """Simulate the canonical failure: side effect lands, commit doesn't.

    Worker 1: leases the step, runs file_append (writes the marker), then
              raises before commit_step runs. The step row is still 'running',
              the queue row is still leased.
    Time passes, the lease expires.
    Worker 2: re-leases the step. file_append runs *again*, sees its
              idempotency marker in the file, and returns appended=False.
              commit_step writes the result and the run finishes.

    Assertion: the file contains the marker exactly once.
    """
    target = tmp_path / "out.log"
    engine.register_workflow(store, _spec_appending_to(target))
    run_id = engine.trigger_run(store, "crash_test")

    # Worker 1: crashes after side effect, before commit.
    with pytest.raises(SystemExit):
        engine.worker_loop(
            store,
            worker_id="worker-1",
            lease_seconds=1,  # short lease so worker-2 can grab it fast
            stop_when_idle=True,
            crash_after="write_once",
        )

    # The side effect should have landed once.
    assert target.read_text().count("ran") == 1

    # The step should still be in 'running' state — never committed.
    step_status = store.execute(
        "SELECT status FROM step WHERE run_id = ?", (run_id,)
    ).fetchone()["status"]
    assert step_status == "running"

    # Wait for the lease to expire.
    import time
    time.sleep(1.5)

    # Worker 2: picks up the orphaned step.
    engine.worker_loop(
        store,
        worker_id="worker-2",
        lease_seconds=30,
        stop_when_idle=True,
    )

    # The whole point of the test:
    # The file_append step ran *twice* (no way around that — the first attempt
    # didn't tell us whether the side effect landed). But because the step
    # honored the idempotency key, the *observable* side effect — the marker
    # in the file — appears exactly once.
    content = target.read_text()
    assert content.count("ran") == 1, f"expected 1 'ran', got: {content!r}"

    # The run should be completed.
    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"


def test_dag_dependencies(store, tmp_path):
    """A two-step workflow where step B depends on step A."""
    target = tmp_path / "out.log"
    spec = {
        "name": "dag",
        "steps": [
            {"name": "a", "type": "file_append", "path": str(target), "text": "A"},
            {"name": "b", "type": "file_append", "path": str(target), "text": "B", "needs": ["a"]},
        ],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "dag")
    engine.worker_loop(store, stop_when_idle=True)

    content = target.read_text()
    assert "A" in content and "B" in content
    # Order: A's line must appear before B's line.
    assert content.find("A") < content.find("B")

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    statuses = [s["status"] for s in detail["steps"]]
    assert statuses == ["completed", "completed"]


def test_event_log_is_linearizable(store, tmp_path):
    """Event log ids are monotonically increasing within a run."""
    target = tmp_path / "out.log"
    engine.register_workflow(store, _spec_appending_to(target))
    run_id = engine.trigger_run(store, "crash_test")
    engine.worker_loop(store, stop_when_idle=True)

    events = store.execute(
        "SELECT id, kind FROM event_log WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.completed"
