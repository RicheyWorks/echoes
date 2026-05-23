"""Migration system tests.

Covers:
  - Fresh DB: yoyo applies 0001 and bookkeeping tables appear.
  - Idempotent re-apply: second call is a no-op.
  - Pre-yoyo upgrade: legacy schema (no _yoyo_migration) is shimmed correctly.
  - Pending detection: `pending()` lists migrations not yet applied.
  - Pre-migrate snapshot: copy of DB exists before changes are applied.
  - assert_up_to_date: raises SystemExit when migrations are pending.

These tests create temporary migrations on-the-fly via a fixture so the
real production migration set (in automaton/migrations/) isn't perturbed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automaton import db as _db
from automaton import migrate as _mig


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "mig.db"


@pytest.fixture
def extra_migration(tmp_path, monkeypatch):
    """Temporarily extend the migrations directory with extra .sql files.

    Returns a callable: ``add("0042-foo", "CREATE TABLE foo ...")`` writes
    the file under a copy of the migrations dir and points
    ``automaton.migrate.MIGRATIONS_DIR`` at the copy. Files are isolated
    per test; nothing leaks across tests or back into the source tree.
    """
    shadow = tmp_path / "migrations"
    shadow.mkdir()
    # Copy the real 0001-initial.sql so fresh-DB tests still work.
    real_initial = _mig.MIGRATIONS_DIR / "0001-initial.sql"
    (shadow / "0001-initial.sql").write_text(
        real_initial.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(_mig, "MIGRATIONS_DIR", shadow)

    def add(mig_id, sql):
        (shadow / f"{mig_id}.sql").write_text(sql, encoding="utf-8")

    return add


def test_fresh_db_applies_initial(db_path, extra_migration):
    """A brand new DB should get all migrations applied and yoyo bookkeeping."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()

    raw = sqlite3.connect(str(db_path))
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    raw.close()

    assert "workflow_def" in tables  # core schema applied
    assert "_yoyo_migration" in tables  # yoyo bookkeeping created
    assert _mig.current_version(db_path) == "0001-initial"


def test_apply_is_idempotent(db_path, extra_migration):
    """Calling migrate twice is a no-op."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    result = _mig.apply(db_path)
    assert result["applied"] == []
    assert result["snapshot"] is None


def test_pre_yoyo_db_gets_shimmed(db_path, extra_migration):
    """An existing install (legacy schema, no _yoyo_migration table) should
    upgrade transparently: shim marks 0001 applied without re-running it."""
    # Simulate the old codebase: apply the schema directly, no yoyo metadata.
    raw = sqlite3.connect(str(db_path))
    raw.executescript(
        (_mig.MIGRATIONS_DIR / "0001-initial.sql").read_text(encoding="utf-8")
    )
    raw.commit()
    raw.close()

    assert _mig._is_pre_yoyo_db(db_path)
    assert not _mig._is_fresh_db(db_path)

    result = _mig.apply(db_path)
    assert result["applied"] == []  # 0001 was shimmed, not re-applied
    assert _mig.current_version(db_path) == "0001-initial"

    # Schema didn't get clobbered.
    raw = sqlite3.connect(str(db_path))
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    raw.close()
    assert "workflow_def" in tables
    assert "_yoyo_migration" in tables


def test_pending_lists_unapplied(db_path, extra_migration):
    """After adding a new migration, pending() returns its id."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    extra_migration("0050-add-demo", "CREATE TABLE demo_table (id INTEGER);")
    assert _mig.pending(db_path) == ["0050-add-demo"]


def test_apply_runs_pending_migration(db_path, extra_migration):
    """A new migration gets executed and the demo table appears."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    extra_migration("0050-add-demo", "CREATE TABLE demo_table (id INTEGER);")
    result = _mig.apply(db_path)
    assert result["applied"] == ["0050-add-demo"]
    raw = sqlite3.connect(str(db_path))
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    raw.close()
    assert "demo_table" in tables
    assert _mig.current_version(db_path) == "0050-add-demo"


def test_pre_migrate_snapshot_when_pending(db_path, extra_migration):
    """apply(snapshot=True) copies the DB before applying."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    extra_migration("0060-snapped", "CREATE TABLE snapped_t (id INTEGER);")
    result = _mig.apply(db_path, snapshot=True)
    assert result["snapshot"] is not None
    snap = Path(result["snapshot"])
    assert snap.exists()
    assert snap.name.startswith(db_path.name + ".pre-migrate-")
    # Snapshot reflects pre-migration state - no snapped_t there.
    raw = sqlite3.connect(str(snap))
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    raw.close()
    assert "snapped_t" not in tables  # snapshot is pre-change
    # Live DB has the new table.
    raw = sqlite3.connect(str(db_path))
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    raw.close()
    assert "snapped_t" in tables


def test_no_snapshot_when_nothing_pending(db_path, extra_migration):
    """If there's nothing to do, no snapshot is taken (we don't litter)."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    result = _mig.apply(db_path, snapshot=True)
    assert result["applied"] == []
    assert result["snapshot"] is None


def test_assert_up_to_date_raises_when_pending(db_path, extra_migration):
    """The startup gate refuses to start with pending migrations."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    extra_migration("0070-gate", "CREATE TABLE gate_t (id INTEGER);")
    with pytest.raises(SystemExit) as exc:
        _mig.assert_up_to_date(db_path)
    msg = str(exc.value)
    assert "0070-gate" in msg
    assert "automaton migrate" in msg


def test_assert_up_to_date_passes_when_clean(db_path, extra_migration):
    """When everything's applied, the gate is a no-op."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    _mig.assert_up_to_date(db_path)  # does not raise


def test_initial_migration_seeds_scheduler_lock(db_path, extra_migration):
    """0001-initial includes the seed INSERT for scheduler_lock(id=1).
    Regression guard - if we ever drop that, scheduler bootstrapping breaks."""
    conn = _db.connect(db_path)
    _db.migrate(conn)
    row = conn.execute("SELECT id, holder, expires FROM scheduler_lock").fetchone()
    conn.close()
    assert row["id"] == 1
    assert row["holder"] is None
    assert row["expires"] is None


# ------------------------------------------------------------------ #
# Per-migration data-preservation tests                               #
# ------------------------------------------------------------------ #

@pytest.fixture
def step_migrations(tmp_path, monkeypatch):
    """Step through real migrations one at a time.

    Returns ``(db_path, up_to)`` where calling ``up_to("NNNN-name")`` copies
    all real migration SQL files up to and including that stem into a shadow
    directory (idempotently) and applies whatever is pending, returning the
    ``apply()`` result dict.

    Migrations are applied in lexicographic stem order, which matches the
    numeric prefix convention (0001-, 0002-, …).
    """
    real_dir = _mig.MIGRATIONS_DIR          # capture BEFORE monkeypatch
    shadow = tmp_path / "migrations"
    shadow.mkdir()
    db = tmp_path / "step.db"
    monkeypatch.setattr(_mig, "MIGRATIONS_DIR", shadow)

    real_files = sorted(real_dir.glob("*.sql"), key=lambda f: f.stem)

    def up_to(migration_id: str) -> dict:
        """Copy real migrations up to *migration_id* and apply pending ones."""
        for sql_file in real_files:
            dest = shadow / sql_file.name
            if not dest.exists():
                dest.write_text(
                    sql_file.read_text(encoding="utf-8"), encoding="utf-8"
                )
            if sql_file.stem == migration_id:
                break
        return _mig.apply(db)

    return db, up_to


def test_0002_add_timezone_preserves_cron_rows(step_migrations):
    """0002 adds cron_trigger.timezone (nullable). Existing rows survive with NULL
    for that column; new rows can carry an IANA timezone string."""
    db, up_to = step_migrations
    up_to("0001-initial")

    # Seed a cron_trigger row under the 0001 schema (no timezone column yet).
    raw = sqlite3.connect(str(db))
    raw.execute(
        "INSERT INTO workflow_def (name, version, spec_json) VALUES ('wf', 1, '{}')"
    )
    raw.execute(
        "INSERT INTO cron_trigger (workflow_name, cron_expr, next_fire_at)"
        " VALUES ('wf', '*/5 * * * *', '2026-01-01 00:00:00')"
    )
    raw.commit()
    raw.close()

    result = up_to("0002-add-timezone")
    assert result["applied"] == ["0002-add-timezone"]

    raw = sqlite3.connect(str(db))
    raw.row_factory = sqlite3.Row
    # Pre-existing row survived.
    row = raw.execute(
        "SELECT * FROM cron_trigger WHERE workflow_name = 'wf'"
    ).fetchone()
    # A new row can store a timezone string.
    raw.execute(
        "INSERT INTO cron_trigger (workflow_name, cron_expr, next_fire_at, timezone)"
        " VALUES ('wf', '0 9 * * *', '2026-01-01 09:00:00', 'America/New_York')"
    )
    raw.commit()
    tz_row = raw.execute(
        "SELECT timezone FROM cron_trigger WHERE cron_expr = '0 9 * * *'"
    ).fetchone()
    raw.close()

    assert row is not None, "pre-existing cron_trigger row was lost after 0002"
    assert row["cron_expr"] == "*/5 * * * *"
    assert row["timezone"] is None, "existing rows should have NULL timezone"
    assert tz_row["timezone"] == "America/New_York"


def test_0003_timed_out_status_preserves_run_rows(step_migrations):
    """0003 recreates the run table to expand the CHECK constraint. Existing run
    rows must survive the table swap; the new timeout_seconds column appears on
    workflow_def; and timed_out becomes a valid status value."""
    db, up_to = step_migrations
    up_to("0002-add-timezone")  # apply 0001 + 0002

    # Seed a run row under the 0002 schema (no timed_out status yet).
    raw = sqlite3.connect(str(db))
    raw.execute(
        "INSERT INTO workflow_def (name, version, spec_json) VALUES ('wf', 1, '{}')"
    )
    raw.execute(
        "INSERT INTO run (workflow_def_id, status, trigger_kind)"
        " VALUES (1, 'completed', 'manual')"
    )
    raw.commit()
    raw.close()

    result = up_to("0003-timed-out-status")
    assert result["applied"] == ["0003-timed-out-status"]

    raw = sqlite3.connect(str(db))
    raw.row_factory = sqlite3.Row
    # Original run row survived the table-rename swap.
    run_row = raw.execute("SELECT status FROM run WHERE id = 1").fetchone()
    # workflow_def gained timeout_seconds (NULL by default for old rows).
    wf_row = raw.execute(
        "SELECT timeout_seconds FROM workflow_def WHERE name = 'wf'"
    ).fetchone()
    # timed_out is now a legal status value.
    raw.execute(
        "INSERT INTO run (workflow_def_id, status, trigger_kind)"
        " VALUES (1, 'timed_out', 'cron')"
    )
    raw.commit()
    timed_row = raw.execute(
        "SELECT status FROM run WHERE trigger_kind = 'cron'"
    ).fetchone()
    raw.close()

    assert run_row is not None, "original run row was lost during 0003 table swap"
    assert run_row["status"] == "completed"
    assert wf_row is not None, "timeout_seconds column missing from workflow_def"
    assert wf_row["timeout_seconds"] is None   # NULL default for existing rows
    assert timed_row["status"] == "timed_out"  # new status accepted by CHECK
