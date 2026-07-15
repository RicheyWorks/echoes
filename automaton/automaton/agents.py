"""Agent memory persistence — Option C integration.

Provides a durable store for echoes agents running with ``--remote-store``.
Each agent has a named row in ``agent`` (goal, last tick) and one row per
memory entry in ``agent_memory`` (the full hash-chain entry as JSON).

The JSON envelope for each entry mirrors the ``echoes report --json`` schema::

    {
        "tick": 1,
        "action": "Observe",
        "event": "...",
        "note": "...",
        "hash": "<hex>",
        "prev_hash": "<hex>"
    }

All write operations run inside a ``db.transaction(conn)`` block so the
caller's connection isolation settings are respected (WAL mode on SQLite,
autocommit on Postgres).
"""
from __future__ import annotations

import json
from typing import Any

from . import db


# ---------------------------------------------------------------------------
# Agent metadata
# ---------------------------------------------------------------------------

def get_agent(conn, name: str) -> dict | None:
    """Return the agent row for *name*, or ``None`` if it doesn't exist."""
    row = conn.execute(
        "SELECT name, goal, tick, created_at, updated_at "
        "FROM agent WHERE name = ?",
        (name,),
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_agent(conn, name: str, goal: str, tick: int) -> dict:
    """Create or update the agent metadata row.

    Safe to call on every ``echoes run`` invocation — subsequent calls just
    advance the ``tick`` counter and ``updated_at`` timestamp.

    Returns the current agent row after the upsert.
    """
    with db.transaction(conn):
        existing = conn.execute(
            "SELECT id FROM agent WHERE name = ?", (name,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO agent (name, goal, tick) VALUES (?, ?, ?)",
                (name, goal, tick),
            )
        else:
            conn.execute(
                "UPDATE agent SET goal = ?, tick = ?, "
                "updated_at = datetime('now') "
                "WHERE name = ?",
                (goal, tick, name),
            )
    row = conn.execute(
        "SELECT name, goal, tick, created_at, updated_at "
        "FROM agent WHERE name = ?",
        (name,),
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Memory entries
# ---------------------------------------------------------------------------

def append_entry(conn, agent_name: str, tick: int, entry: dict[str, Any]) -> int:
    """Append one memory entry and return its row ID.

    *entry* must be a dict matching the echoes JSON schema (tick, action,
    event, note, hash, prev_hash).  The ``tick`` parameter is taken from
    the entry dict and must be unique per agent — duplicate ticks raise an
    ``IntegrityError``.

    The agent row must exist before calling this function (call
    ``upsert_agent`` first).
    """
    entry_json = json.dumps(entry, default=str)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO agent_memory (agent_name, tick, entry_json) "
            "VALUES (?, ?, ?)",
            (agent_name, tick, entry_json),
        )
        # Advance the agent tick counter.
        conn.execute(
            "UPDATE agent SET tick = ?, updated_at = datetime('now') "
            "WHERE name = ?",
            (tick, agent_name),
        )
    return cur.lastrowid


def get_entries(conn, agent_name: str) -> list[dict[str, Any]]:
    """Return all memory entries for *agent_name*, ordered by tick ascending.

    Each element is the parsed ``entry_json`` dict.  Returns an empty list
    if the agent doesn't exist or has no entries yet.
    """
    rows = conn.execute(
        "SELECT entry_json FROM agent_memory "
        "WHERE agent_name = ? ORDER BY tick ASC",
        (agent_name,),
    ).fetchall()
    return [json.loads(r["entry_json"]) for r in rows]


def list_agents(conn) -> list[dict]:
    """Return a summary row for every agent (name, goal, tick, updated_at)."""
    rows = conn.execute(
        "SELECT name, goal, tick, updated_at FROM agent ORDER BY name ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def latest_entry(conn, agent_name: str) -> dict[str, Any] | None:
    """Return the highest-tick memory entry for *agent_name*, or None."""
    row = conn.execute(
        "SELECT entry_json FROM agent_memory "
        "WHERE agent_name = ? ORDER BY tick DESC LIMIT 1",
        (agent_name,),
    ).fetchone()
    return json.loads(row["entry_json"]) if row else None


def chain_linkage_ok(conn, agent_name: str) -> bool | None:
    """Cheap server-side chain check: every entry's ``prev_hash`` must equal
    the previous entry's ``hash``, starting from the zero genesis.

    This detects reordering, deletion, and splicing of stored entries. It
    does NOT recompute the SHA-256 content hashes (that's echoes' /
    echoes-wasm's job — automaton does not replicate the hashing scheme), so
    a forged entry with self-consistent hashes passes here but fails
    ``echoes verify``. Returns None when the agent has no entries.
    """
    entries = get_entries(conn, agent_name)
    if not entries:
        return None
    prev = "0" * 64
    for e in entries:
        if e.get("prev_hash") != prev:
            return False
        prev = e.get("hash", "")
    return True


def delete_agent(conn, name: str) -> bool:
    """Delete an agent and all its memory entries (CASCADE).

    Returns ``True`` if the agent existed and was deleted, ``False`` otherwise.
    """
    with db.transaction(conn):
        conn.execute("DELETE FROM agent WHERE name = ?", (name,))
        # rowcount isn't reliable across backends; check existence instead.
    return conn.execute(
        "SELECT 1 FROM agent WHERE name = ?", (name,)
    ).fetchone() is None
