"""Long-tail load: short + long steps interleaved.

Tests that short steps don't get starved by concurrent long-running
ones. Catches priority-inversion bugs in lease ordering.

Usage::

    python -m tests.load.long_tail --short 50 --long 5 --workers 4

``run(short_count, long_count, workers, db_path=None)`` returns a dict
with separate timing summaries for short vs long step durations.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from automaton import db as _db
from automaton import engine

from .harness import Timings, run_workers_until_idle, db_size_bytes


_SHORT_WORKFLOW = {
    "name": "shortie",
    "steps": [{
        "name": "fast",
        "type": "shell",
        # python -c '' is the fastest cross-platform "do nothing" we have.
        "cmd": [sys.executable, "-c", "pass"],
    }],
}

_LONG_WORKFLOW = {
    "name": "slowie",
    "steps": [{
        "name": "slow",
        "type": "shell",
        # Sleeps for ~1s
        "cmd": [sys.executable, "-c", "import time; time.sleep(1)"],
    }],
}


def run(short_count: int = 50, long_count: int = 5, workers: int = 4,
        db_path: str | None = None) -> dict:
    tmpdir_obj = None
    if db_path is None:
        tmpdir_obj = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmpdir_obj.name, "longtail.db")

    conn = _db.connect(db_path)
    _db.migrate(conn)
    engine.register_workflow(conn, _SHORT_WORKFLOW)
    engine.register_workflow(conn, _LONG_WORKFLOW)

    # Interleave: a couple of long ones then a bunch of short ones, etc.
    short_left = short_count
    long_left = long_count
    while short_left or long_left:
        if long_left:
            engine.trigger_run(conn, "slowie", trigger_kind="longtail")
            long_left -= 1
        if short_left:
            engine.trigger_run(conn, "shortie", trigger_kind="longtail")
            short_left -= 1
        if short_left:
            # Bias toward shorts so they outnumber longs (catches
            # starvation more obviously).
            engine.trigger_run(conn, "shortie", trigger_kind="longtail")
            short_left -= 1

    def open_conn():
        return _db.connect(db_path)

    drain_seconds = run_workers_until_idle(
        open_conn, workers, stop_after_seconds=180.0,
    )

    short_times = Timings(label="short_ms")
    long_times = Timings(label="long_ms")
    rows = conn.execute(
        "SELECT s.name AS step_name, w.name AS wf_name, "
        "       (julianday(s.finished_at) - julianday(s.started_at)) * 86400000 AS ms "
        "FROM step s JOIN run r ON r.id = s.run_id "
        "JOIN workflow_def w ON w.id = r.workflow_def_id "
        "WHERE s.finished_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        if row["ms"] is None or row["ms"] < 0:
            continue
        if row["wf_name"] == "shortie":
            short_times.add(row["ms"])
        elif row["wf_name"] == "slowie":
            long_times.add(row["ms"])

    report = {
        "short_count": short_count,
        "long_count": long_count,
        "workers": workers,
        "drain_seconds": round(drain_seconds, 3),
        "short": short_times.summary(),
        "long": long_times.summary(),
        "db_size_bytes": db_size_bytes(db_path),
    }
    conn.close()
    if tmpdir_obj is not None:
        tmpdir_obj.cleanup()
    return report


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--short", type=int, default=50)
    p.add_argument("--long", type=int, default=5)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()
    print(json.dumps(run(args.short, args.long, args.workers), indent=2))


if __name__ == "__main__":
    _cli()
