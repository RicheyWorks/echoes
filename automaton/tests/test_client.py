"""AutomatonClient tests, against the real ui.serve() running in a thread.

These exercise the full HTTP path - the client wraps it as method calls.
"""
from __future__ import annotations

import threading
import time

import pytest

from automaton import db as _db
from automaton import ui as _ui
from automaton.client import AutomatonClient, AutomatonError


@pytest.fixture
def server(tmp_path):
    db_path = tmp_path / "test.db"
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    httpd = _ui.serve(str(db_path), host="127.0.0.1", port=0,
                      auth_token="testtoken")
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", "testtoken", str(db_path)
    httpd.shutdown()
    httpd.server_close()


def test_health(server):
    base, token, _ = server
    with AutomatonClient(base, token=token, trust_env=False) as c:
        assert c.health()["ok"] is True


def test_register_and_trigger_and_inspect(server, tmp_path):
    base, token, _ = server
    with AutomatonClient(base, token=token, trust_env=False) as c:
        wf = c.register_workflow({
            "name": "client_wf",
            "steps": [{"name": "noop", "type": "file_append",
                       "path": str(tmp_path / "automaton-client-test.log"),
                       "text": "via client"}],
        })
        assert wf["name"] == "client_wf"
        assert wf["workflow_def_id"] > 0

        r = c.trigger("client_wf", payload={"caller": "tester"})
        assert "run_id" in r
        run = c.run(r["run_id"])
        assert run["run"]["workflow"] == "client_wf"


def test_unauthorized_post_raises(server):
    base, _, _ = server
    with AutomatonClient(base, trust_env=False) as c:  # no token
        with pytest.raises(AutomatonError) as exc:
            c.trigger("anything")
        assert exc.value.status_code == 401


def test_step_types(server):
    base, token, _ = server
    with AutomatonClient(base, token=token, trust_env=False) as c:
        types = c.step_types()
        assert "http_get" in types["types"]
        assert "wait_for_signal" in types["types"]
        assert "shell" in types["types"]


def test_signed_webhook_round_trip(server, tmp_path):
    """End-to-end: register endpoint, then use the client's signed-webhook
    helper to fire it."""
    base, token, db_path = server
    # Register the target workflow
    with AutomatonClient(base, token=token, trust_env=False) as c:
        c.register_workflow({
            "name": "hooked",
            "steps": [{"name": "n", "type": "file_append",
                       "path": str(tmp_path / "hooked.log"),
                       "text": "got webhook"}],
        })

    # Register a webhook endpoint via the helper module (CLI path would also work)
    from automaton import webhooks
    conn = _db.connect(db_path)
    wid, secret = webhooks.register_webhook(conn, "test-hook", "hooked")
    conn.close()

    # Fire it via the client. The client signs the body and posts.
    with AutomatonClient(base, token=token, trust_env=False) as c:
        r = c.send_signed_webhook("test-hook", secret,
                                  body={"event": "test", "x": 1})
        assert "run_id" in r
        assert r["endpoint"] == "test-hook"
