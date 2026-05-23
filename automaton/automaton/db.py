"""SQLite connection, migrations, and transaction helpers.

Schema is owned by ``automaton/migrations/`` and applied via yoyo. See
``automaton.migrate`` for the implementation. ``migrate(conn)`` is kept here
as a backward-compatible entry point for callers that already have a
connection - it derives the underlying file path from the connection and
hands off to the yoyo-backed wrapper.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Kept for backward compat - some tests reference this. The canonical
# schema source today is automaton/migrations/0001-initial.sql.
SCHEMA_PATH = Path(__file__).with_name("migrations") / "0001-initial.sql"


# Production PRAGMA values - SQLite hardening per the Phase 13
# operating-envelope work. busy_timeout above 5 s is a safe default; the
# 30 s here gives migrations / backups / load spikes room without the
# engine returning SQLITE_BUSY too eagerly.
_PRAGMAS = (
    ("journal_mode", "wal"),
    ("synchronous", "1"),       # NORMAL
    ("foreign_keys", "1"),
    ("busy_timeout", "30000"),
    ("wal_autocheckpoint", "1000"),
)


def connect(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return conn


def verify_pragmas(conn) -> dict:
    """Return a dict of {pragma_name: (observed, expected)} pairs.

    Callers compare values for equality and surface a startup-time
    error if anything's off - e.g. a hand-edited PRAGMA on the live DB
    or an old SQLite version that doesn't honor a setting.

    Numeric and lowercase-string comparisons; PRAGMA return values are
    a mix of integers and strings depending on the setting.
    """
    out = {}
    for name, expected in _PRAGMAS:
        row = conn.execute(f"PRAGMA {name}").fetchone()
        observed = "" if row is None else row[0]
        out[name] = (str(observed).lower(), expected)
    return out


def _path_from_connection(conn) -> str:
    """Extract the on-disk path of the main database from a Connection.

    SQLite returns ('main', name, path) rows from PRAGMA database_list;
    'main' is index 0 and its path is what we want. Empty path means
    in-memory ":memory:" which we can't migrate via yoyo (it opens its
    own connection that would point at a different in-memory DB).
    """
    rows = conn.execute("PRAGMA database_list").fetchall()
    for r in rows:
        # sqlite3.Row is indexable by name when row_factory is set.
        if r["name"] == "main":
            path = r["file"]
            if not path:
                raise RuntimeError(
                    "db.migrate(conn) on an in-memory database isn't "
                    "supported - migrations need a real file. Open with "
                    "a path under tmp_path/."
                )
            return path
    raise RuntimeError("could not determine database path from connection")


def migrate(conn):
    """Apply pending migrations against the connection's database.

    Idempotent: safe to call on every startup. Tests that wrap a Connection
    around a tmp_path file should call this exactly once after ``connect()``.
    For the CLI's startup-gate behavior (refuse to start unless caught up)
    see ``automaton.migrate.assert_up_to_date``.

    No pre-migration snapshot is taken when called this way - that's the
    CLI's responsibility (``automaton migrate``). Library callers don't
    want extra files appearing next to test DBs.
    """
    from . import migrate as _mig  # local import to avoid a cycle on cold start
    _mig.apply(_path_from_connection(conn), snapshot=False)


@contextmanager
def transaction(conn):
    """Wrap a block of statements in BEGIN IMMEDIATE ... COMMIT."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def to_json(value):
    return None if value is None else json.dumps(value, default=str)


def from_json(value):
    return None if value is None else json.loads(value)
