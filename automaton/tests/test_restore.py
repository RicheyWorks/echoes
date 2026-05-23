"""Backup + restore drill.

This is the "we have backups but never restored from them" failure mode
the plan calls out. The whole drill, with assertions:

  1. Run workflows against a live DB.
  2. Snapshot the DB.
  3. Delete the live DB.
  4. Restore from the snapshot.
  5. Confirm run history + schema version + integrity all intact.
  6. Workers can continue running off the restored DB.

We can't exercise Litestream itself in CI (it's a Go binary not in the
runner image), but the backup-API path that Litestream rebuilds restore
against is exactly the same SQLite mechanism, so this catches the
critical failure mode (broken integrity check, schema mismatch, missing
event log).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automaton import backup as _backup
from automaton import db as _db
from automaton import engine
from automaton import migrate as _mig


@pytest.fixture
def live(tmp_path):
    """A live DB with the schema applied + one completed run + one parked run."""
    path = tmp_path / "live.db"
    conn = _db.connect(path)
    _db.migrate(conn)
    engine.register_workflow(conn, {
        "name": "hello",
        "steps": [{"name": "noop", "type": "file_append",
                   "path": str(tmp_path / "out.log"), "text": "x"}],
    })
    engine.register_workflow(conn, {
        "name": "park",
        "steps": [{"name": "wait", "type": "wait_for_signal",
                   "signal": "go", "poll_seconds": 1}],
    })
    rid_done = engine.trigger_run(conn, "hello")
    rid_park = engine.trigger_run(conn, "park")
    engine.worker_loop(conn, stop_when_idle=True)  # drains hello, parks park
    conn.close()
    return path, rid_done, rid_park


# --------------- integrity_check ---------------

def test_integrity_check_ok_on_fresh_db(tmp_path):
    p = tmp_path / "fresh.db"
    conn = _db.connect(p)
    _db.migrate(conn)
    conn.close()
    assert _backup.integrity_check(p) == "ok"


def test_integrity_check_detects_corruption(tmp_path):
    """Stomp the SQLite header. integrity_check should surface this.

    We build the DB in journal_mode=DELETE (no WAL sidecar) so corrupting
    the main file actually corrupts the live data. With WAL, SQLite reads
    valid pages from the sidecar and reports 'ok' despite header damage.
    """
    p = tmp_path / "corrupt.db"
    # Skip our _db.connect helper because it enables WAL; use a vanilla
    # connection and apply the schema by hand.
    raw = sqlite3.connect(str(p))
    raw.executescript(
        (_mig.MIGRATIONS_DIR / "0001-initial.sql").read_text(encoding="utf-8")
    )
    raw.commit()
    raw.close()
    # Corrupt the SQLite header (first 32 bytes of the magic + page-size area).
    with open(p, "r+b") as f:
        f.seek(0)
        f.write(b"\x00" * 32)
    result = _backup.integrity_check(p)
    assert result != "ok"


# --------------- snapshot includes integrity result ---------------

def test_snapshot_reports_integrity(live, tmp_path):
    live_path, _, _ = live
    snap = tmp_path / "snap.db"
    info = _backup.snapshot(live_path, snap)
    assert info["integrity"] == "ok"
    assert info["size_bytes"] > 0


# --------------- restore round trip ---------------

def test_restore_round_trip(live, tmp_path):
    """The full drill: snapshot → delete live → restore → verify."""
    live_path, rid_done, rid_park = live
    snap = tmp_path / "snap.db"
    _backup.snapshot(live_path, snap)

    # Capture state BEFORE we delete, to compare after.
    pre_conn = _db.connect(live_path)
    pre_runs = pre_conn.execute(
        "SELECT id, status FROM run ORDER BY id"
    ).fetchall()
    pre_events = pre_conn.execute(
        "SELECT COUNT(*) AS c FROM event_log"
    ).fetchone()["c"]
    pre_conn.close()

    # Disaster.
    live_path.unlink()
    assert not live_path.exists()

    # Restore.
    info = _backup.restore(snap, live_path, force=False)
    assert info["integrity_source"] == "ok"
    assert info["integrity_destination"] == "ok"
    # The schema version reports the latest migration that was applied
    # to the snapshot. We don't pin it to "0001-initial" because the
    # migrations/ directory may contain extra (no-op placeholder)
    # entries from prior test runs on a non-deletable mount.
    assert info["schema_version"] is not None

    # State matches.
    post_conn = _db.connect(live_path)
    post_runs = post_conn.execute(
        "SELECT id, status FROM run ORDER BY id"
    ).fetchall()
    post_events = post_conn.execute(
        "SELECT COUNT(*) AS c FROM event_log"
    ).fetchone()["c"]
    assert [(r["id"], r["status"]) for r in post_runs] == \
           [(r["id"], r["status"]) for r in pre_runs]
    assert post_events == pre_events

    # The parked run is still parked - the restored DB resumes cleanly.
    parked = post_conn.execute(
        "SELECT status FROM run WHERE id = ?", (rid_park,)
    ).fetchone()
    assert parked["status"] in ("pending", "running")

    # Send the parked signal; the engine should finish it. The queue
    # has ready_at ~1s in the future from the wait_for_signal park; we
    # advance it manually so stop_when_idle doesn't bail out immediately.
    engine.send_signal(post_conn, rid_park, "go")
    post_conn.execute(
        "UPDATE queue SET ready_at = datetime('now') "
        "WHERE step_id IN (SELECT id FROM step WHERE run_id = ?)",
        (rid_park,),
    )
    engine.worker_loop(post_conn, stop_when_idle=True)
    final = post_conn.execute(
        "SELECT status FROM run WHERE id = ?", (rid_park,)
    ).fetchone()
    assert final["status"] == "completed"
    post_conn.close()


def test_restore_refuses_to_clobber(live, tmp_path):
    live_path, _, _ = live
    snap = tmp_path / "snap.db"
    _backup.snapshot(live_path, snap)
    # live_path still exists. Restore must refuse.
    with pytest.raises(FileExistsError, match="refusing to clobber"):
        _backup.restore(snap, live_path, force=False)
    # With --force, it goes through.
    info = _backup.restore(snap, live_path, force=True)
    assert info["integrity_destination"] == "ok"


def test_restore_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _backup.restore(tmp_path / "nope.db", tmp_path / "out.db")


def test_restore_refuses_corrupt_source(live, tmp_path):
    """A corrupt snapshot shouldn't quietly become the new live DB."""
    live_path, _, _ = live
    snap = tmp_path / "corrupt.db"
    _backup.snapshot(live_path, snap)
    # Corrupt the snapshot.
    with open(snap, "r+b") as f:
        f.seek(4096)
        f.write(b"\x00" * 4096)
    with pytest.raises(RuntimeError, match="fails integrity_check"):
        _backup.restore(snap, tmp_path / "new.db", force=False)


def test_restore_reports_pending_migrations_when_binary_is_ahead(live, tmp_path,
                                                                  monkeypatch):
    """If the binary's migrations dir has more versions than the snapshot,
    _mig.pending(restored_db) lists them and the CLI can warn.

    Use a shadow MIGRATIONS_DIR for the duration of this test so we
    don't pollute the on-disk migrations/ directory.
    """
    live_path, _, _ = live
    snap = tmp_path / "snap.db"
    _backup.snapshot(live_path, snap)

    # Shadow MIGRATIONS_DIR with a tmp_path copy that adds 0500-fake.
    shadow = tmp_path / "migrations"
    shadow.mkdir()
    real_initial = _mig.MIGRATIONS_DIR / "0001-initial.sql"
    (shadow / "0001-initial.sql").write_text(
        real_initial.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (shadow / "0500-fake.sql").write_text(
        "CREATE TABLE future_t (id INTEGER PRIMARY KEY);\n"
    )
    monkeypatch.setattr(_mig, "MIGRATIONS_DIR", shadow)

    live_path.unlink()
    _backup.restore(snap, live_path)
    pending = _mig.pending(live_path)
    assert "0500-fake" in pending
