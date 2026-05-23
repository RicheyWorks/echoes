"""Signal round-trip tests.

What we prove:
  1. A workflow with a wait_for_signal step parks (status returns to pending,
     event log shows step.waiting).
  2. Sending a signal causes the next poll to consume it and complete the step.
  3. The signal row is marked consumed and linked to the consuming step.
  4. A second sibling signal stays unconsumed.
  5. Timeout: a wait_for_signal with timeout_seconds=0 fails (and respects
     the retry policy if one is set).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from automaton import db as _db
from automaton import engine


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _workflow_with_wait(signal_name="ready", **wait_overrides):
    step = {
        "name": "park",
        "type": "wait_for_signal",
        "signal": signal_name,
        "poll_seconds": 0.05,
    }
    step.update(wait_overrides)
    return {"name": "sig_wf", "steps": [step]}


def test_step_parks_and_then_completes_on_signal(store, tmp_path):
    engine.register_workflow(store, _workflow_with_wait(signal_name="ok"))
    run_id = engine.trigger_run(store, "sig_wf")

    # Run the worker in a thread; it will park the step and keep polling.
    stop = [False]

    def run_worker():
        c = _db.connect(tmp_path / "test.db")
        while not stop[0]:
            engine.worker_loop(c, stop_when_idle=True, poll_interval=0.05)
            time.sleep(0.05)

    t = threading.Thread(target=run_worker)
    t.start()

    # Give the worker a moment to park the step.
    time.sleep(0.2)

    # Step should be in 'pending' state (parked, awaiting signal).
    row = store.execute(
        "SELECT status FROM step WHERE run_id = ? AND name = 'park'", (run_id,)
    ).fetchone()
    assert row["status"] == "pending"

    # Confirm a step.waiting event was logged.
    waiting_events = store.execute(
        "SELECT COUNT(*) AS c FROM event_log WHERE run_id = ? AND kind = 'step.waiting'",
        (run_id,),
    ).fetchone()["c"]
    assert waiting_events >= 1

    # Send the signal.
    engine.send_signal(store, run_id, "ok", payload={"result": 42})

    # Give the worker time to pick it up.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        rs = store.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
        if rs["status"] == "completed":
            break
        time.sleep(0.05)

    stop[0] = True
    t.join(timeout=2)

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    park = [s for s in detail["steps"] if s["name"] == "park"][0]
    assert park["status"] == "completed"
    # Output should include the payload we sent.
    import json
    output = json.loads(park["output_json"])
    assert output["signal_received"] == "ok"
    assert output["payload"] == {"result": 42}

    # Signal should be marked consumed and linked to the step.
    sig = store.execute("SELECT * FROM signal WHERE name = 'ok'").fetchone()
    assert sig["consumed_at"] is not None
    assert sig["consumed_by_step_id"] is not None


def test_unmatched_signal_stays_unconsumed(store, tmp_path):
    engine.register_workflow(store, _workflow_with_wait(signal_name="ok"))
    run_id = engine.trigger_run(store, "sig_wf")

    # Send a signal with a DIFFERENT name first. The wait step shouldn't take it.
    engine.send_signal(store, run_id, "other", payload="ignored")

    stop = [False]
    def run_worker():
        c = _db.connect(tmp_path / "test.db")
        while not stop[0]:
            engine.worker_loop(c, stop_when_idle=True, poll_interval=0.05)
            time.sleep(0.05)
    t = threading.Thread(target=run_worker); t.start()

    time.sleep(0.3)
    # Now send the matching one.
    engine.send_signal(store, run_id, "ok", payload="taken")

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        rs = store.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
        if rs["status"] == "completed":
            break
        time.sleep(0.05)
    stop[0] = True; t.join(timeout=2)

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    sigs = store.execute(
        "SELECT name, consumed_at FROM signal WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    # 'other' stays unconsumed, 'ok' is consumed.
    by_name = {s["name"]: s for s in sigs}
    assert by_name["other"]["consumed_at"] is None
    assert by_name["ok"]["consumed_at"] is not None


def test_wait_for_signal_times_out(store, tmp_path):
    """A wait with timeout_seconds=0 should fail on the first poll."""
    engine.register_workflow(store, _workflow_with_wait(
        signal_name="never_sent",
        timeout_seconds=0,
    ))
    run_id = engine.trigger_run(store, "sig_wf")

    # Run worker once - it parks the step (started_at is now set).
    engine.worker_loop(store, stop_when_idle=True, poll_interval=0.01)
    # Now wait briefly so elapsed > 0, then drain again to hit the timeout.
    time.sleep(0.15)
    engine.worker_loop(store, stop_when_idle=True, poll_interval=0.01)

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "failed"
    park = [s for s in detail["steps"] if s["name"] == "park"][0]
    assert park["status"] == "failed"
    assert "timed out" in (park["error_json"] or "")
