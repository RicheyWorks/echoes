"""
automaton.auth — API key management for multi-tenant access control.

Key lifecycle
-------------
1. ``create_api_key(conn, name, role)`` generates a random key, stores
   SHA-256(key) in the database, and returns the plaintext **once**.
2. On every request, ``authenticate(conn, raw_token)`` hashes the token
   and looks it up.  The AUTOMATON_TOKEN env-var bypass is handled in
   ``ui.py`` before this module is reached.
3. ``touch_last_used(conn, key_id)`` is called after a successful lookup
   to keep the ``last_used_at`` column fresh.

Key format:  ``atk_`` + 64 lowercase hex chars (32 random bytes).
ID format:   ``key_`` + first 8 chars of the key_hash.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROLES = ("admin", "operator", "viewer")

# Routes that operators (and admins) can write to.
_OPERATOR_WRITE_PATHS = {
    "/api/trigger",
    "/api/signals",
    "/api/run",         # cancel sub-path checked at dispatch
    "/api/agents",      # POST entries / meta
    "/api/crons",
    "/api/workflows",
}

# ---------------------------------------------------------------------------
# Key generation and hashing
# ---------------------------------------------------------------------------

def generate_key() -> str:
    """Return a new raw API key: ``atk_<64 hex chars>``."""
    return "atk_" + secrets.token_hex(32)


def hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of *raw_key* (64 lowercase hex chars)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_api_key(conn: sqlite3.Connection, name: str, role: str) -> tuple[str, str]:
    """
    Create a new API key.

    Returns ``(key_id, raw_key)``.  ``raw_key`` is the plaintext and must be
    shown to the user exactly once — it is never stored.

    Raises ``ValueError`` if *role* is not in ``ROLES`` or *name* is blank.
    Raises ``sqlite3.IntegrityError`` if *name* is already taken.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    name = name.strip()
    if not name:
        raise ValueError("name must not be blank")

    raw_key  = generate_key()
    key_hash = hash_key(raw_key)
    key_id   = "key_" + key_hash[:8]
    now      = _now_utc()

    conn.execute(
        """
        INSERT INTO api_keys (id, name, key_hash, role, active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (key_id, name, key_hash, role, now),
    )
    conn.commit()
    return key_id, raw_key


def get_api_key(conn: sqlite3.Connection, key_id: str) -> Optional[dict]:
    """Return the key row by id, or ``None``."""
    row = conn.execute(
        "SELECT id, name, key_hash, role, active, created_at, last_used_at "
        "FROM api_keys WHERE id = ?",
        (key_id,),
    ).fetchone()
    return dict(row) if row else None


def get_api_key_by_name(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Return the key row by name, or ``None``."""
    row = conn.execute(
        "SELECT id, name, key_hash, role, active, created_at, last_used_at "
        "FROM api_keys WHERE name = ?",
        (name,),
    ).fetchone()
    return dict(row) if row else None


def list_api_keys(conn: sqlite3.Connection) -> list[dict]:
    """Return all key rows ordered by creation time."""
    rows = conn.execute(
        "SELECT id, name, role, active, created_at, last_used_at "
        "FROM api_keys ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(conn: sqlite3.Connection, name_or_id: str) -> bool:
    """
    Deactivate a key by name or id.  Returns ``True`` if a key was found
    and deactivated, ``False`` if no matching key exists.
    """
    cur = conn.execute(
        "UPDATE api_keys SET active = 0 "
        "WHERE (name = ? OR id = ?) AND active = 1",
        (name_or_id, name_or_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_api_key(conn: sqlite3.Connection, name_or_id: str) -> bool:
    """
    Permanently delete a key by name or id.  Prefer ``revoke_api_key`` for
    audit-friendly deactivation.  Returns ``True`` if a row was deleted.
    """
    cur = conn.execute(
        "DELETE FROM api_keys WHERE name = ? OR id = ?",
        (name_or_id, name_or_id),
    )
    conn.commit()
    return cur.rowcount > 0


def touch_last_used(conn: sqlite3.Connection, key_id: str) -> None:
    """Update ``last_used_at`` to now for the given key id."""
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
        (_now_utc(), key_id),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(conn: sqlite3.Connection, raw_token: str) -> Optional[dict]:
    """
    Look up *raw_token* in the api_keys table.

    Returns the key row dict (including ``role``) if found and active,
    ``None`` otherwise.

    The caller is responsible for checking ``AUTOMATON_TOKEN`` first —
    this function only handles DB-stored keys.
    """
    if not raw_token:
        return None
    kh = hash_key(raw_token)
    row = conn.execute(
        "SELECT id, name, key_hash, role, active, created_at, last_used_at "
        "FROM api_keys WHERE key_hash = ?",
        (kh,),
    ).fetchone()
    if row is None:
        return None
    r = dict(row)
    if not r["active"]:
        return None
    return r

# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------

def role_can_write(role: str) -> bool:
    """``operator`` and ``admin`` may use write (POST/DELETE) routes."""
    return role in ("admin", "operator")


def role_can_read(role: str) -> bool:
    """All roles may use read (GET) routes."""
    return role in ROLES


def role_is_admin(role: str) -> bool:
    """Only ``admin`` may manage API keys and access admin-only routes."""
    return role == "admin"
