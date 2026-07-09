"""The engine. Lease, execute, commit - all the consistency-critical code."""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import db as _db
from . import templating as _templating
from .steps import StepContext, StepError, StepWaiting, run_step


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


_SQL_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

log = logging.getLogger("automaton.engine")



def validate_spec(spec: dict) -> None:
    """Sanity-check a workflow spec before it goes into the DB.

    Raises ValueError with a clear message on:
      - missing top-level 'name' or 'steps'
      - empty steps list
      - duplicate step names
      - step without 'name' or 'type'
      - 'needs' referencing an unknown step
      - cycles in the DAG (a depends-on-b-depends-on-a chain)
    """
    if not isinstance(spec, dict):
        raise ValueError("workflow spec must be a dict")
    if "name" not in spec or not isinstance(spec["name"], str):
        raise ValueError("workflow spec missing 'name' (string)")
    if "steps" not in spec or not isinstance(spec["steps"], list):
        raise ValueError("workflow spec missing 'steps' (list)")
    if not spec["steps"]:
        raise ValueError("workflow has no steps")

    names: set[str] = set()
    for i, s in enumerate(spec["steps"]):
        if not isinstance(s, dict):
            raise ValueError(f"step #{i} is not a dict")
        if "name" not in s or not isinstance(s["name"], str):
            raise ValueError(f"step #{i} missing 'name'")
        if "type" not in s or not isinstance(s["type"], str):
            raise ValueError(f"step {s['name']!r} missing 'type'")
        if "when" in s and not isinstance(s.get("when"), str):
            raise ValueError(f"step {s['name']!r}: 'when' must be a string")
        env = s.get("env")
        if env is not None:
            if not isinstance(env, dict):
                raise ValueError(f"step {s['name']!r}: 'env' must be a dict")
            for k, v in env.items():
                if not isinstance(k, str):
                    raise ValueError(
                        f"step {s['name']!r}: 'env' key {k!r} must be a string"
                    )
                if not isinstance(v, str):
                    raise ValueError(
                        f"step {s['name']!r}: 'env[{k}]' value {v!r} must be a string"
                    )
        if s["name"] in names:
            raise ValueError(f"duplicate step name {s['name']!r}")
        names.add(s["name"])

    for s in spec["steps"]:
        for dep in s.get("needs", []) or []:
            if dep not in names:
                raise ValueError(
                    f"step {s['name']!r} needs unknown step {dep!r}"
                )

    # Cycle detection via DFS three-color algorithm.
    graph = {s["name"]: list(s.get("needs", []) or []) for s in spec["steps"]}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    def visit(n: str, path: list[str]):
        color[n] = GRAY
        for d in graph[n]:
            if color[d] == GRAY:
                cycle = path[path.index(d):] + [d]
                raise ValueError(f"cycle in workflow DAG: {' -> '.join(cycle)}")
            if color[d] == WHITE:
                visit(d, path + [d])
        color[n] = BLACK
    for n in graph:
        if color[n] == WHITE:
            visit(n, [n])

    # Workflow-level timezone (used as default for cron triggers and for
    # documentation). Validate it here so register_workflow refuses bad
    # zones before they hit the scheduler.
    tz = spec.get("timezone")
    if tz is not None:
        from . import scheduler as _sched
        try:
            _sched._validate_timezone(tz)
        except ValueError as e:
            raise ValueError(f"workflow timezone: {e}") from e

    # Required trigger-payload inputs.
    inputs = spec.get("inputs")
    if inputs is not None:
        if not isinstance(inputs, list):
            raise ValueError("workflow 'inputs' must be a list of strings")
        for i, item in enumerate(inputs):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"workflow 'inputs[{i}]' must be a non-empty string, got {item!r}"
                )


def register_workflow(conn: sqlite3.Connection, spec: dict) -> int:
    validate_spec(spec)
    name = spec["name"]
    with _db.transaction(conn):
        cur = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM workflow_def WHERE name = ?",
            (name,),
        )
        next_version = cur.fetchone()["v"] + 1
        timeout_seconds = spec.get("timeout_seconds")
        cur = conn.execute(
            "INSERT INTO workflow_def (name, version, spec_json, timeout_seconds) "
            "VALUES (?, ?, ?, ?)",
            (name, next_version, _db.to_json(spec), timeout_seconds),
        )
        return cur.lastrowid


def _latest_workflow(conn: sqlite3.Connection, name: str):
    row = conn.execute(
        "SELECT * FROM workflow_def WHERE name = ? ORDER BY version DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no workflow named {name!r}")
    return row


def trigger_run(conn, workflow_name, trigger_kind="manual", trigger_payload=None):
    """Public entry. Opens its own transaction. Use _trigger_run_locked()
    if you're already inside a transaction (e.g. from the scheduler)."""
    with _db.transaction(conn):
        return _trigger_run_locked(conn, workflow_name, trigger_kind, trigger_payload)


def _trigger_run_locked(conn, workflow_name, trigger_kind, trigger_payload):
    """Same as trigger_run but assumes the caller holds an open transaction."""
    wf = _latest_workflow(conn, workflow_name)
    spec = _db.from_json(wf["spec_json"])

    # Validate required inputs before creating the run.
    required = spec.get("inputs") or []
    if required:
        payload_keys = set((trigger_payload or {}).keys())
        missing = [k for k in required if k not in payload_keys]
        if missing:
            raise ValueError(
                f"workflow {workflow_name!r} requires inputs that are missing "
                f"from the trigger payload: {', '.join(missing)}"
            )

    cur = conn.execute(
        "INSERT INTO run (workflow_def_id, status, trigger_kind, trigger_payload) "
        "VALUES (?, 'running', ?, ?)",
        (wf["id"], trigger_kind, _db.to_json(trigger_payload)),
    )
    run_id = cur.lastrowid
    _seed_runnable_steps(conn, run_id, spec)
    _log_event(conn, run_id, "run.started",
               {"workflow": workflow_name, "version": wf["version"]})
    return run_id


def _seed_runnable_steps(conn, run_id, spec):
    steps_by_name = {s["name"]: s for s in spec["steps"]}
    for s in spec["steps"]:
        for dep in s.get("needs", []):
            if dep not in steps_by_name:
                raise ValueError(f"step {s['name']!r} depends on unknown step {dep!r}")
    for s in spec["steps"]:
        if not s.get("needs"):
            _create_and_queue_step(conn, run_id, s, attempt=1)


def _create_and_queue_step(conn, run_id, step_spec, attempt, ready_at=None):
    name = step_spec["name"]
    idem = _idempotency_key(run_id, name, attempt)
    cur = conn.execute(
        "INSERT INTO step (run_id, name, attempt, status, input_json, idempotency_key) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (run_id, name, attempt, _db.to_json(step_spec), idem),
    )
    step_id = cur.lastrowid
    if ready_at is None:
        conn.execute("INSERT INTO queue (step_id) VALUES (?)", (step_id,))
    else:
        conn.execute("INSERT INTO queue (step_id, ready_at) VALUES (?, ?)",
                     (step_id, ready_at))
    return step_id


def _idempotency_key(run_id, step_name, attempt):
    raw = f"{run_id}|{step_name}|{attempt}".encode()
    return hashlib.sha256(raw).hexdigest()


def worker_loop(conn, worker_id=None, lease_seconds=30, poll_interval=0.5,
                stop_when_idle=False, crash_after=None):
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    while True:
        step_id = lease_one(conn, worker_id, lease_seconds)
        if step_id is None:
            if stop_when_idle:
                return
            time.sleep(poll_interval)
            continue
        execute_and_commit(conn, step_id, worker_id, crash_after=crash_after)


def lease_one(conn, worker_id, lease_seconds):
    now = _utcnow()
    until = _iso(now + timedelta(seconds=lease_seconds))
    is_pg = getattr(conn, "is_postgres", False)
    with _db.transaction(conn):
        if is_pg:
            # Postgres: SELECT … FOR UPDATE SKIP LOCKED atomically claims the
            # row; no optimistic-lock retry needed.  The PgConn wrapper
            # translates the ? placeholders and datetime() calls automatically.
            row = conn.execute(
                "SELECT step_id FROM queue "
                "WHERE ready_at <= NOW() "
                "  AND (leased_by IS NULL OR leased_until < NOW()) "
                "ORDER BY ready_at LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if row is None:
                return None
            step_id = row["step_id"]
            conn.execute(
                "UPDATE queue SET leased_by = ?, leased_until = ? "
                "WHERE step_id = ?",
                (worker_id, until, step_id),
            )
        else:
            # SQLite: optimistic lock — SELECT then UPDATE with a re-check in
            # the WHERE clause; rowcount == 0 means a concurrent worker won.
            row = conn.execute(
                "SELECT step_id FROM queue "
                f"WHERE ready_at <= {_SQL_NOW} "
                f"  AND (leased_by IS NULL OR leased_until < {_SQL_NOW}) "
                "ORDER BY ready_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            step_id = row["step_id"]
            cur = conn.execute(
                "UPDATE queue SET leased_by = ?, leased_until = ? "
                "WHERE step_id = ? "
                f"  AND (leased_by IS NULL OR leased_until < {_SQL_NOW})",
                (worker_id, until, step_id),
            )
            if cur.rowcount == 0:
                return None
        conn.execute(
            "UPDATE step SET status = 'running', started_at = datetime('now') WHERE id = ?",
            (step_id,),
        )
        log.info("leased step id=%s worker=%s", step_id, worker_id)
        return step_id


def _is_truthy(value) -> bool:
    """Evaluate a resolved when: value as a boolean.

    Booleans and ints use Python truthiness directly. Strings treat the
    canonical false words ("false", "no", "off", "0", "") as False and
    everything else as True. None is False.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "no", "off", "0")
    # lists, dicts: non-empty is truthy
    return bool(value)


def execute_and_commit(conn, step_id, worker_id, crash_after=None):
    step_row = conn.execute("SELECT * FROM step WHERE id = ?", (step_id,)).fetchone()
    raw_spec = _db.from_json(step_row["input_json"])
    try:
        spec, secret_values = _templating.resolve_spec(
            conn, step_row["run_id"], raw_spec
        )
    except _templating.TemplateError as e:
        # Treat unresolved templates as a step failure so retry policy applies.
        commit_step(conn, step_id, "failed", None,
                    {"type": "TemplateError", "message": str(e)})
        return

    # Evaluate optional when: condition.  A falsy resolved value skips the
    # step without failing the run; downstream successors are still queued.
    when_val = spec.get("when")
    if when_val is not None:
        if not _is_truthy(when_val):
            commit_step(conn, step_id, "skipped", None, None)
            return

    ctx = StepContext(
        run_id=step_row["run_id"],
        step_name=step_row["name"],
        attempt=step_row["attempt"],
        conn=conn,
    )

    try:
        output = run_step(spec, idempotency_key=step_row["idempotency_key"], context=ctx)
        error = None
        status = "completed"
    except StepWaiting as w:
        # Step isn't done. Release the lease, set future ready_at, don't commit.
        # Same step row, same idempotency key - the engine doesn't treat this
        # as a new attempt because the side effect (if any) hasn't fired yet.
        _requeue_waiting_step(conn, step_id, w.retry_after_seconds, w.reason)
        return
    except StepError as e:
        output = None
        error = {"type": type(e).__name__, "message": str(e), "details": e.details}
        status = "failed"

    if crash_after is not None and step_row["name"] == crash_after:
        raise SystemExit(f"simulated crash after side effect of step {crash_after!r}")

    commit_step(conn, step_id, status, output, error, secret_values=secret_values)


def _requeue_waiting_step(conn, step_id, delay_seconds, reason):
    """Put a parked step back on the queue with future ready_at and no lease.
    Step row goes back to 'pending'. Same row, same idempotency key."""
    ready_at = _iso(_utcnow() + timedelta(seconds=delay_seconds))
    with _db.transaction(conn):
        conn.execute(
            "UPDATE step SET status = 'pending' WHERE id = ?", (step_id,)
        )
        conn.execute(
            "UPDATE queue SET leased_by = NULL, leased_until = NULL, "
            "  ready_at = ? WHERE step_id = ?",
            (ready_at, step_id),
        )
        step = conn.execute(
            "SELECT run_id, name FROM step WHERE id = ?", (step_id,)
        ).fetchone()
        _log_event(conn, step["run_id"], "step.waiting",
                   {"name": step["name"], "retry_after_seconds": delay_seconds,
                    "reason": reason})
    log.info("step waiting id=%s reason=%s requeued ready_at=%s",
             step_id, reason or "(none)", ready_at)



def _compute_backoff(retry: dict, current_attempt: int) -> float:
    """Seconds to wait before the next attempt.

    retry spec:
      max: int                    (default: 1 - no retries)
      backoff: 'fixed' | 'exponential' (default: 'fixed')
      initial_seconds: float      (default: 1.0)
    """
    initial = float(retry.get("initial_seconds", 1.0))
    kind = retry.get("backoff", "fixed")
    if kind == "exponential":
        return initial * (2 ** (current_attempt - 1))
    return initial


def _maybe_schedule_retry(conn, failed_step) -> bool:
    """If the step's retry policy permits, create the next attempt row and
    queue it for future execution. Returns True if a retry was scheduled."""
    spec = _db.from_json(failed_step["input_json"])
    retry = spec.get("retry") or {}
    max_attempts = int(retry.get("max", 1))
    if failed_step["attempt"] >= max_attempts:
        return False
    backoff = _compute_backoff(retry, failed_step["attempt"])
    ready_at = _iso(_utcnow() + timedelta(seconds=backoff))
    next_attempt = failed_step["attempt"] + 1
    _create_and_queue_step(conn, failed_step["run_id"], spec,
                           attempt=next_attempt, ready_at=ready_at)
    _log_event(
        conn, failed_step["run_id"], "step.retry_scheduled",
        {"name": failed_step["name"],
         "next_attempt": next_attempt,
         "ready_at": ready_at,
         "backoff_seconds": backoff},
    )
    log.info("scheduled retry attempt=%s of step name=%s run=%s after %ss",
             next_attempt, failed_step["name"], failed_step["run_id"], backoff)
    return True


def _scrub(value, secret_values):
    """Walk a JSON-able value and replace any embedded secret strings."""
    if not secret_values:
        return value
    from . import secrets as _secrets
    if isinstance(value, str):
        return _secrets.redact(value, secret_values)
    if isinstance(value, dict):
        return {k: _scrub(v, secret_values) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secret_values) for v in value]
    return value


def commit_step(conn, step_id, status, output, error, secret_values=None):
    # Scrub any secret values that might've leaked into output/error before
    # they hit the DB (and from there the event log, the UI, backups...).
    if secret_values:
        output = _scrub(output, secret_values)
        error = _scrub(error, secret_values)
    with _db.transaction(conn):
        conn.execute(
            "UPDATE step SET status = ?, output_json = ?, error_json = ?, "
            "  finished_at = datetime('now') WHERE id = ?",
            (status, _db.to_json(output), _db.to_json(error), step_id),
        )
        conn.execute("DELETE FROM queue WHERE step_id = ?", (step_id,))

        step_row = conn.execute("SELECT * FROM step WHERE id = ?", (step_id,)).fetchone()
        run_id = step_row["run_id"]

        _log_event(conn, run_id, f"step.{status}",
                   {"name": step_row["name"], "attempt": step_row["attempt"]})
        log.info("step %s id=%s name=%s run=%s",
                 status, step_id, step_row["name"], run_id)

        if status in ("completed", "skipped"):
            _enqueue_newly_ready_successors(conn, run_id, step_row["name"])
        elif status == "failed":
            _maybe_schedule_retry(conn, step_row)

        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM step WHERE run_id = ? "
            "AND status IN ('pending','running')",
            (run_id,),
        ).fetchone()["c"]
        if remaining == 0:
            # The run is failed only if some step name's FINAL attempt failed.
            # Earlier failed attempts that were superseded by successful retries
            # don't count toward run failure.
            any_failed = conn.execute(
                "SELECT COUNT(DISTINCT name) AS c FROM step s1 "
                "WHERE run_id = ? AND status = 'failed' "
                "  AND attempt = (SELECT MAX(attempt) FROM step s2 "
                "                 WHERE s2.run_id = s1.run_id AND s2.name = s1.name)",
                (run_id,),
            ).fetchone()["c"]
            final = "failed" if any_failed else "completed"
            conn.execute(
                "UPDATE run SET status = ?, finished_at = datetime('now') WHERE id = ?",
                (final, run_id),
            )
            _log_event(conn, run_id, f"run.{final}", None)
            _try_notify_terminal(conn, run_id)


def _enqueue_newly_ready_successors(conn, run_id, just_completed):
    run_row = conn.execute(
        "SELECT workflow_def_id FROM run WHERE id = ?", (run_id,)
    ).fetchone()
    wf = conn.execute(
        "SELECT spec_json FROM workflow_def WHERE id = ?", (run_row["workflow_def_id"],)
    ).fetchone()
    spec = _db.from_json(wf["spec_json"])

    completed_names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM step WHERE run_id = ? AND status IN ('completed', 'skipped')",
            (run_id,),
        )
    }

    for s in spec["steps"]:
        if just_completed not in s.get("needs", []):
            continue
        exists = conn.execute(
            "SELECT 1 FROM step WHERE run_id = ? AND name = ?", (run_id, s["name"])
        ).fetchone()
        if exists:
            continue
        if all(dep in completed_names for dep in s.get("needs", [])):
            _create_and_queue_step(conn, run_id, s, attempt=1)


def _try_notify_terminal(conn, run_id):
    """Fire-and-forget notification on terminal-run transitions.

    Imported locally so the engine doesn't pay the apprise import cost
    on every step. Any failure is logged at WARNING and swallowed - the
    cost of getting this wrong is "I missed an alert," not "my run is
    stuck."
    """
    try:
        from . import notify as _notify
        _notify.dispatch_for_run(conn, run_id)
    except Exception as e:
        log.warning("notify dispatch failed for run %s: %s", run_id, e)


def _log_event(conn, run_id, kind, payload):
    conn.execute(
        "INSERT INTO event_log (run_id, kind, payload_json) VALUES (?, ?, ?)",
        (run_id, kind, _db.to_json(payload)),
    )




def list_workflows(conn):
    """Return the latest version of every registered workflow definition.

    Each entry includes the parsed spec so callers can inspect steps,
    timeout, etc. without a separate query.
    """
    rows = conn.execute(
        "SELECT id, name, version, spec_json, timeout_seconds "
        "FROM workflow_def w1 "
        "WHERE version = ("
        "  SELECT MAX(version) FROM workflow_def w2 WHERE w2.name = w1.name"
        ") "
        "ORDER BY name",
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["spec"] = _db.from_json(d.pop("spec_json"))
        result.append(d)
    return result

def list_runs(conn, limit=20):
    rows = conn.execute(
        "SELECT r.id, w.name AS workflow, r.status, r.started_at, r.finished_at "
        "FROM run r JOIN workflow_def w ON r.workflow_def_id = w.id "
        "ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]



def search_runs(conn, *, status=None, workflow=None, after=None, before=None, limit=50):
    """Filter runs by status, workflow name, and/or date range.

    Parameters
    ----------
    status   : str or None — one of the valid run status values, e.g. "failed"
    workflow : str or None — exact workflow name (case-sensitive)
    after    : str or None — ISO-8601 datetime; only runs started after this
    before   : str or None — ISO-8601 datetime; only runs started before this
    limit    : int — max rows to return (default 50)

    Returns a list of dicts newest-first, same shape as list_runs().
    """
    clauses = []
    params: list = []
    if status is not None:
        clauses.append("r.status = ?")
        params.append(status)
    if workflow is not None:
        clauses.append("w.name = ?")
        params.append(workflow)
    if after is not None:
        clauses.append("r.started_at > ?")
        params.append(after)
    if before is not None:
        clauses.append("r.started_at < ?")
        params.append(before)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = conn.execute(
        "SELECT r.id, w.name AS workflow, r.status, r.started_at, r.finished_at "
        f"FROM run r JOIN workflow_def w ON r.workflow_def_id = w.id "
        f"{where} ORDER BY r.id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]

def run_detail(conn, run_id):
    run = conn.execute(
        "SELECT r.*, w.name AS workflow, w.version "
        "FROM run r JOIN workflow_def w ON r.workflow_def_id = w.id "
        "WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise KeyError(f"no run {run_id}")
    steps = conn.execute(
        "SELECT name, attempt, status, started_at, finished_at, output_json, error_json "
        "FROM step WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    events = conn.execute(
        "SELECT id, ts, kind, payload_json FROM event_log WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()

    def _parse_step(row):
        d = dict(row)
        # Parse output_json / error_json from raw JSON strings to Python
        # objects so callers (UI, API consumers, tests) get structured data.
        # Always pop both keys (even when NULL) so callers only see "output"/"error".
        raw_out = d.pop("output_json", None)
        raw_err = d.pop("error_json", None)
        d["output"] = _db.from_json(raw_out) if raw_out else None
        d["error"] = _db.from_json(raw_err) if raw_err else None
        return d

    return {
        "run": dict(run),
        "steps": [_parse_step(s) for s in steps],
        "events": [dict(e) for e in events],
    }


def send_signal(conn, run_id: int, name: str, payload=None) -> int:
    """Queue a signal for the given run. Returns the signal row id.

    Signals are durable. A wait_for_signal step polling on this (run_id, name)
    will pick it up on its next tick (default poll: 5s) and consume it.
    """
    with _db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO signal (run_id, name, payload_json) VALUES (?, ?, ?)",
            (run_id, name, _db.to_json(payload)),
        )
        _log_event(conn, run_id, "signal.sent",
                   {"name": name, "signal_id": cur.lastrowid})
        log.info("signal sent run=%s name=%s id=%s", run_id, name, cur.lastrowid)
        return cur.lastrowid

def cancel_run(conn, run_id: int, reason: Optional[str] = None) -> bool:
    """Cancel an in-flight run. Sets run.status='cancelled', marks all
    pending/running steps cancelled, removes their queue entries. Returns
    True if the run was actually cancelled; False if it was already terminal
    or doesn't exist."""
    with _db.transaction(conn):
        row = conn.execute(
            "SELECT id, status FROM run WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None or row["status"] not in ("pending", "running"):
            return False
        conn.execute(
            "DELETE FROM queue WHERE step_id IN ("
            "  SELECT id FROM step WHERE run_id = ? "
            "    AND status IN ('pending', 'running'))",
            (run_id,),
        )
        conn.execute(
            "UPDATE step SET status = 'cancelled', finished_at = datetime('now') "
            "WHERE run_id = ? AND status IN ('pending', 'running')",
            (run_id,),
        )
        conn.execute(
            "UPDATE run SET status = 'cancelled', finished_at = datetime('now') "
            "WHERE id = ?", (run_id,),
        )
        _log_event(conn, run_id, "run.cancelled", {"reason": reason})
    log.info("cancelled run=%s reason=%s", run_id, reason or "(none)")
    return True


def reap_timed_out_runs(conn) -> int:
    """Mark runs that have exceeded their workflow's timeout_seconds as timed_out.

    Called by the scheduler on each tick. A run is timed_out when:
      - run.status IN ('pending', 'running')
      - workflow_def.timeout_seconds IS NOT NULL
      - (julianday('now') - julianday(run.started_at)) * 86400 > timeout_seconds

    Steps belonging to the run are marked 'cancelled' and removed from the
    queue, exactly as cancel_run does. The notify hook fires for each reaped
    run so the operator gets an alert.

    Returns the number of runs reaped.
    """
    # Find candidate run IDs in one query; reap them one at a time so each
    # gets its own transaction and its own notify call.
    candidates = conn.execute(
        "SELECT r.id "
        "FROM run r "
        "JOIN workflow_def wf ON wf.id = r.workflow_def_id "
        "WHERE r.status IN ('pending', 'running') "
        "  AND wf.timeout_seconds IS NOT NULL "
        "  AND (julianday('now') - julianday(r.started_at)) * 86400 "
        "      > wf.timeout_seconds"
    ).fetchall()

    reaped = 0
    for row in candidates:
        run_id = row["id"]
        with _db.transaction(conn):
            # Re-check inside the transaction; another worker may have
            # finished the run between the SELECT above and now.
            current = conn.execute(
                "SELECT status FROM run WHERE id = ?", (run_id,)
            ).fetchone()
            if current is None or current["status"] not in ("pending", "running"):
                continue
            # Cancel outstanding steps + queue entries.
            conn.execute(
                "DELETE FROM queue WHERE step_id IN ("
                "  SELECT id FROM step WHERE run_id = ? "
                "    AND status IN ('pending', 'running'))",
                (run_id,),
            )
            conn.execute(
                "UPDATE step SET status = 'cancelled', finished_at = datetime('now') "
                "WHERE run_id = ? AND status IN ('pending', 'running')",
                (run_id,),
            )
            conn.execute(
                "UPDATE run SET status = 'timed_out', finished_at = datetime('now') "
                "WHERE id = ?",
                (run_id,),
            )
            _log_event(conn, run_id, "run.timed_out", None)
        log.warning("timed_out run=%s", run_id)
        _try_notify_terminal(conn, run_id)
        reaped += 1
    return reaped


def doctor(conn) -> list[dict]:
    """Run a suite of health checks against the database and engine state.

    Returns a list of result dicts, each with:
      check  (str)  — dot-separated check name
      status (str)  — "ok" | "warn" | "fail"
      detail (str)  — human-readable description

    Does not raise; all exceptions are caught and returned as "fail" items.
    """
    results: list[dict] = []

    def _add(check, status, detail):
        results.append({"check": check, "status": status, "detail": detail})

    # ── db.reachable ────────────────────────────────────────────────────────
    try:
        conn.execute("SELECT 1").fetchone()
        _add("db.reachable", "ok", "database is accessible")
    except Exception as e:
        _add("db.reachable", "fail", f"cannot query database: {e}")
        # Can't run any other checks without DB access
        return results

    # ── db.migrations ───────────────────────────────────────────────────────
    try:
        # Check that the yoyo _yoyo_migration table exists and count applied migrations
        applied = conn.execute(
            "SELECT COUNT(*) AS c FROM _yoyo_migration"
        ).fetchone()["c"]
        # Count migration files in the migrations/ directory (excluding test files)
        from pathlib import Path as _Path
        mig_dir = _Path(__file__).with_name("migrations")
        core_files = sorted(
            f for f in mig_dir.glob("*.sql")
            if not any(t in f.name for t in ("test", "fake", "snapshot", "gate"))
        )
        expected = len(core_files)
        if applied >= expected:
            _add("db.migrations", "ok",
                 f"all {expected} core migrations applied ({applied} total)")
        else:
            _add("db.migrations", "warn",
                 f"{applied} migrations applied, expected at least {expected}. "
                 "Run 'automaton migrate' to update.")
    except Exception as e:
        _add("db.migrations", "fail", f"could not check migrations: {e}")

    # ── db.integrity ────────────────────────────────────────────────────────
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result == "ok":
            _add("db.integrity", "ok", "PRAGMA integrity_check: ok")
        else:
            _add("db.integrity", "fail", f"PRAGMA integrity_check: {result}")
    except Exception as e:
        _add("db.integrity", "fail", f"integrity check failed: {e}")

    # ── db.wal_mode ─────────────────────────────────────────────────────────
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode == "wal":
            _add("db.wal_mode", "ok", "WAL mode enabled")
        else:
            _add("db.wal_mode", "warn",
                 f"journal_mode={mode!r}; WAL mode is recommended for concurrency")
    except Exception as e:
        _add("db.wal_mode", "fail", f"could not check journal mode: {e}")

    # ── queue.stuck_steps ───────────────────────────────────────────────────
    try:
        # A step is "stuck" if it has been leased but the lease expired over
        # 5 minutes ago. This suggests a dead worker.
        stuck = conn.execute(
            "SELECT COUNT(*) AS c FROM queue "
            "WHERE leased_by IS NOT NULL "
            "  AND leased_until < datetime('now', '-5 minutes')"
        ).fetchone()["c"]
        if stuck == 0:
            _add("queue.stuck_steps", "ok", "no stuck steps in queue")
        else:
            _add("queue.stuck_steps", "warn",
                 f"{stuck} step(s) appear stuck (lease expired >5 min ago). "
                 "Check that workers are running.")
    except Exception as e:
        _add("queue.stuck_steps", "fail", f"could not check queue: {e}")

    # ── workflows.valid ─────────────────────────────────────────────────────
    try:
        wfs = list_workflows(conn)
        if not wfs:
            _add("workflows.valid", "ok", "no workflows registered")
        else:
            invalid = []
            for wf in wfs:
                try:
                    validate_spec(wf["spec"])
                except (ValueError, KeyError) as e:
                    invalid.append(f"{wf['name']!r}: {e}")
            if not invalid:
                _add("workflows.valid", "ok",
                     f"{len(wfs)} workflow(s) registered, all valid")
            else:
                _add("workflows.valid", "fail",
                     f"{len(invalid)} invalid workflow spec(s): "
                     + "; ".join(invalid))
    except Exception as e:
        _add("workflows.valid", "fail", f"could not validate workflows: {e}")


    # -- crons.valid --------------------------------------------------
    try:
        rows = conn.execute(
            "SELECT workflow_name AS name, cron_expr "
            "FROM cron_trigger WHERE enabled = 1"
        ).fetchall()
        if not rows:
            _add("crons.valid", "ok", "no cron triggers registered")
        else:
            from croniter import croniter as _croniter
            bad = []
            for r in rows:
                name_val = r["name"]
                expr_val = r["cron_expr"]
                if not _croniter.is_valid(expr_val):
                    bad.append(f"{name_val}={expr_val!r}")
            if bad:
                _add("crons.valid", "fail",
                     "invalid cron expressions: " + ", ".join(bad))
            else:
                _add("crons.valid", "ok",
                     f"{len(rows)} cron trigger(s) all valid")
    except Exception as e:
        _add("crons.valid", "fail", f"could not validate crons: {e}")

    return results
