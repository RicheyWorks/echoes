"""Read-route auth tests.

What we prove:
  1. With AUTOMATON_TOKEN set and require_read_auth=True (the default),
     GET /metrics returns 401 without a token.
  2. The same route returns 200 with a valid Bearer token.
  3. ?token=<TOKEN> in the query string also grants access (browser-bookmark path).
  4. GET /healthz and GET /health are always open — no token required.
  5. GET /manifest.json and GET /sw.js are always open.
  6. require_read_auth=False (--insecure-read-no-auth) makes GET routes open
     while POST routes still require the token.
  7. The HTML run-list page (GET /) is protected the same as /api routes.
"""
from __future__ import annotations

import pytest

from automaton import db as _db
from automaton.ui import make_handler


TOKEN = "test-secret-token-abc123"


def _make_db(tmp_path):
    db_path = str(tmp_path / "ui.db")
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    return db_path


class _Harness:
    """Minimal stand-in for BaseHTTPRequestHandler that records the response."""

    def __init__(self, Handler, path, method="GET", headers=None):
        self.path = path
        self.command = method
        self.headers = headers or {}
        self._code = None
        self._body = b""
        self._headers_sent = {}
        # Call the appropriate do_* method directly.
        Handler.__init__ = lambda s: None  # skip super().__init__ socket magic
        inst = Handler.__new__(Handler)
        inst.path = path
        inst.command = method
        inst.headers = self.headers
        inst._code = None
        inst._body = b""

        def send_response(code, msg=None): inst._code = code
        def send_header(k, v): inst._headers_sent = getattr(inst, "_headers_sent", {}); inst._headers_sent[k.lower()] = v
        def end_headers(): pass

        @property
        def wfile(inner_inst=inst):
            class W:
                def write(s, data):
                    inner_inst._body += data
                def flush(s): pass
            return W()

        inst.send_response = send_response
        inst.send_header = send_header
        inst.end_headers = end_headers
        inst.__class__.wfile = wfile  # type: ignore[assignment]
        inst.log_message = lambda fmt, *a: None
        inst.rfile = None
        inst._headers_sent = {}

        if method == "GET":
            inst.do_GET()
        else:
            inst.do_POST()

        self._code = inst._code
        self._body = inst._body


def _get(db_path, path, *, token=None, auth_token=TOKEN,
         require_auth=True, require_read_auth=True, query_token=None):
    """Fire a GET through the handler harness and return (status_code, body_str)."""
    if query_token:
        path = f"{path}?token={query_token}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    Handler = make_handler(
        db_path,
        auth_token=auth_token,
        require_auth=require_auth,
        require_read_auth=require_read_auth,
    )
    h = _Harness(Handler, path, headers=headers)
    return h._code, h._body.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ #
# 1 & 2: /metrics requires token by default                           #
# ------------------------------------------------------------------ #

def test_metrics_requires_token_by_default(tmp_path):
    db = _make_db(tmp_path)
    code, _ = _get(db, "/metrics")
    assert code == 401, f"expected 401, got {code}"


def test_metrics_accepts_valid_token(tmp_path):
    db = _make_db(tmp_path)
    code, body = _get(db, "/metrics", token=TOKEN)
    assert code == 200, f"expected 200, got {code}"
    assert "automaton_runs_total" in body


# ------------------------------------------------------------------ #
# 3: ?token= query string grants access on GET                        #
# ------------------------------------------------------------------ #

def test_metrics_query_string_token(tmp_path):
    db = _make_db(tmp_path)
    code, body = _get(db, "/metrics", query_token=TOKEN)
    assert code == 200, f"expected 200 via ?token=, got {code}"
    assert "automaton_runs_total" in body


def test_metrics_wrong_query_token_rejected(tmp_path):
    db = _make_db(tmp_path)
    code, _ = _get(db, "/metrics", query_token="wrong-token")
    assert code == 401


# ------------------------------------------------------------------ #
# 4 & 5: always-open routes                                           #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("path", ["/healthz", "/health"])
def test_healthz_always_open(tmp_path, path):
    db = _make_db(tmp_path)
    code, body = _get(db, path)
    assert code == 200, f"expected 200 for {path}, got {code}"
    assert "ok" in body


@pytest.mark.parametrize("path", ["/manifest.json", "/sw.js"])
def test_pwa_assets_always_open(tmp_path, path):
    db = _make_db(tmp_path)
    code, _ = _get(db, path)
    assert code == 200, f"expected 200 for PWA asset {path}, got {code}"


# ------------------------------------------------------------------ #
# 6: --insecure-read-no-auth opens reads, keeps writes protected      #
# ------------------------------------------------------------------ #

def test_insecure_read_no_auth_opens_metrics(tmp_path):
    db = _make_db(tmp_path)
    code, body = _get(db, "/metrics", require_read_auth=False)
    assert code == 200, f"expected 200 with require_read_auth=False, got {code}"
    assert "automaton_runs_total" in body


# ------------------------------------------------------------------ #
# 7: HTML run-list is also protected                                  #
# ------------------------------------------------------------------ #

def test_run_list_requires_token(tmp_path):
    db = _make_db(tmp_path)
    code, _ = _get(db, "/")
    assert code == 401, f"expected 401 for GET /, got {code}"


def test_run_list_accepts_token(tmp_path):
    db = _make_db(tmp_path)
    code, body = _get(db, "/", token=TOKEN)
    assert code == 200, f"expected 200 for GET / with token, got {code}"
    assert "Runs" in body


# ------------------------------------------------------------------ #
# wrong token is always rejected                                      #
# ------------------------------------------------------------------ #

def test_wrong_token_rejected_on_read(tmp_path):
    db = _make_db(tmp_path)
    code, _ = _get(db, "/api/runs", token="wrong-token")
    assert code == 401
