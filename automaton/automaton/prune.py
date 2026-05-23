"""Event-log + run pruning.

For multi-year deployments. Deletes terminal runs older than the threshold and
all their associated rows (steps, queue entries, event log, signals). Active
runs (status in {'pending','running'}) are NEVER touched, regardless of age.

A `dry_run` mode reports what would go without actually deleting.

The schema doesn't use ON DELETE CASCADE - we do explicit ordered deletes
inside one transaction so the operation is atomic and the FK constraints
stay simple.

VACUUM is optional and reclaims disk space. It must run outside any
transaction, so we do it last and only on explicit request.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from . import db as _db

log = logging.getLogger("automaton.prune")


def _summarize(conn: sqlite3.Connection, cutoff_iso: str) -> dict:
    """Count what would be pruned."""
    return {
        "runs": conn.execute(
            "SELECT COUNT(*) AS c FROM run "
            "WHERE status IN ('completed','failed','cancelled') "
            "  AND COALESCE(finished_at, started_at) < ?",
            (cutoff_iso,),
        ).fetchone()["c"],
        "events": conn.execute(
            "SELECT COUNT(*) AS c FROM event_log "
            "WHERE run_id IN (SELECT id FROM run "
            "                  WHERE status IN ('completed','failed','cancelled') "
            "                    AND COALESCE(finished_at, started_at) < ?)",
            (cutoff_iso,),
        ).fetchone()["c"],
        "steps": conn.execute(
            "SELECT COUNT(*) AS c FROM step "
            "WHERE run_id IN (SELECT id FROM run "
            "                  WHERE status IN ('completed','failed','cancelled') "
            "                    AND COALESCE(finished_at, started_at) < ?)",
            (cutoff_iso,),
        ).fetchone()["c"],
        "signals": conn.execute(
            "SELECT COUNT(*) AS c FROM signal "
            "WHERE run_id IN (SELECT id FROM run "
            "                  WHERE status IN ('completed','failed','cancelled') "
            "                    AND COALESCE(finished_at, started_at) < ?)",
            (cutoff_iso,),
        ).fetchone()["c"],
        "cutoff": cutoff_iso,
    }


def prune(
    conn: sqlite3.Connection,
    older_than_days: float,
    dry_run: bool = False,
    vacuum: bool = False,
) -> dict:
    """Delete terminal runs older than `older_than_days`.

    Returns a summary dict with row counts. With dry_run=True, nothing is
    actually deleted - the counts represent what WOULD be deleted.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")

    # cutoff = now - N days, formatted to match the schema's text timestamps.
    cutoff_iso = conn.execute(
        "SELECT datetime('now', ?) AS c", (f"-{older_than_days} days",)
    ).fetchone()["c"]

    summary = _summarize(conn, cutoff_iso)
    summary["dry_run"] = dry_run

    if dry_run or summary["runs"] == 0:
        log.info("prune dry_run=%s cutoff=%s would delete: %s runs, %s steps, "
                 "%s events, %s signals", dry_run, cutoff_iso,
                 summary["runs"], summary["steps"],
                 summary["events"], summary["signals"])
        return summary

    # Real delete - inside one transaction. Order matters: children first.
    with _db.transaction(conn):
        # signals reference both run and step, so delete first
        conn.execute(
            "DELETE FROM signal WHERE run_id IN (SELECT id FROM run "
            "  WHERE status IN ('completed','failed','cancelled') "
            "    AND COALESCE(finished_at, started_at) < ?)",
            (cutoff_iso,),
        )
        # queue references step
        conn.execute(
            "DELETE FROM queue WHERE step_id IN (SELECT id FROM step "
            "  WHERE run_id IN (SELECT id FROM run "
            "    WHERE status IN ('completed','failed','cancelled') "
            "      AND COALESCE(finished_at, started_at) < ?))",
            (cutoff_iso,),
        )
        conn.execute(
            "DELETE FROM event_log WHERE run_id IN (SELECT id FROM run "
            "  WHERE status IN ('completed','failed','cancelled') "
            "    AND COALESCE(finished_at, started_at) < ?)",
            (cutoff_iso,),
        )
        conn.execute(
            "DELETE FROM step WHERE run_id IN (SELECT id FROM run "
            "  WHERE status IN ('completed','failed','cancelled') "
            "    AND COALESCE(finished_at, started_at) < ?)",
            (cutoff_iso,),
        )
        conn.execute(
            "DELETE FROM run WHERE status IN ('completed','failed','cancelled') "
            "  AND COALESCE(finished_at, started_at) < ?",
            (cutoff_iso,),
        )

    log.info("pruned cutoff=%s: %s runs, %s steps, %s events, %s signals",
             cutoff_iso, summary["runs"], summary["steps"],
             summary["events"], summary["signals"])

    if vacuum:
        # VACUUM must run outside a transaction.
        conn.execute("VACUUM")
        summary["vacuumed"] = True
        log.info("vacuumed database")

    return summary
