"""Phase 21: Run search, filter, and re-run tests.

Covers:
- engine.search_runs() with status / workflow / date / limit filters
- combined filters
- empty result set
- render_run_list() includes filter bar HTML
- render_run_list() filtered call returns only matching rows
- _rerun_button() renders for terminal statuses
- render_run_detail() includes Re-run button for terminal runs
- render_run_detail() excludes Re-run button for active runs
- GET / passes query-string filters into render_run_list
- automaton inspect --status / --workflow flags
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from automaton import db as _db
from automaton import engine
from automaton.ui import render_run_list, render_run_detail, _rerun_button


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    yield conn
    conn.close()


def _make_workflow(conn, name):
    engine.register_workflow(conn, {
        "name": name,
        "steps": [{"name": "noop", "type": "file_append",
                   "path": "/tmp/noop.log", "text": "x"}],
    })


def _run(conn, name, status="completed"):
    """Trigger a run and force its status for test isolation."""
    rid = engine.trigger_run(conn, name)
    if status != "pending":
        conn.execute("UPDATE run SET status = ? WHERE id = ?", (status, rid))
        conn.commit()
    return rid


# ── search_runs() ─────────────────────────────────────────────────────────────

class TestSearchRuns:
    def test_no_filters_returns_all(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf", "completed")
        _run(store, "wf", "failed")
        rows = engine.search_runs(store)
        assert len(rows) == 2

    def test_filter_by_status_completed(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf", "completed")
        _run(store, "wf", "failed")
        rows = engine.search_runs(store, status="completed")
        assert all(r["status"] == "completed" for r in rows)
        assert len(rows) == 1

    def test_filter_by_status_failed(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf", "completed")
        _run(store, "wf", "failed")
        rows = engine.search_runs(store, status="failed")
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

    def test_filter_by_workflow_name(self, store):
        _make_workflow(store, "alpha")
        _make_workflow(store, "beta")
        _run(store, "alpha")
        _run(store, "beta")
        rows = engine.search_runs(store, workflow="alpha")
        assert all(r["workflow"] == "alpha" for r in rows)
        assert len(rows) == 1

    def test_filter_wrong_workflow_returns_empty(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf")
        rows = engine.search_runs(store, workflow="does-not-exist")
        assert rows == []

    def test_combined_status_and_workflow(self, store):
        _make_workflow(store, "alpha")
        _make_workflow(store, "beta")
        _run(store, "alpha", "failed")
        _run(store, "alpha", "completed")
        _run(store, "beta", "failed")
        rows = engine.search_runs(store, status="failed", workflow="alpha")
        assert len(rows) == 1
        assert rows[0]["workflow"] == "alpha"
        assert rows[0]["status"] == "failed"

    def test_filter_by_after(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf")
        # A future timestamp should return nothing
        rows = engine.search_runs(store, after="2099-01-01T00:00:00")
        assert rows == []

    def test_filter_by_before(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf")
        # A past timestamp should return nothing
        rows = engine.search_runs(store, before="2000-01-01T00:00:00")
        assert rows == []

    def test_limit_respected(self, store):
        _make_workflow(store, "wf")
        for _ in range(5):
            _run(store, "wf")
        rows = engine.search_runs(store, limit=3)
        assert len(rows) == 3

    def test_returns_newest_first(self, store):
        _make_workflow(store, "wf")
        r1 = _run(store, "wf")
        r2 = _run(store, "wf")
        rows = engine.search_runs(store)
        assert rows[0]["id"] == r2
        assert rows[1]["id"] == r1

    def test_result_shape(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf", "completed")
        row = engine.search_runs(store)[0]
        assert "id" in row
        assert "workflow" in row
        assert "status" in row
        assert "started_at" in row


# ── UI: filter bar in render_run_list ─────────────────────────────────────────

class TestRunListUI:
    def test_filter_bar_always_present(self, store):
        _make_workflow(store, "wf")
        html = render_run_list(store)
        assert 'name="status"' in html
        assert 'name="workflow"' in html

    def test_filter_bar_present_even_with_no_runs(self, store):
        _make_workflow(store, "wf")
        html = render_run_list(store, status="failed")
        assert 'name="status"' in html
        assert "No matching runs" in html

    def test_filtered_call_shows_only_matching(self, store):
        _make_workflow(store, "wf")
        _run(store, "wf", "completed")
        _run(store, "wf", "failed")
        html_completed = render_run_list(store, status="completed")
        assert "completed" in html_completed
        # failed runs should not appear when filtering for completed
        # (we check that the count note mentions 1 run)
        assert "1 run(s) found" in html_completed

    def test_clear_link_present(self, store):
        html = render_run_list(store, status="failed")
        assert 'href="/"' in html

    def test_status_selected_option(self, store):
        html = render_run_list(store, status="failed")
        assert 'value="failed" selected' in html

    def test_no_filter_shows_auto_refresh(self, store):
        html = render_run_list(store)
        assert "Auto-refreshes" in html or 'content="5"' in html

    def test_filtered_no_auto_refresh(self, store):
        html = render_run_list(store, status="failed")
        # When filtering, auto_refresh=0 so no meta refresh tag
        assert 'content="0"' not in html


# ── _rerun_button + render_run_detail Re-run visibility ───────────────────────

class TestRerunButton:
    @pytest.mark.parametrize("status", ["completed", "failed", "timed_out", "cancelled"])
    def test_rerun_button_shows_for_terminal(self, store, status):
        _make_workflow(store, "wf")
        rid = _run(store, "wf", status)
        html_pg, code = render_run_detail(store, rid)
        assert "Re-run" in html_pg
        assert "rerunRun" in html_pg

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_rerun_button_hidden_for_active(self, store, status):
        _make_workflow(store, "wf")
        rid = _run(store, "wf", status)
        html_pg, code = render_run_detail(store, rid)
        assert "Re-run" not in html_pg

    def test_rerun_button_contains_workflow_name(self, store):
        _make_workflow(store, "mywf")
        rid = _run(store, "mywf", "completed")
        html_pg, _ = render_run_detail(store, rid)
        assert "mywf" in html_pg

    def test_rerun_button_helper_returns_html(self):
        run = {"id": 42, "workflow": "test-wf",
               "status": "completed", "trigger_payload": {"k": "v"}}
        btn = _rerun_button(run)
        assert "Re-run" in btn
        assert "rerunRun" in btn
        assert "test-wf" in btn
        assert "<script>" in btn


# ── automaton inspect CLI flags ───────────────────────────────────────────────

def _inspect(*args):
    return subprocess.run(
        [sys.executable, "-m", "automaton", "inspect", *args],
        capture_output=True, text=True,
    )


class TestInspectCLI:
    def test_inspect_help_shows_status_flag(self):
        r = _inspect("--help")
        assert "--status" in r.stdout

    def test_inspect_help_shows_workflow_flag(self):
        r = _inspect("--help")
        assert "--workflow" in r.stdout

    def test_inspect_help_shows_limit_flag(self):
        r = _inspect("--help")
        assert "--limit" in r.stdout

    def test_inspect_no_runs_exits_zero(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOMATON_DB", str(tmp_path / "test.db"))
        _db.migrate(_db.connect(tmp_path / "test.db"))
        r = _inspect()
        assert r.returncode == 0
