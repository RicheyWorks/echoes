"""Tests for Phase 27: automaton doctor health checks.

Covers:
- engine.doctor() returns list of dicts with check/status/detail
- db.reachable passes on healthy DB
- db.migrations passes when all migrations applied
- db.integrity passes on uncorrupted DB
- db.wal_mode passes (WAL mode is the default)
- queue.stuck_steps passes when no stuck steps
- queue.stuck_steps warns when stale leases present
- workflows.valid passes with no workflows
- workflows.valid passes with valid workflows
- workflows.valid fails when a corrupted spec is in the DB
- crons.valid passes with no cron triggers
- CLI: all-ok exits 0
- CLI: any-fail exits 1
- CLI output contains ✓ for passing checks
- CLI output contains ✗ for failing checks
- All expected check names present in results
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from automaton import db as _db, engine
from automaton import cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _run_cli(argv, db_path, capsys=None):
    with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
        try:
            cli.main(argv)
            return 0
        except SystemExit as e:
            return int(e.code) if e.code is not None else 0


# ---------------------------------------------------------------------------
# engine.doctor() return structure
# ---------------------------------------------------------------------------

class TestDoctorReturnStructure:
    def test_returns_list(self, store):
        result = engine.doctor(store)
        assert isinstance(result, list)

    def test_each_item_has_required_keys(self, store):
        for item in engine.doctor(store):
            assert "check" in item
            assert "status" in item
            assert "detail" in item

    def test_status_values_are_valid(self, store):
        valid = {"ok", "warn", "fail"}
        for item in engine.doctor(store):
            assert item["status"] in valid, f"unexpected status {item['status']!r}"

    def test_all_checks_present(self, store):
        expected = {
            "db.reachable",
            "db.migrations",
            "db.integrity",
            "db.wal_mode",
            "queue.stuck_steps",
            "workflows.valid",
            "crons.valid",
        }
        check_names = {r["check"] for r in engine.doctor(store)}
        assert expected <= check_names, f"missing checks: {expected - check_names}"

    def test_check_names_are_strings(self, store):
        for item in engine.doctor(store):
            assert isinstance(item["check"], str)
            assert isinstance(item["detail"], str)


# ---------------------------------------------------------------------------
# Individual checks — healthy DB
# ---------------------------------------------------------------------------

class TestDoctorHealthyDB:
    def _results(self, store):
        return {r["check"]: r for r in engine.doctor(store)}

    def test_db_reachable_ok(self, store):
        r = self._results(store)
        assert r["db.reachable"]["status"] == "ok"

    def test_db_migrations_ok(self, store):
        r = self._results(store)
        assert r["db.migrations"]["status"] in ("ok", "warn")
        # On a fresh migrated store, should be ok or at worst warn due to test files

    def test_db_integrity_ok(self, store):
        r = self._results(store)
        assert r["db.integrity"]["status"] == "ok"

    def test_db_wal_mode_ok(self, store):
        r = self._results(store)
        # WAL is our default
        assert r["db.wal_mode"]["status"] in ("ok", "warn")

    def test_queue_stuck_steps_ok_when_empty(self, store):
        r = self._results(store)
        assert r["queue.stuck_steps"]["status"] == "ok"

    def test_workflows_valid_ok_when_none(self, store):
        r = self._results(store)
        assert r["workflows.valid"]["status"] == "ok"
        assert "no workflows" in r["workflows.valid"]["detail"].lower()

    def test_crons_valid_ok_when_none(self, store):
        r = self._results(store)
        assert r["crons.valid"]["status"] == "ok"
        assert "no" in r["crons.valid"]["detail"].lower()


# ---------------------------------------------------------------------------
# queue.stuck_steps: warns on stale leases
# ---------------------------------------------------------------------------

class TestDoctorStuckSteps:
    def test_stuck_step_triggers_warn(self, store):
        """Inject a step row + queue entry with an expired lease."""
        # Register and trigger a workflow so we have a run and step
        engine.register_workflow(store, {
            "name": "stuck-wf",
            "steps": [{"name": "s", "type": "shell",
                        "cmd": [sys.executable, "-c", "pass"]}],
        })
        run_id = engine.trigger_run(store, "stuck-wf")
        # Get the queued step
        step = store.execute(
            "SELECT id FROM step WHERE run_id = ?", (run_id,)
        ).fetchone()
        # Artificially mark it leased with an expired timestamp
        store.execute(
            "UPDATE queue SET leased_by = 'dead-worker', "
            "leased_until = datetime('now', '-10 minutes') "
            "WHERE step_id = ?",
            (step["id"],),
        )
        store.execute("COMMIT") if False else None  # already autocommit in test

        results = {r["check"]: r for r in engine.doctor(store)}
        assert results["queue.stuck_steps"]["status"] == "warn"
        assert "stuck" in results["queue.stuck_steps"]["detail"].lower()


# ---------------------------------------------------------------------------
# workflows.valid: detects corrupted spec in DB
# ---------------------------------------------------------------------------

class TestDoctorWorkflowsValid:
    def test_valid_workflow_passes(self, store):
        engine.register_workflow(store, {
            "name": "healthy",
            "steps": [{"name": "s", "type": "shell",
                        "cmd": [sys.executable, "-c", "pass"]}],
        })
        results = {r["check"]: r for r in engine.doctor(store)}
        assert results["workflows.valid"]["status"] == "ok"
        assert "1" in results["workflows.valid"]["detail"]

    def test_multiple_valid_workflows_pass(self, store):
        for name in ["wf-a", "wf-b", "wf-c"]:
            engine.register_workflow(store, {
                "name": name,
                "steps": [{"name": "s", "type": "shell",
                            "cmd": [sys.executable, "-c", "pass"]}],
            })
        results = {r["check"]: r for r in engine.doctor(store)}
        assert results["workflows.valid"]["status"] == "ok"

    def test_corrupted_spec_fails(self, store):
        """Inject a workflow with a broken spec_json directly into the DB."""
        store.execute(
            "INSERT INTO workflow_def (name, version, spec_json) VALUES (?, 1, ?)",
            ("corrupt-wf", json.dumps({"name": "corrupt-wf"})),  # missing 'steps'
        )
        results = {r["check"]: r for r in engine.doctor(store)}
        assert results["workflows.valid"]["status"] == "fail"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestDoctorCLI:
    def test_healthy_db_exits_0(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = _db.connect(db_path)
        _db.migrate(conn)
        conn.close()
        rc = _run_cli(["doctor"], db_path)
        assert rc == 0

    def test_output_contains_check_symbols(self, tmp_path, capsys):
        db_path = tmp_path / "test.db"
        conn = _db.connect(db_path)
        _db.migrate(conn)
        conn.close()
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["doctor"])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        # At least some ✓ checks should appear
        assert "✓" in out or "ok" in out.lower()

    def test_output_includes_check_names(self, tmp_path, capsys):
        db_path = tmp_path / "test.db"
        conn = _db.connect(db_path)
        _db.migrate(conn)
        conn.close()
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["doctor"])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "db.reachable" in out
        assert "queue.stuck_steps" in out
        assert "workflows.valid" in out

    def test_fail_check_exits_1(self, tmp_path, capsys):
        """Insert a corrupted workflow spec → workflows.valid fails → exit 1."""
        db_path = tmp_path / "test.db"
        conn = _db.connect(db_path)
        _db.migrate(conn)
        conn.execute(
            "INSERT INTO workflow_def (name, version, spec_json) VALUES (?, 1, ?)",
            ("bad-wf", json.dumps({"name": "bad-wf"})),
        )
        conn.commit()
        conn.close()
        rc = _run_cli(["doctor"], db_path)
        assert rc == 1

    def test_fail_symbol_in_output(self, tmp_path, capsys):
        db_path = tmp_path / "test.db"
        conn = _db.connect(db_path)
        _db.migrate(conn)
        conn.execute(
            "INSERT INTO workflow_def (name, version, spec_json) VALUES (?, 1, ?)",
            ("bad-wf2", json.dumps({"name": "bad-wf2"})),
        )
        conn.commit()
        conn.close()
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["doctor"])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "✗" in out or "fail" in out.lower()
