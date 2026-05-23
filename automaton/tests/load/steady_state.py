"""Steady-state load: N runs/sec, M workers, T seconds.

Reports p50/p95/p99 lease-to-completion duration, ending queue depth,
and DB size growth. Usage::

    python -m tests.load.steady_state --rate 50 --workers 4 --seconds 30

Calling ``run(rate, workers, seconds, db_path=None)`` returns a dict
suitable for asserting against in tests.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from automaton import db as _db
from automaton import engine

from .harness import Timings, run_workers_until_idle, db_size_bytes


_NOOP_WORKFLOW = {
    "name": "noop",
    "steps": [
        # file_append is idempotent + cheap. Path is per-run so writes
        # don't contend on the same file.
        {"name": "n", "type": "file_append",
         "path": "${{ run.payload.path }}", "text": "x"},
    ],
}


def run(rate: int = 50, workers: int = 4, seconds: int = 30,
        db_path: str | None = None, log_dir: str | None = None) -> dict:
    tmpdir_obj = None
    if db_path is None:
        tmpdir_obj = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmpdir_obj.name, "load.db")
    if log_dir is None:
        log_dir = tempfile.mkdtemp(prefix="load-logs-")

    conn = _db.connect(db_path)
    _db.migrate(conn)
    engine.register_workflow(conn, _NOOP_WORKFLOW)

    def open_conn():
        return _db.connect(db_path)

    # Producer: enqueue at the requested rate for T seconds.
    enqueued = 0
    stop = threading.Event()

    def producer():
        nonlocal enqueued
        pconn = open_conn()  # per-thread connection (sqlite3 is not thread-safe)
        try:
            interval = 1.0 / rate
            next_at = time.perf_counter()
            while not stop.is_set() and (time.perf_counter() - prod_start) < seconds:
                now = time.perf_counter()
                if now >= next_at:
                    engine.trigger_run(
                        pconn, "noop",
                        trigger_kind="load",
                        trigger_payload={"path": os.path.join(log_dir, f"r-{enqueued}.log")},
                    )
                    enqueued += 1
                    next_at = now + interval
                time.sleep(min(0.001, max(0.0, next_at - now)))
        finally:
            pconn.close()

    # Worker threads
    worker_threads = []
    worker_stop = threading.Event()

    def worker():
        c = open_conn()
        try:
            while not worker_stop.is_set():
                engine.worker_loop(c, stop_when_idle=True)
                time.sleep(0.005)
        finally:
            c.close()

    for _ in range(workers):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        worker_threads.append(t)

    prod_start = time.perf_counter()
    p_thread = threading.Thread(target=producer, daemon=True)
    p_thread.start()
    p_thread.join()
    stop.set()

    # Let the workers drain the rest of the queue.
    drain_start = time.perf_counter()
    poll = open_conn()
    try:
        while poll.execute("SELECT COUNT(*) FROM queue").fetchone()[0] > 0:
            if time.perf_counter() - drain_start > 60:
                break  # safety
            time.sleep(0.01)
    finally:
        poll.close()

    worker_stop.set()
    for t in worker_threads:
        t.join(timeout=2.0)

    # Compute per-step durations from the step table.
    durations = Timings(label="step_duration_ms")
    rows = conn.execute(
        "SELECT (julianday(finished_at) - julianday(started_at)) * 86400000 AS ms "
        "FROM step WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
    ).fetchall()
    for r in rows:
        if r["ms"] is not None and r["ms"] >= 0:
            durations.add(r["ms"])

    completed = conn.execute(
        "SELECT COUNT(*) FROM run WHERE status = 'completed'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM run WHERE status = 'failed'"
    ).fetchone()[0]
    remaining = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    db_size = db_size_bytes(db_path)

    elapsed = time.perf_counter() - prod_start
    report = {
        "rate_target": rate,
        "workers": workers,
        "seconds": seconds,
        "enqueued": enqueued,
        "completed": completed,
        "failed": failed,
        "queue_remaining": remaining,
        "wall_seconds": round(elapsed, 2),
        "throughput_runs_per_sec": round(completed / max(elapsed, 1e-6), 2),
        "step_duration": durations.summary(),
        "db_size_bytes": db_size,
    }
    conn.close()
    if tmpdir_obj is not None:
        tmpdir_obj.cleanup()
    return report


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--rate", type=int, default=50)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--seconds", type=int, default=30)
    args = p.parse_args()
    print(json.dumps(run(args.rate, args.workers, args.seconds), indent=2))


if __name__ == "__main__":
    _cli()
