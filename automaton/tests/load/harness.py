"""Shared utilities for the load scripts."""
from __future__ import annotations

import os
import statistics
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, List


@dataclass
class Timings:
    """Collect per-event durations in ms; report percentiles."""
    samples: List[float] = field(default_factory=list)
    label: str = "duration"

    def add(self, ms: float):
        self.samples.append(ms)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return float("nan")
        return statistics.quantiles(sorted(self.samples), n=100)[int(p) - 1] \
            if len(self.samples) >= 100 else max(self.samples)

    def summary(self) -> dict:
        if not self.samples:
            return {"count": 0}
        ss = sorted(self.samples)
        n = len(ss)
        return {
            "count": n,
            "min_ms": round(ss[0], 2),
            "p50_ms": round(ss[n // 2], 2),
            "p95_ms": round(ss[min(int(n * 0.95), n - 1)], 2),
            "p99_ms": round(ss[min(int(n * 0.99), n - 1)], 2),
            "max_ms": round(ss[-1], 2),
            "mean_ms": round(sum(ss) / n, 2),
        }


@contextmanager
def timed(timings: Timings):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings.add((time.perf_counter() - t0) * 1000.0)


def run_workers_until_idle(open_conn: Callable, worker_count: int,
                            stop_after_seconds: float = 60.0) -> float:
    """Spin up ``worker_count`` worker threads against the given DB and
    wait for the queue to drain. Returns wall-clock seconds elapsed.

    Each thread opens its own connection (SQLite WAL handles concurrent
    readers, and writes serialize naturally). ``open_conn`` returns a
    new connection.
    """
    from automaton import engine

    stop = threading.Event()
    start = time.perf_counter()

    def worker():
        conn = open_conn()
        try:
            while not stop.is_set():
                if (time.perf_counter() - start) > stop_after_seconds:
                    return
                # stop_when_idle returns when queue is empty; we use a
                # short sleep loop so the thread can re-check for new
                # work pushed during the run.
                engine.worker_loop(conn, stop_when_idle=True)
                time.sleep(0.01)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(worker_count)]
    for t in threads:
        t.start()

    # Wait for drainage: poll the queue table from a separate connection.
    poll = open_conn()
    try:
        while time.perf_counter() - start < stop_after_seconds:
            n = poll.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            if n == 0:
                # One more grace iteration to ensure threads have
                # completed in-flight steps.
                time.sleep(0.05)
                n = poll.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
                if n == 0:
                    break
            time.sleep(0.01)
    finally:
        poll.close()

    stop.set()
    for t in threads:
        # Generous join: on Windows an alive thread's open connection makes
        # the tempdir cleanup fail with WinError 32.
        t.join(timeout=15.0)
    return time.perf_counter() - start


def db_size_bytes(db_path) -> int:
    return os.path.getsize(os.fspath(db_path))


def queue_depth(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
