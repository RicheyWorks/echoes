"""Prometheus metrics exposition (text format 0.0.4).

Exposes operational counters and gauges that a Prometheus scraper (or
any tool that understands the text format — Grafana Agent, VictoriaMetrics,
even plain curl) can read from ``GET /metrics``.

No external dependency: the format is simple enough to generate by hand.
The full spec is at https://prometheus.io/docs/instrumenting/exposition_formats/

Metrics exported:

    automaton_runs_total{status=...}
        Counter of runs that have reached each terminal status.
        Labels: completed, failed, cancelled, timed_out.

    automaton_runs_active{status=...}
        Gauge of runs currently in a non-terminal state.
        Labels: running, pending.

    automaton_queue_depth
        Gauge: number of steps currently waiting in the step queue.

    automaton_cron_triggers{enabled=...}
        Gauge of registered cron triggers by enabled state.
        Labels: "true", "false".

    automaton_db_size_bytes
        Gauge: live size of the SQLite database file in bytes.
        Omitted if the db_path is None or the file can't be stat'd
        (e.g. in-memory DB used by tests).

Design notes:

* The endpoint is intentionally open (no auth required), matching the
  ``/healthz`` convention. Prometheus scrapers rarely carry bearer tokens;
  if you need to protect /metrics, put a reverse proxy in front.

* All queries run in a single read transaction on the caller's connection.
  No writes, no schema changes.

* The format is text/plain with a fixed Content-Type header so Prometheus
  auto-detects the version.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _line(name: str, labels: dict, value: int | float) -> str:
    """Render one Prometheus sample line."""
    if labels:
        pairs = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{pairs}}} {value}"
    return f"{name} {value}"


def _gauge(name: str, help_text: str, samples: list[tuple[dict, int | float]]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        lines.append(_line(name, labels, value))
    return lines


def _counter(name: str, help_text: str,
              samples: list[tuple[dict, int | float]]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
    for labels, value in samples:
        lines.append(_line(name, labels, value))
    return lines


def collect(conn: sqlite3.Connection,
            db_path: Optional[str | Path] = None) -> str:
    """Generate a Prometheus text-format scrape payload.

    Args:
        conn:    An open SQLite connection to the automaton DB.
        db_path: Filesystem path to the DB file. When provided,
                 ``automaton_db_size_bytes`` is included. Pass None
                 (or omit) for in-memory / test DBs.

    Returns:
        A UTF-8 string in Prometheus exposition format 0.0.4.
    """
    out: list[str] = []

    # ------------------------------------------------------------------ #
    # automaton_runs_total — terminal statuses only (counter semantics)   #
    # ------------------------------------------------------------------ #
    terminal = ("completed", "failed", "cancelled", "timed_out")
    counts: dict[str, int] = {s: 0 for s in terminal}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM run "
        "WHERE status IN ('completed','failed','cancelled','timed_out') "
        "GROUP BY status"
    ):
        counts[row["status"]] = row["n"]

    out += _counter(
        "automaton_runs_total",
        "Total runs that have reached each terminal status.",
        [({"status": s}, counts[s]) for s in terminal],
    )
    out.append("")

    # ------------------------------------------------------------------ #
    # automaton_runs_active — non-terminal (gauge)                        #
    # ------------------------------------------------------------------ #
    active_statuses = ("running", "pending")
    active: dict[str, int] = {s: 0 for s in active_statuses}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM run "
        "WHERE status IN ('running','pending') GROUP BY status"
    ):
        active[row["status"]] = row["n"]

    out += _gauge(
        "automaton_runs_active",
        "Runs currently in a non-terminal state.",
        [({"status": s}, active[s]) for s in active_statuses],
    )
    out.append("")

    # ------------------------------------------------------------------ #
    # automaton_queue_depth (gauge)                                       #
    # ------------------------------------------------------------------ #
    depth = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    out += _gauge(
        "automaton_queue_depth",
        "Number of steps currently waiting in the step queue.",
        [({}, depth)],
    )
    out.append("")

    # ------------------------------------------------------------------ #
    # automaton_cron_triggers (gauge)                                     #
    # ------------------------------------------------------------------ #
    cron_on  = conn.execute("SELECT COUNT(*) FROM cron_trigger WHERE enabled=1").fetchone()[0]
    cron_off = conn.execute("SELECT COUNT(*) FROM cron_trigger WHERE enabled=0").fetchone()[0]
    out += _gauge(
        "automaton_cron_triggers",
        "Registered cron triggers by enabled state.",
        [({"enabled": "true"}, cron_on), ({"enabled": "false"}, cron_off)],
    )
    out.append("")

    # ------------------------------------------------------------------ #
    # automaton_integrity_failures_total (counter)                        #
    #                                                                     #
    # echoes_agent verify steps that failed because the hash chain did    #
    # not verify. Matches the stable StepError message emitted by         #
    # steps.py's _parse_verify_output — see ADR-002 Phase 7a.             #
    # ------------------------------------------------------------------ #
    integ = conn.execute(
        "SELECT COUNT(*) FROM step "
        "WHERE status = 'failed' "
        "AND error_json LIKE '%hash-chain integrity FAILED%'"
    ).fetchone()[0]
    out += _counter(
        "automaton_integrity_failures_total",
        "echoes_agent verify steps that failed hash-chain integrity.",
        [({}, integ)],
    )
    out.append("")

    # ------------------------------------------------------------------ #
    # automaton_db_size_bytes (gauge) — omitted for in-memory DBs        #
    # ------------------------------------------------------------------ #
    if db_path is not None:
        try:
            size = os.path.getsize(os.fspath(db_path))
            out += _gauge(
                "automaton_db_size_bytes",
                "Size of the SQLite database file in bytes.",
                [({}, size)],
            )
            out.append("")
        except OSError:
            pass  # in-memory or missing — skip silently

    return "\n".join(out).rstrip() + "\n"
