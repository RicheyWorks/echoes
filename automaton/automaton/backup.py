"""Online snapshot backup + restore using SQLite's backup API.

``snapshot()`` produces a consistent snapshot of the live database file
even while workers and the scheduler are writing to it. The backup API
copies pages under a read lock that doesn't block writers, so the
destination is a transactionally-consistent point-in-time copy.

``integrity_check()`` runs ``PRAGMA integrity_check`` against a DB file
and returns either ``"ok"`` or the corruption description from SQLite.
Used by the CLI's ``backup`` and ``restore`` paths to catch silent
corruption at backup time rather than at recovery time (when you need
it to work).

``restore()`` is a safe ``cp`` with verification: refuses to clobber an
existing target without ``force=True``, runs integrity_check on both
source and destination, and verifies the schema version after.

For continuous replication see ``deploy/litestream/``.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional


def snapshot(src_path: str | Path, dest_path: str | Path,
             pages_per_step: int = 200, sleep_ms: int = 10,
             integrity: bool = True) -> dict:
    """Copy the live SQLite database at src_path to dest_path consistently.

    Returns a dict with the source, destination, size in bytes, pages
    copied, elapsed seconds, and (if ``integrity=True``) the
    ``PRAGMA integrity_check`` result on the destination.
    """
    src_path = str(src_path)
    dest_path = str(dest_path)
    if os.path.exists(dest_path):
        os.remove(dest_path)

    start = time.monotonic()
    pages_total = 0
    with sqlite3.connect(src_path, timeout=30.0) as src:
        with sqlite3.connect(dest_path) as dest:
            # progress callback receives (status, remaining, page_count)
            def progress(status, remaining, page_count):
                nonlocal pages_total
                pages_total = page_count
            src.backup(dest, pages=pages_per_step, progress=progress,
                       sleep=sleep_ms / 1000.0)
    elapsed = time.monotonic() - start
    size_bytes = os.path.getsize(dest_path)
    result = {
        "source": src_path,
        "destination": dest_path,
        "size_bytes": size_bytes,
        "pages": pages_total,
        "elapsed_seconds": round(elapsed, 3),
    }
    if integrity:
        result["integrity"] = integrity_check(dest_path)
    return result


def integrity_check(db_path: str | Path) -> str:
    """Return ``"ok"`` if the DB passes ``PRAGMA integrity_check``.

    Otherwise return SQLite's corruption description (a multi-line
    string with one issue per line). Callers should compare for equality
    with ``"ok"`` rather than truthiness. A DB so corrupt that the
    PRAGMA itself fails is reported as ``"unreadable: <reason>"``.
    """
    try:
        conn = sqlite3.connect(os.fspath(db_path))
    except sqlite3.Error as e:
        return f"unreadable: {e}"
    try:
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as e:
            return f"unreadable: {e}"
    finally:
        conn.close()
    if len(rows) == 1 and rows[0][0] == "ok":
        return "ok"
    return "\n".join(r[0] for r in rows)


def restore(src_path: str | Path, dest_path: str | Path,
            force: bool = False,
            verify_integrity: bool = True,
            verify_schema: bool = True) -> dict:
    """Restore a snapshot file to ``dest_path`` (the live DB location).

    Refuses to clobber an existing ``dest_path`` unless ``force=True`` -
    accidental restore on top of a healthy DB is exactly the failure
    mode we don't want to make easy.

    Returns a dict like::

        {
            "source": "/path/to/snapshot",
            "destination": "/path/to/automaton.db",
            "size_bytes": 102400,
            "integrity_source": "ok",
            "integrity_destination": "ok",
            "schema_version": "0001-initial",
        }
    """
    src_path = str(src_path)
    dest_path = str(dest_path)
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"snapshot source does not exist: {src_path}")
    if not force and os.path.exists(dest_path):
        raise FileExistsError(
            f"refusing to clobber existing DB at {dest_path}; "
            "pass force=True (or --force on the CLI) to override"
        )

    integrity_src = None
    if verify_integrity:
        integrity_src = integrity_check(src_path)
        if integrity_src != "ok":
            raise RuntimeError(
                f"snapshot at {src_path} fails integrity_check:\n{integrity_src}"
            )

    # Atomic-ish: copy to a temp file next to the destination, then rename.
    # If anything goes wrong before the rename, the existing dest is preserved.
    tmp = dest_path + ".restoring"
    if os.path.exists(tmp):
        os.remove(tmp)
    shutil.copy2(src_path, tmp)

    integrity_dest = None
    if verify_integrity:
        integrity_dest = integrity_check(tmp)
        if integrity_dest != "ok":
            os.remove(tmp)
            raise RuntimeError(
                f"copied DB at {tmp} fails integrity_check:\n{integrity_dest}"
            )

    schema_version: Optional[str] = None
    if verify_schema:
        # Lazy import - migrate doesn't need to be loaded for the
        # backup-only path.
        from . import migrate as _mig
        schema_version = _mig.current_version(tmp)

    os.replace(tmp, dest_path)

    return {
        "source": src_path,
        "destination": dest_path,
        "size_bytes": os.path.getsize(dest_path),
        "integrity_source": integrity_src,
        "integrity_destination": integrity_dest,
        "schema_version": schema_version,
    }
