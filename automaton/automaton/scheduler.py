"""Cron scheduler with a single-leader pattern.

Design (from system-design doc, section 5):
- The scheduler claims a single-row 'lock' in scheduler_lock by atomic UPDATE.
- Only the row owner fires due cron triggers; other scheduler processes spin
  waiting for the lock to expire.
- Skip-on-miss semantics: if next_fire_at is in the past, we fire ONCE and
  advance to the next future tick. We do not backfill missed fires.

This keeps cron correctness aligned with the rest of the engine: even if you
accidentally run two scheduler processes, you cannot double-fire a trigger.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from croniter import croniter
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # py<3.9 not supported, but defensive
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

from . import db as _db
from . import engine

log = logging.getLogger("automaton.scheduler")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def _validate_timezone(tz_name: Optional[str]) -> None:
    """Raise ValueError if tz_name isn't a known IANA timezone."""
    if tz_name is None or tz_name == "":
        return
    if ZoneInfo is None:
        raise ValueError("zoneinfo not available on this Python")
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"unknown IANA timezone: {tz_name!r}") from e


def _next_fire(cron_expr: str, base: Optional[datetime] = None,
               tz: Optional[str] = None) -> datetime:
    """Return the next fire time as a tz-aware UTC datetime.

    When ``tz`` is given (IANA name like 'America/Los_Angeles'), the cron
    expression is interpreted in that timezone's wall-clock - so a
    'every day at 2:30' fires at 2:30 local time year-round, correctly
    handling DST transitions. Otherwise it's interpreted in UTC.
    """
    base = base or _utcnow()
    if tz:
        zi = ZoneInfo(tz)
        # Convert base into the target tz, then strip tz for croniter
        # (croniter operates on naive wall-clock times).
        local = base.astimezone(zi).replace(tzinfo=None)
        n = croniter(cron_expr, local).get_next(datetime)
        # Re-attach the tz, then convert back to UTC for storage.
        return n.replace(tzinfo=zi).astimezone(timezone.utc)
    # No tz: interpret in UTC.
    naive = base.replace(tzinfo=None)
    n = croniter(cron_expr, naive).get_next(datetime)
    return n.replace(tzinfo=timezone.utc)


def register_cron(conn: sqlite3.Connection, workflow_name: str,
                  cron_expr: str, timezone: Optional[str] = None) -> int:
    """Register a recurring trigger. Idempotent on (workflow_name, cron_expr).

    ``timezone`` is an optional IANA name. When set, the cron expression
    is interpreted in that timezone's local wall-clock time, with DST
    handled correctly. When unset, expressions are interpreted in UTC.
    """
    if not croniter.is_valid(cron_expr):
        raise ValueError(f"invalid cron expression: {cron_expr!r}")
    _validate_timezone(timezone)
    nxt = _iso(_next_fire(cron_expr, tz=timezone))
    with _db.transaction(conn):
        existing = conn.execute(
            "SELECT id FROM cron_trigger WHERE workflow_name = ? AND cron_expr = ?",
            (workflow_name, cron_expr),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE cron_trigger SET enabled = 1, next_fire_at = ?, "
                "timezone = ? WHERE id = ?",
                (nxt, timezone, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO cron_trigger (workflow_name, cron_expr, "
            "  next_fire_at, timezone) VALUES (?, ?, ?, ?)",
            (workflow_name, cron_expr, nxt, timezone),
        )
        return cur.lastrowid


def list_crons(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, workflow_name, cron_expr, next_fire_at, last_fire_at, "
            "  enabled, timezone FROM cron_trigger ORDER BY id"
        )
    ]


def acquire_lock(
    conn: sqlite3.Connection, holder: str, lease_seconds: int = 10
) -> bool:
    """Try to acquire the scheduler leader row. Returns True if we hold it.

    Either the row is unheld (holder IS NULL), the lease has expired, or we
    already hold it. In all those cases the UPDATE succeeds.
    """
    expires = _iso(_utcnow() + timedelta(seconds=lease_seconds))
    with _db.transaction(conn):
        cur = conn.execute(
            "UPDATE scheduler_lock SET holder = ?, expires = ? "
            "WHERE id = 1 AND ("
            "  holder IS NULL "
            "  OR holder = ? "
            "  OR expires < strftime('%Y-%m-%d %H:%M:%f','now')"
            ")",
            (holder, expires, holder),
        )
        return cur.rowcount > 0


def release_lock(conn: sqlite3.Connection, holder: str) -> None:
    """Best-effort release. Only releases the row if we still own it."""
    with _db.transaction(conn):
        conn.execute(
            "UPDATE scheduler_lock SET holder = NULL, expires = NULL "
            "WHERE id = 1 AND holder = ?",
            (holder,),
        )


def fire_due_crons(conn: sqlite3.Connection) -> int:
    """Fire every cron whose next_fire_at is in the past. Returns # fired.

    Each fire is one transaction: trigger the run, advance next_fire_at,
    record last_fire_at. This is the same exactly-once pattern the rest of
    the engine uses - if anything fails between the trigger and the advance,
    rollback leaves the trigger unmoved and the run uncreated.
    """
    fired = 0
    while True:
        # Pull one due trigger at a time; loop until none are due.
        due = conn.execute(
            "SELECT * FROM cron_trigger "
            "WHERE enabled = 1 "
            "  AND next_fire_at <= strftime('%Y-%m-%d %H:%M:%f','now') "
            "ORDER BY next_fire_at LIMIT 1"
        ).fetchone()
        if due is None:
            break

        # Compute the next future fire time relative to NOW.
        # Skip-on-miss: we don't backfill prior ticks.
        next_fire = _iso(_next_fire(due["cron_expr"], tz=due["timezone"]))
        last_fire = _iso(_utcnow())

        with _db.transaction(conn):
            # Guard against another scheduler having moved the row already.
            cur = conn.execute(
                "UPDATE cron_trigger SET next_fire_at = ?, last_fire_at = ? "
                "WHERE id = ? AND next_fire_at = ?",
                (next_fire, last_fire, due["id"], due["next_fire_at"]),
            )
            if cur.rowcount == 0:
                continue  # raced; another scheduler already fired this one
            engine._trigger_run_locked(
                conn,
                due["workflow_name"],
                trigger_kind="cron",
                trigger_payload={
                    "cron_trigger_id": due["id"],
                    "cron_expr": due["cron_expr"],
                    "scheduled_for": due["next_fire_at"],
                },
            )
            fired += 1
            log.info("fired cron trigger=%s workflow=%s scheduled_for=%s",
                     due["id"], due["workflow_name"], due["next_fire_at"])
    return fired


def scheduler_loop(
    conn: sqlite3.Connection,
    holder: Optional[str] = None,
    lease_seconds: int = 10,
    poll_interval: float = 0.25,
    stop_after_seconds: Optional[float] = None,
) -> None:
    """Run the scheduler forever (or until stop_after_seconds elapses).

    Each tick: try to grab the leader lock. If we hold it, fire any due
    crons. Sleep briefly. Renew the lock to keep our lease.
    """
    holder = holder or f"sched-{uuid.uuid4().hex[:8]}"
    start = time.monotonic()
    try:
        while True:
            if stop_after_seconds is not None and (time.monotonic() - start) >= stop_after_seconds:
                return
            if acquire_lock(conn, holder, lease_seconds):
                fire_due_crons(conn)
                from . import engine as _engine
                _engine.reap_timed_out_runs(conn)
            time.sleep(poll_interval)
    finally:
        release_lock(conn, holder)


def preview_fires(cron_expr: str, tz: Optional[str] = None,
                  count: int = 10) -> list[dict]:
    """Return the next ``count`` fire times for ``cron_expr`` in both the
    configured tz and UTC. Used by `automaton scheduler next` for DST
    surprise detection."""
    _validate_timezone(tz)
    if not croniter.is_valid(cron_expr):
        raise ValueError(f"invalid cron expression: {cron_expr!r}")
    out = []
    base = _utcnow()
    for _ in range(count):
        nxt = _next_fire(cron_expr, base=base, tz=tz)
        utc_str = nxt.strftime("%Y-%m-%d %H:%M:%S UTC")
        if tz:
            local_str = nxt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            local_str = utc_str
        out.append({"local": local_str, "utc": utc_str})
        # Advance one second past this fire so the next iteration moves forward.
        base = nxt + timedelta(seconds=1)
    return out
