"""Web UI: Tailwind shell, PWA bits, SSE live updates, query-string token.

Spins up the real ui.serve() in a background thread on an ephemeral
port. Tests hit it with http.client / urllib so we exercise the full
HTTP path the browser will see.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.request

import pytest

from automaton import db as _db
from automaton import engine
from automaton import ui as _ui


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    return port


@pytest.fixture
def server(tmp_path):
    db_path = tmp_path / "ui.db"
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    port = _free_port()
    httpd = _ui.serve(str(db_path), host="127.0.0.1", port=port,
                       auth_token="testtoken", insecure_read_no_auth=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{port}", str(db_path)
    httpd.shutdown()
    httpd.server_close()


def _get(host: str, path: str, headers=None):
    c = http.client.HTTPConnection(host, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body.decode("utf-8", errors="replace")


def _post(host: str, path: str, body: str = "", headers=None):
    c = http.client.HTTPConnection(host, timeout=5)
    c.request("POST", path, body=body, headers=headers or {})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out.decode("utf-8", errors="replace")


# --------- HTML shell ---------

def test_runs_page_uses_tailwind_cdn(server):
    host, _ = server
    status, headers, body = _get(host, "/")
    assert status == 200
    assert "https://cdn.tailwindcss.com" in body
    assert "Strict-Transport-Security" not in headers  # we're plain HTTP here
    assert "/manifest.json" in body
    assert "/sw.js" in body


def test_runs_page_renders_responsive_layouts(server):
    """Both the mobile-only card section and the desktop-only table
    are in the markup; CSS handles which one shows."""
    host, db_path = server
    # Need at least one run to render the cards/rows.
    conn = _db.connect(db_path)
    engine.register_workflow(conn, {
        "name": "ui_demo",
        "steps": [{"name": "n", "type": "shell",
                   "cmd": ["sh", "-c", "true"]}],
    })
    engine.trigger_run(conn, "ui_demo")
    engine.worker_loop(conn, stop_when_idle=True)
    conn.close()
    _, _, body = _get(host, "/")
    assert 'class="flex flex-col gap-2 sm:hidden"' in body  # mobile cards
    assert 'class="hidden sm:block overflow-x-auto"' in body  # desktop table


# --------- PWA bits ---------

def test_manifest_json_is_valid(server):
    host, _ = server
    status, headers, body = _get(host, "/manifest.json")
    assert status == 200
    assert headers["Content-Type"].startswith("application/manifest+json")
    parsed = json.loads(body)
    assert parsed["short_name"] == "automaton"
    assert parsed["start_url"] == "/"
    assert parsed["display"] == "standalone"
    assert any(i.get("src", "").startswith("data:image/svg")
               for i in parsed["icons"])


def test_service_worker_served_as_javascript(server):
    host, _ = server
    status, headers, body = _get(host, "/sw.js")
    assert status == 200
    assert headers["Content-Type"].startswith("application/javascript")
    assert "addEventListener" in body
    assert "automaton-v1" in body  # the cache name we ship


# --------- query-string token auth ---------

def test_query_string_token_works_for_get(server):
    """A bearer token in ?token=... is accepted for GETs only - lets a
    browser bookmark work without the Authorization header."""
    host, _ = server
    # Read routes don't require auth today, so this is more meaningful
    # as a probe of the fallback path. Just confirm the URL with a token
    # query still returns 200, no 401.
    status, _, _ = _get(host, "/?token=testtoken")
    assert status == 200


def test_query_string_token_does_not_work_for_post(server):
    """Same token on a POST must be rejected - posts go through the
    Authorization header, never the URL."""
    host, _ = server
    # POST to a route that requires auth.
    status, body = _post(
        host, "/api/trigger/anything?token=testtoken",
        body="", headers={"Content-Type": "application/json"},
    )
    assert status == 401, body


def test_bearer_header_still_works(server):
    host, _ = server
    status, _ = _post(
        host, "/api/trigger/nope",
        body="", headers={"Authorization": "Bearer testtoken"},
    )
    # 404 (no such workflow) is the success-path here; the auth check
    # passed before the routing logic returned 404.
    assert status == 404


# --------- SSE: live run status ---------

def test_sse_endpoint_emits_initial_status(server):
    """The /api/run/<id>/events stream emits at least one frame with the
    current status, then closes once the run reaches a terminal state."""
    host, db_path = server
    conn = _db.connect(db_path)
    engine.register_workflow(conn, {
        "name": "sse_demo",
        "steps": [{"name": "n", "type": "shell",
                   "cmd": ["sh", "-c", "true"]}],
    })
    rid = engine.trigger_run(conn, "sse_demo")
    engine.worker_loop(conn, stop_when_idle=True)  # already terminal
    conn.close()

    # Open the SSE connection; read the first frame; assert shape.
    c = http.client.HTTPConnection(host, timeout=5)
    c.request("GET", f"/api/run/{rid}/events")
    r = c.getresponse()
    assert r.status == 200
    assert r.getheader("Content-Type") == "text/event-stream"

    # Read line-by-line; the first frame arrives within a poll cycle.
    payload = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        line = r.fp.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").rstrip()
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            break
    c.close()
    assert payload is not None and payload["run_id"] == rid
    assert payload["status"] in ("completed", "failed", "running", "pending")


def test_sse_unknown_run_returns_error_frame(server):
    """A request for a nonexistent run id emits one error frame and closes."""
    host, _ = server
    c = http.client.HTTPConnection(host, timeout=5)
    c.request("GET", "/api/run/9999/events")
    r = c.getresponse()
    saw_error = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        line = r.fp.readline()
        if not line:
            break
        if b"not found" in line:
            saw_error = True
            break
    c.close()
    assert saw_error


# --------- run detail page wires up EventSource when in-flight ---------

def test_run_detail_includes_eventsource_when_pending(server):
    """If the run is still in flight, the rendered HTML embeds the
    EventSource hookup so the status pill updates without polling."""
    host, db_path = server
    conn = _db.connect(db_path)
    engine.register_workflow(conn, {
        "name": "parky",
        "steps": [{"name": "wait", "type": "wait_for_signal",
                   "signal": "go", "poll_seconds": 999}],
    })
    rid = engine.trigger_run(conn, "parky")
    engine.worker_loop(conn, stop_when_idle=True)  # parks
    conn.close()
    _, _, body = _get(host, f"/run/{rid}")
    assert f'/api/run/{rid}/events' in body
    assert "new EventSource" in body


def test_run_detail_omits_eventsource_when_terminal(server):
    host, db_path = server
    conn = _db.connect(db_path)
    engine.register_workflow(conn, {
        "name": "done",
        "steps": [{"name": "n", "type": "shell",
                   "cmd": ["sh", "-c", "true"]}],
    })
    rid = engine.trigger_run(conn, "done")
    engine.worker_loop(conn, stop_when_idle=True)
    conn.close()
    _, _, body = _get(host, f"/run/{rid}")
    assert "new EventSource" not in body
