"""Burst load: enqueue N workflows as fast as possible, measure drain.

Usage::

    python -m tests.load.burst --count 1000 --workers 4

``run(count, workers, db_path=None)`` returns a dict with enqueue time,
drain time, throughput, and the queue depth high-water mark.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time

from automaton import db as _db
from automaton import engine

from .harness import run_workers_until_idle, db_size_bytes


_BURST_WORKFLOW = {
    "name": "burst",
    "steps": [
        {"name": "n", "type": "file_append",
         "path": "${{ run.payload.path }}", "text": "x"},
    ],
}


def run(count: int = 1000, workers: int = 4,
        db_path: str | None = None,
        log_dir: str | None = None) -> dict:
    tmpdir_obj = None
    if db_path is None:
        tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db_path = os.path.join(tmpdir_obj.name, "burst.db")
    if log_dir is None:
        log_dir = tempfile.mkdtemp(prefix="burst-logs-")

    conn = _db.connect(db_path)
    _db.migrate(conn)
    engine.register_workflow(conn, _BURST_WORKFLOW)

    # ENQUEUE phase: workers haven't started yet. Time the producer
    # alone so we can attribute drain time only to worker throughput.
    t_enq_start = time.perf_counter()
    for i in range(count):
        engine.trigger_run(
            conn, "burst", trigger_kind="burst",
            trigger_payload={"path": os.path.join(log_dir, f"r-{i}.log")},
        )
    enq_seconds = time.perf_counter() - t_enq_start

    high_water = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    # DRAIN phase: kick the workers.
    def open_conn():
        return _db.connect(db_path)

    drain_seconds = run_workers_until_idle(
        open_conn, workers, stop_after_seconds=300.0,
    )

    completed = conn.execute(
        "SELECT COUNT(*) FROM run WHERE status = 'completed'"
    ).fetchone()[0]

    report = {
        "count": count,
        "workers": workers,
        "enqueue_seconds": round(enq_seconds, 3),
        "drain_seconds": round(drain_seconds, 3),
        "queue_high_water": high_water,
        "completed": completed,
        "throughput_runs_per_sec": round(
            completed / max(drain_seconds, 1e-6), 2
        ),
        "db_size_bytes": db_size_bytes(db_path),
    }
    conn.close()
    if tmpdir_obj is not None:
        tmpdir_obj.cleanup()
    return report


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()
    print(json.dumps(run(args.count, args.workers), indent=2))


if __name__ == "__main__":
    _cli()
