"""Prometheus /metrics endpoint.

Covers:
  - collect() with an empty DB produces all metric families.
  - Label format is valid (no spaces inside braces).
  - Counts reflect actual DB state: seeded runs, queue entries, cron triggers.
  - db_size_bytes is included when db_path is provided and omitted otherwise.
  - The /metrics HTTP route returns 200 with the correct Content-Type.
  - No auth is required on /metrics (same as /healthz).
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from automaton import db as _db
from automaton import engine
from automaton import metrics as _m
from automaton import scheduler as _sched


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn, tmp_path / "test.db"


# ------------------------------------------------------------------ #
# collect() unit tests                                                 #
# ------------------------------------------------------------------ #

def test_empty_db_has_all_families(store):
    conn, path = store
    out = _m.collect(conn, path)
    assert "# TYPE automaton_runs_total counter" in out
    assert "# TYPE automaton_runs_active gauge" in out
    assert "# TYPE automaton_queue_depth gauge" in out
    assert "# TYPE automaton_cron_triggers gauge" in out
    assert "# TYPE automaton_db_size_bytes gauge" in out


def test_label_format_no_spaces_in_braces(store):
    """Prometheus requires no whitespace between { and the first label key."""
    conn, path = store
    out = _m.collect(conn, path)
    # Matches any { followed by a space before the label key.
    bad = re.findall(r"\{[ \t]+\w", out)
    assert not bad, f"bad label format: {bad}"


def test_all_terminal_statuses_present(store):
    conn, path = store
    out = _m.collect(conn, path)
    for status in ("completed", "failed", "cancelled", "timed_out"):
        assert f'automaton_runs_total{{status="{status}"}}' in out, \
            f"missing status={status!r}"


def test_run_counts_reflect_db_state(store):
    conn, path = store
    engine.register_workflow(conn, {
        "name": "wf",
        "steps": [{"name": "s", "type": "shell", "cmd": ["true"]}],
    })
    engine.trigger_run(conn, "wf")           # → pending
    engine.worker_loop(conn, stop_when_idle=True)  # → completed

    out = _m.collect(conn, path)
    assert 'automaton_runs_total{status="completed"} 1' in out
    assert 'automaton_runs_total{status="failed"} 0' in out
    assert 'automaton_runs_active{status="pending"} 0' in out
    assert 'automaton_runs_active{status="running"} 0' in out


def test_queue_depth_counts_pending_steps(store):
    conn, path = store
    engine.register_workflow(conn, {
        "name": "wf",
        "steps": [{"name": "s", "type": "shell", "cmd": ["true"]}],
    })
    engine.trigger_run(conn, "wf")   # queues one step, doesn't run it

    out = _m.collect(conn, path)
    assert "automaton_queue_depth 1" in out


def test_cron_trigger_counts(store):
    conn, path = store
    engine.register_workflow(conn, {
        "name": "wf",
        "steps": [{"name": "s", "type": "shell", "cmd": ["true"]}],
    })
    _sched.register_cron(conn, "wf", "0 9 * * *")   # enabled by default

    out = _m.collect(conn, path)
    assert 'automaton_cron_triggers{enabled="true"} 1' in out
    assert 'automaton_cron_triggers{enabled="false"} 0' in out


def test_db_size_included_when_path_given(store):
    conn, path = store
    out = _m.collect(conn, path)
    assert "automaton_db_size_bytes" in out
    # Value should be a positive integer.
    m = re.search(r"automaton_db_size_bytes (\d+)", out)
    assert m and int(m.group(1)) > 0


def test_db_size_omitted_when_no_path(store):
    conn, _ = store
    out = _m.collect(conn, db_path=None)
    assert "automaton_db_size_bytes" not in out


def test_output_ends_with_newline(store):
    conn, path = store
    out = _m.collect(conn, path)
    assert out.endswith("\n"), "Prometheus parsers expect a trailing newline"


# ------------------------------------------------------------------ #
# HTTP route tests                                                     #
# ------------------------------------------------------------------ #

def _make_ui_handler(tmp_path):
    """Stand up the HTTP handler pointed at a fresh DB."""
    from automaton.ui import make_handler
    db_path = str(tmp_path / "ui.db")
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    return make_handler(db_path, auth_token="secret",
                        require_auth=True, tls_enabled=False)


class FakeRequest:
    """Minimal httplib request stub for make_handler unit tests."""
    def __init__(self, path):
        self.path = path
        self.command = "GET"
        self.headers = {}
        self._written = []

    def send_response(self, code): self._code = code
    def send_header(self, k, v): pass
    def end_headers(self): pass
    def write(self, data): self._written.append(data)

    @property
    def wfile(self):
        inner = self
        class W:
            def write(self, data): inner._written.append(data)
        return W()

    def log_message(self, *a): pass


def test_metrics_route_returns_200(tmp_path):
    """GET /metrics should return 200 with the Prometheus content type."""
    from io import BytesIO
    from http.server import BaseHTTPRequestHandler
    import socket

    db_path = str(tmp_path / "ui.db")
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()

    from automaton.ui import make_handler
    Handler = make_handler(db_path, auth_token=None,
                           require_auth=False, tls_enabled=False)

    responses = []
    bodies = []

    class Harness(Handler):
        def __init__(self):
            # Skip BaseHTTPRequestHandler.__init__; wire up manually.
            self.path = "/metrics"
            self.command = "GET"
            self.headers = {}
            self._response_code = None
            self._headers = {}
            self._body = b""

        def send_response(self, code, msg=None):
            self._response_code = code

        def send_header(self, key, val):
            self._headers[key.lower()] = val

        def end_headers(self):
            pass

        @property
        def wfile(self):
            outer = self
            class W:
                def write(self, data):
                    outer._body += data
            return W()

        def log_message(self, *a):
            pass

    h = Harness()
    h.do_GET()

    assert h._response_code == 200
    assert "text/plain" in h._headers.get("content-type", "")
    body = h._body.decode("utf-8")
    assert "automaton_runs_total" in body


def test_metrics_requires_no_auth(tmp_path):
    """Unlike write routes, /metrics must be accessible without a token."""
    db_path = str(tmp_path / "ui.db")
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()

    from automaton.ui import make_handler
    Handler = make_handler(db_path, auth_token="secret",
                           require_auth=True, tls_enabled=False)

    class Harness(Handler):
        def __init__(self):
            self.path = "/metrics"
            self.command = "GET"
            self.headers = {}
            self._response_code = None
            self._body = b""

        def send_response(self, code, msg=None):
            self._response_code = code

        def send_header(self, *a): pass
        def end_headers(self): pass

        @property
        def wfile(self):
            outer = self
            class W:
                def write(self, data): outer._body += data
            return W()

        def log_message(self, *a): pass

    h = Harness()
    h.do_GET()
    assert h._response_code == 200, \
        f"/metrics returned {h._response_code} — should be open like /healthz"
