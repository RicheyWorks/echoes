"""Phase 6: Postgres backend tests.

These tests exercise the core engine operations (register, trigger, lease,
execute, commit) against a real Postgres database.  They are skipped
automatically when the ``AUTOMATON_TEST_PG_URL`` environment variable is not
set, so the suite stays green in environments without a Postgres server.

To run locally::

    AUTOMATON_TEST_PG_URL="postgresql://localhost/automaton_test" pytest tests/test_postgres.py -v

The test user must have CREATE TABLE / DROP TABLE privileges on the database.

SQL translation unit tests (no live DB required) run unconditionally.
"""
from __future__ import annotations

import os
import re

import pytest

from automaton.pg import _translate, PgConn, connect as pg_connect, migrate as pg_migrate
from automaton import db as _db, engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PG_URL = os.environ.get("AUTOMATON_TEST_PG_URL", "")
_REQUIRES_PG = pytest.mark.skipif(
    not PG_URL,
    reason="Set AUTOMATON_TEST_PG_URL to run Postgres integration tests",
)


def _pg_conn():
    """Return a fresh Postgres PgConn with schema applied."""
    conn = pg_connect(PG_URL)
    pg_migrate(conn)
    return conn


def _teardown(conn: PgConn) -> None:
    """Drop all tables in reverse dependency order for test isolation."""
    tables = [
        "signal", "event_log", "queue", "step", "run",
        "cron_trigger", "scheduler_lock", "webhook_endpoint", "workflow_def",
    ]
    conn.execute("BEGIN")
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    conn.execute("COMMIT")
    conn.close()


# ---------------------------------------------------------------------------
# Unit tests: SQL translation (no live DB needed)
# ---------------------------------------------------------------------------

class TestTranslate:
    def test_qmark_replaced_with_percent_s(self):
        sql, _ = _translate("SELECT * FROM run WHERE id = ?")
        assert "%s" in sql
        assert "?" not in sql

    def test_multiple_qmarks(self):
        sql, _ = _translate("INSERT INTO foo (a, b) VALUES (?, ?)")
        assert sql.count("%s") == 2

    def test_datetime_now_replaced(self):
        sql, _ = _translate("UPDATE foo SET ts = datetime('now') WHERE id = ?")
        assert "NOW()" in sql
        assert "datetime('now')" not in sql

    def test_strftime_replaced(self):
        sql, _ = _translate(
            "SELECT * FROM queue WHERE ready_at <= "
            "strftime('%Y-%m-%d %H:%M:%f', 'now')"
        )
        assert "to_char(" in sql
        assert "strftime" not in sql

    def test_begin_immediate_translated(self):
        sql, _ = _translate("BEGIN IMMEDIATE")
        assert sql.strip() == "BEGIN"

    def test_insert_or_ignore_translated(self):
        sql, needs = _translate(
            "INSERT OR IGNORE INTO scheduler_lock (id) VALUES (?)"
        )
        assert "ON CONFLICT DO NOTHING" in sql
        assert "OR IGNORE" not in sql
        # Should NOT append RETURNING id when ON CONFLICT DO NOTHING is present.
        assert not needs
        assert "RETURNING" not in sql

    def test_plain_insert_gets_returning(self):
        sql, needs = _translate("INSERT INTO run (status) VALUES (?)")
        assert needs is True
        assert "RETURNING id" in sql

    def test_insert_with_existing_returning_unchanged(self):
        sql, needs = _translate("INSERT INTO foo (x) VALUES (?) RETURNING id")
        assert needs is False
        assert sql.count("RETURNING id") == 1

    def test_select_not_flagged(self):
        sql, needs = _translate("SELECT * FROM foo WHERE id = ?")
        assert needs is False
        assert "RETURNING" not in sql

    def test_update_not_flagged(self):
        sql, needs = _translate("UPDATE foo SET x = ? WHERE id = ?")
        assert needs is False

    def test_delete_not_flagged(self):
        sql, needs = _translate("DELETE FROM foo WHERE id = ?")
        assert needs is False

    def test_case_insensitive(self):
        sql, _ = _translate("insert into foo (a) values (?)")
        assert "RETURNING id" in sql


# ---------------------------------------------------------------------------
# Integration tests: live Postgres DB
# ---------------------------------------------------------------------------

class TestPgConn:
    @_REQUIRES_PG
    def test_connect_returns_pgconn(self):
        conn = _pg_conn()
        assert isinstance(conn, PgConn)
        assert conn.is_postgres is True
        _teardown(conn)

    @_REQUIRES_PG
    def test_migrate_is_idempotent(self):
        conn = _pg_conn()
        # Second call must not raise.
        pg_migrate(conn)
        _teardown(conn)

    @_REQUIRES_PG
    def test_execute_select(self):
        conn = _pg_conn()
        row = conn.execute("SELECT 1 AS n").fetchone()
        assert row["n"] == 1
        _teardown(conn)

    @_REQUIRES_PG
    def test_insert_lastrowid(self):
        conn = _pg_conn()
        with _db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO workflow_def (name, version, spec_json) "
                "VALUES (?, ?, ?)",
                ("pg-test", 1, '{"name":"pg-test","steps":[]}'),
            )
        assert cur.lastrowid is not None
        assert cur.lastrowid > 0
        _teardown(conn)

    @_REQUIRES_PG
    def test_fetchall_returns_list_of_dicts(self):
        conn = _pg_conn()
        with _db.transaction(conn):
            for i in range(3):
                conn.execute(
                    "INSERT INTO workflow_def (name, version, spec_json) VALUES (?, ?, ?)",
                    (f"wf{i}", 1, f'{{"name":"wf{i}","steps":[]}}'),
                )
        rows = conn.execute("SELECT name FROM workflow_def ORDER BY name").fetchall()
        assert len(rows) == 3
        assert isinstance(rows[0], dict)
        _teardown(conn)

    @_REQUIRES_PG
    def test_transaction_rollback(self):
        conn = _pg_conn()
        try:
            with _db.transaction(conn):
                conn.execute(
                    "INSERT INTO workflow_def (name, version, spec_json) VALUES (?, ?, ?)",
                    ("rollback-test", 1, '{"name":"rollback-test","steps":[]}'),
                )
                raise ValueError("deliberate rollback")
        except ValueError:
            pass
        rows = conn.execute(
            "SELECT * FROM workflow_def WHERE name = ?", ("rollback-test",)
        ).fetchall()
        assert rows == []
        _teardown(conn)

    @_REQUIRES_PG
    def test_insert_or_ignore_does_not_raise(self):
        conn = _pg_conn()
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_lock (id, holder, expires) "
            "VALUES (?, ?, ?)",
            (1, None, None),
        )
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_lock (id, holder, expires) "
            "VALUES (?, ?, ?)",
            (1, None, None),
        )
        conn.execute("COMMIT")
        rows = conn.execute("SELECT COUNT(*) AS c FROM scheduler_lock").fetchone()
        assert rows["c"] == 1
        _teardown(conn)


class TestEngineOnPostgres:
    """Core engine operations against a live Postgres backend."""

    @_REQUIRES_PG
    def test_register_and_trigger_run(self):
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-wf",
            "steps": [{"name": "s1", "type": "shell", "cmd": ["true"]}],
        })
        run_id = engine.trigger_run(conn, "pg-wf")
        assert isinstance(run_id, int)
        assert run_id > 0
        _teardown(conn)

    @_REQUIRES_PG
    def test_lease_one_uses_skip_locked(self):
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-lease",
            "steps": [{"name": "s1", "type": "shell", "cmd": ["true"]}],
        })
        engine.trigger_run(conn, "pg-lease")
        step_id = engine.lease_one(conn, "worker-pg", lease_seconds=30)
        assert step_id is not None
        _teardown(conn)

    @_REQUIRES_PG
    def test_full_happy_path(self):
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-happy",
            "steps": [{"name": "run", "type": "shell", "cmd": ["true"]}],
        })
        run_id = engine.trigger_run(conn, "pg-happy")
        engine.worker_loop(conn, stop_when_idle=True)
        detail = engine.run_detail(conn, run_id)
        assert detail["status"] == "completed"
        _teardown(conn)

    @_REQUIRES_PG
    def test_two_step_dag(self):
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-dag",
            "steps": [
                {"name": "a", "type": "shell", "cmd": ["true"]},
                {"name": "b", "type": "shell", "cmd": ["true"], "needs": ["a"]},
            ],
        })
        run_id = engine.trigger_run(conn, "pg-dag")
        engine.worker_loop(conn, stop_when_idle=True)
        detail = engine.run_detail(conn, run_id)
        assert detail["status"] == "completed"
        step_names = {s["name"] for s in detail["steps"]}
        assert step_names == {"a", "b"}
        _teardown(conn)

    @_REQUIRES_PG
    def test_failed_step_marks_run_failed(self):
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-fail",
            "steps": [{"name": "boom", "type": "shell", "cmd": ["false"]}],
        })
        run_id = engine.trigger_run(conn, "pg-fail")
        engine.worker_loop(conn, stop_when_idle=True)
        detail = engine.run_detail(conn, run_id)
        assert detail["status"] == "failed"
        _teardown(conn)

    @_REQUIRES_PG
    def test_list_runs_after_trigger(self):
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-list",
            "steps": [{"name": "s", "type": "shell", "cmd": ["true"]}],
        })
        engine.trigger_run(conn, "pg-list")
        runs = engine.list_runs(conn)
        assert any(r["workflow"] == "pg-list" for r in runs)
        _teardown(conn)

    @_REQUIRES_PG
    def test_idempotency_key_prevents_double_execution(self):
        """Exactly-once semantics: same idempotency key never produces two rows."""
        conn = _pg_conn()
        engine.register_workflow(conn, {
            "name": "pg-idem",
            "steps": [{"name": "s", "type": "shell", "cmd": ["true"]}],
        })
        engine.trigger_run(conn, "pg-idem")
        engine.worker_loop(conn, stop_when_idle=True)
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM step WHERE name = 's'"
        ).fetchone()
        assert rows["c"] == 1
        _teardown(conn)


# ---------------------------------------------------------------------------
# open_store routing
# ---------------------------------------------------------------------------

class TestOpenStore:
    def test_open_store_sqlite_path(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _db.open_store(db_path)
        assert not getattr(conn, "is_postgres", False)
        _db.migrate(conn)
        conn.close()

    def test_open_store_empty_falls_back_to_env_or_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOMATON_DB", str(tmp_path / "env.db"))
        monkeypatch.delenv("AUTOMATON_DB_URL", raising=False)
        conn = _db.open_store()
        assert not getattr(conn, "is_postgres", False)
        conn.close()

    @_REQUIRES_PG
    def test_open_store_postgresql_url_returns_pgconn(self):
        conn = _db.open_store(PG_URL)
        assert getattr(conn, "is_postgres", False)
        conn.close()
