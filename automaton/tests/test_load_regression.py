"""Cheap load tests that run in CI to catch large regressions.

Strategy: keep the parameters small enough that a slow shared CI runner
finishes in <5s, but tight enough that a 5x slowdown gets caught.

For the actual operating envelope, run the full scripts under
``tests/load/`` on real hardware - the numbers are in
``docs/scale.md``. These tests are tripwires, not benchmarks.

Also asserts the production SQLite PRAGMAs survive any future change
to ``db.connect()``.
"""
from __future__ import annotations

import pytest

from automaton import db as _db
from tests.load import burst, long_tail


def test_pragmas_match_production_defaults(tmp_path):
    """db.connect() must apply WAL + busy_timeout + wal_autocheckpoint."""
    conn = _db.connect(tmp_path / "t.db")
    seen = _db.verify_pragmas(conn)
    # Each entry is (observed, expected) - compare them and report
    # specifically what's wrong.
    bad = [name for name, (obs, exp) in seen.items() if obs != exp.lower()]
    conn.close()
    assert not bad, f"PRAGMAs that drifted: {[(b, seen[b]) for b in bad]}"


def test_burst_drains_within_bound():
    """100 noop runs across 2 workers should finish in well under 5s.

    On a fresh laptop this completes in ~0.1s; in a heavily-loaded
    CI runner we still expect <5s. If this trips, something regressed
    enough to investigate.
    """
    report = burst.run(count=100, workers=2)
    assert report["completed"] == 100, report
    assert report["queue_high_water"] == 100, report
    assert report["drain_seconds"] < 5.0, \
        f"100 burst drained slower than 5s: {report}"


def test_long_tail_shorts_do_not_starve():
    """Short steps don't degrade by an order of magnitude when long
    steps are running. If their p95 approaches the long-step duration,
    lease ordering has a starvation bug.

    The long steps sleep ~1000 ms; the threshold here is intentionally
    loose (3000 ms) so a heavily-loaded CI runner with SQLite WAL
    pressure from prior tests doesn't trip a false positive. A real
    starvation regression would push shorts to multi-second range."""
    report = long_tail.run(short_count=10, long_count=2, workers=2)
    assert report["short"]["count"] == 10, report
    assert report["long"]["count"] == 2, report
    assert report["short"]["p95_ms"] < 3000, \
        f"shorts are getting starved: {report}"
