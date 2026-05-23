"""Retry policy tests.

What we prove here:
  1. A step with retry: {max: 3} that fails twice then succeeds completes
     in 3 attempts, the run finishes 'completed', and there are exactly
     3 step rows for that name.
  2. Each attempt gets a fresh idempotency key (per design §5).
  3. Exhausting retries leaves the run 'failed'.
  4. Successors of a retried step only enqueue after the final attempt
     completes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from automaton import db as _db
from automaton import engine
from automaton.steps import StepError, step_type, _STEP_TYPES


# --- a step type that fails N times then succeeds, keyed by (run_id, name) ---

_FLAKY_STATE: dict[tuple[int, str], int] = {}


@step_type("flaky")
def _flaky(spec, idempotency_key):
    """Fails the first `fail_first` times it's invoked for a given (run, step),
    then succeeds. Counts invocations in module state - reset between tests."""
    run_id = spec["run_id"]
    name = spec["step_name"]
    fail_first = int(spec["fail_first"])
    k = (run_id, name)
    seen = _FLAKY_STATE.get(k, 0) + 1
    _FLAKY_STATE[k] = seen
    if seen <= fail_first:
        raise StepError(f"flaky: attempt {seen} fails by design")
    return {"succeeded_on_invocation": seen, "key": idempotency_key}


@pytest.fixture(autouse=True)
def reset_flaky():
    _FLAKY_STATE.clear()
    yield
    _FLAKY_STATE.clear()


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def test_retries_succeed_on_third_attempt(store):
    """Workflow with retry max=3 succeeds when the step fails twice first."""
    spec = {
        "name": "retry_ok",
        "steps": [{
            "name": "fetch",
            "type": "flaky",
            "run_id": 1,           # we know run_id will be 1 (fresh DB)
            "step_name": "fetch",
            "fail_first": 2,
            "retry": {"max": 3, "backoff": "fixed", "initial_seconds": 0.0},
        }],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "retry_ok")
    engine.worker_loop(store, stop_when_idle=True)

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    # Three step rows for the same name, attempts 1, 2, 3
    statuses = [(s["name"], s["attempt"], s["status"]) for s in detail["steps"]]
    assert statuses == [
        ("fetch", 1, "failed"),
        ("fetch", 2, "failed"),
        ("fetch", 3, "completed"),
    ]


def test_retry_idempotency_key_rotates(store):
    """Each attempt's idempotency_key must differ - the previous attempt's
    side effect is presumed not to have landed (it raised), so the next
    attempt should fire fresh from external systems' point of view."""
    spec = {
        "name": "retry_key",
        "steps": [{
            "name": "fetch",
            "type": "flaky",
            "run_id": 1,
            "step_name": "fetch",
            "fail_first": 1,
            "retry": {"max": 2, "backoff": "fixed", "initial_seconds": 0.0},
        }],
    }
    engine.register_workflow(store, spec)
    engine.trigger_run(store, "retry_key")
    engine.worker_loop(store, stop_when_idle=True)
    keys = [r["idempotency_key"] for r in store.execute(
        "SELECT idempotency_key FROM step WHERE name = 'fetch' ORDER BY attempt"
    )]
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_retries_exhausted_run_fails(store):
    """If the step keeps failing past max attempts, the run is 'failed'."""
    spec = {
        "name": "retry_exhaust",
        "steps": [{
            "name": "fetch",
            "type": "flaky",
            "run_id": 1,
            "step_name": "fetch",
            "fail_first": 99,   # never succeeds within our retry budget
            "retry": {"max": 2, "backoff": "fixed", "initial_seconds": 0.0},
        }],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "retry_exhaust")
    engine.worker_loop(store, stop_when_idle=True)
    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "failed"
    statuses = [s["status"] for s in detail["steps"]]
    assert statuses == ["failed", "failed"]


def test_successor_waits_for_final_attempt(store, tmp_path):
    """Step B depends on step A. A fails once then succeeds. B should run
    exactly once, after A's successful attempt, not after A's failure."""
    target = tmp_path / "out.log"
    spec = {
        "name": "retry_chain",
        "steps": [
            {
                "name": "fetch",
                "type": "flaky",
                "run_id": 1,
                "step_name": "fetch",
                "fail_first": 1,
                "retry": {"max": 2, "initial_seconds": 0.0},
            },
            {
                "name": "after",
                "type": "file_append",
                "needs": ["fetch"],
                "path": str(target),
                "text": "ran",
            },
        ],
    }
    engine.register_workflow(store, spec)
    engine.trigger_run(store, "retry_chain")
    engine.worker_loop(store, stop_when_idle=True)
    # 'after' should appear exactly once
    assert target.read_text().count("ran") == 1
    after_rows = store.execute(
        "SELECT attempt, status FROM step WHERE name = 'after'"
    ).fetchall()
    assert [(r["attempt"], r["status"]) for r in after_rows] == [(1, "completed")]


def test_no_retry_field_default_no_retry(store):
    """Backward compat: a step without 'retry' fails once and stops."""
    spec = {
        "name": "no_retry",
        "steps": [{
            "name": "fetch",
            "type": "flaky",
            "run_id": 1,
            "step_name": "fetch",
            "fail_first": 5,
        }],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "no_retry")
    engine.worker_loop(store, stop_when_idle=True)
    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "failed"
    # Only one step row - no retries
    assert len(detail["steps"]) == 1
