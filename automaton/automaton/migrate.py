"""Schema migrations via yoyo-migrations.

The engine's source of truth for schema is the set of SQL files under
``automaton/migrations/``. Each file is named ``NNNN-description.sql`` where
NNNN is a zero-padded integer; yoyo applies them in that order and records
applied migrations in a ``_yoyo_migration`` table inside the database.

This module is a thin wrapper that:

1. Detects ``pre-yoyo`` databases (created by the old ``db.migrate()`` that
   ran ``schema.sql`` directly) and retroactively marks ``0001-initial`` as
   applied. This is a one-time forward-compatibility shim so existing
   installs upgrade without losing data.

2. Snapshots the DB to ``<db_path>.pre-migrate-<ts>`` before applying any
   pending migration. Easy rollback if a migration goes sideways.

3. Provides a small API used by both ``db.migrate(conn)`` (library entry
   point that tests and the engine code call) and the ``automaton migrate``
   CLI subcommand.

Design notes:

* The wrapper takes a ``db_path`` because yoyo opens its own connection. We
  could try to share the caller's connection, but yoyo wants to lock the DB
  itself and applying inside another connection's transaction would
  deadlock. A separate connection is fine: SQLite WAL handles concurrent
  readers + the migration writer cleanly.

* On a fresh DB, ``apply()`` runs migration 0001 from scratch. On a
  pre-yoyo DB (any of our tables present, no ``_yoyo_migration``), the shim
  inserts the 0001 row first so yoyo treats it as already applied.

* ``apply(snapshot=True)`` is the production path - used by the CLI. Tests
  and the library call ``apply(snapshot=False)`` to keep tmp_path clean.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Tables we expect to find on a pre-yoyo install. If any of these are
# present without the yoyo bookkeeping tables, we're upgrading.
_LEGACY_TABLES = {
    "workflow_def", "run", "step", "queue", "event_log",
    "scheduler_lock", "cron_trigger", "signal", "webhook_endpoint",
}


def _backend(db_path):
    """Yoyo backend pointed at the local SQLite file."""
    from yoyo import get_backend
    return get_backend(f"sqlite:///{os.fspath(db_path)}")


def _read_migrations():
    from yoyo import read_migrations
    return read_migrations(str(MIGRATIONS_DIR))


def _migration_list(items):
    """Wrap a plain Python list back into yoyo's MigrationList type.

    yoyo's apply_migrations / mark_migrations call .post_apply on the
    argument, which only exists on MigrationList - passing a built-in list
    raises AttributeError mid-apply. Wrap explicitly to keep that contract.
    """
    from yoyo.migrations import MigrationList
    return MigrationList(items)


def _table_names(db_path) -> set:
    conn = sqlite3.connect(os.fspath(db_path))
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def _is_fresh_db(db_path) -> bool:
    """No legacy tables and no yoyo tables - we're applying from scratch."""
    tables = _table_names(db_path)
    return not (tables & _LEGACY_TABLES) and "_yoyo_migration" not in tables


def _is_pre_yoyo_db(db_path) -> bool:
    """Legacy tables exist but yoyo bookkeeping doesn't - needs the shim."""
    tables = _table_names(db_path)
    return bool(tables & _LEGACY_TABLES) and "_yoyo_migration" not in tables


def _install_shim(db_path) -> None:
    """Mark migration 0001 as already applied without re-running its SQL.

    Done by running yoyo's mark-as-applied path. This creates yoyo's
    bookkeeping tables and inserts a row for 0001-initial so the next
    ``apply()`` skips straight to 0002 (when it exists).
    """
    backend = _backend(db_path)
    migrations = _read_migrations()
    initial = _migration_list([m for m in migrations if m.id == "0001-initial"])
    if not initial:
        raise RuntimeError(
            "expected migration 0001-initial.sql but didn't find it - "
            "is the migrations/ directory intact?"
        )
    with backend.lock():
        backend.mark_migrations(initial)


def _pre_migrate_snapshot(db_path) -> str:
    """Copy the live DB to a sibling file tagged with a timestamp.

    Uses SQLite's online backup API to handle WAL correctly. Returns the
    absolute snapshot path. Never overwrites an existing snapshot.
    """
    src = Path(os.fspath(db_path))
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = src.with_name(f"{src.name}.pre-migrate-{ts}")
    if dest.exists():
        raise FileExistsError(f"pre-migrate snapshot already exists: {dest}")
    src_conn = sqlite3.connect(os.fspath(src))
    dst_conn = sqlite3.connect(os.fspath(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return str(dest)


def pending(db_path) -> List[str]:
    """Migration IDs that haven't been applied yet."""
    if _is_pre_yoyo_db(db_path):
        _install_shim(db_path)
    backend = _backend(db_path)
    migrations = _read_migrations()
    return [m.id for m in backend.to_apply(migrations)]


def current_version(db_path) -> Optional[str]:
    """ID of the highest applied migration, or None on a fresh DB."""
    tables = _table_names(db_path)
    if "_yoyo_migration" not in tables:
        return None
    conn = sqlite3.connect(os.fspath(db_path))
    try:
        row = conn.execute(
            "SELECT migration_id FROM _yoyo_migration "
            "ORDER BY applied_at_utc DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def apply(db_path, snapshot: bool = False) -> dict:
    """Apply pending migrations.

    Returns ``{"applied": [...], "snapshot": str | None}``.
    """
    db_path = os.fspath(db_path)
    if _is_pre_yoyo_db(db_path):
        _install_shim(db_path)

    backend = _backend(db_path)
    migrations = _read_migrations()
    with backend.lock():
        to_apply = backend.to_apply(migrations)  # already a MigrationList
        snapshot_path = None
        if len(to_apply) and snapshot and Path(db_path).exists():
            snapshot_path = _pre_migrate_snapshot(db_path)
        applied_ids = [m.id for m in to_apply]
        backend.apply_migrations(to_apply)
    return {
        "applied": applied_ids,
        "snapshot": snapshot_path,
    }


def assert_up_to_date(db_path) -> None:
    """Raise SystemExit with a helpful message if migrations are pending.

    Called from the worker / scheduler / UI entry points to refuse to start
    against a DB that's behind the binary. The user has to run
    ``automaton migrate`` explicitly, or set AUTOMATON_AUTO_MIGRATE=1.
    """
    p = pending(db_path)
    if p:
        raise SystemExit(
            f"refusing to start: {len(p)} pending migration(s): "
            f"{', '.join(p)}\n"
            "run `automaton migrate` (or set AUTOMATON_AUTO_MIGRATE=1) "
            "to apply them."
        )
