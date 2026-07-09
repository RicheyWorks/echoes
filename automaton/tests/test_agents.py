"""Agent memory API — Option C integration tests.

Spins up the real ui.serve() in a background thread on an ephemeral port,
then exercises every agent route via HTTP.  Tests cover:

  - GET  /api/agents            list (empty + populated)
  - GET  /api/agents/<n>/meta   not found + found
  - POST /api/agents/<n>/meta   create + idempotent update
  - GET  /api/agents/<n>/entries tick ordering + empty list
  - POST /api/agents/<n>/entries create entry + duplicate tick 409
  - Auth enforcement on both GET (with insecure_read_no_auth=False) and POST

All writes go through the HTTP layer to verify the full stack from routing
down to the SQLite tables created by migration 0004.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading

import pytest

from automaton import db as _db
from automaton import ui as _ui


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(tmp_path):
    """Server with auth required on reads AND writes."""
    db_path = tmp_path / "agents_test.db"
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    port = _free_port()
    httpd = _ui.serve(
        str(db_path),
        host="127.0.0.1",
        port=port,
        auth_token="s3cr3t",
        insecure_read_no_auth=False,   # reads also require auth
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{port}", str(db_path)
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def open_server(tmp_path):
    """Server with insecure_read_no_auth=True for non-auth GET tests."""
    db_path = tmp_path / "agents_open.db"
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()
    port = _free_port()
    httpd = _ui.serve(
        str(db_path),
        host="127.0.0.1",
        port=port,
        auth_token="s3cr3t",
        insecure_read_no_auth=True,
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{port}", str(db_path)
    httpd.shutdown()
    httpd.server_close()


def _get(host, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    c = http.client.HTTPConnection(host, timeout=5)
    c.request("GET", path, headers=headers)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, json.loads(body)


def _post(host, path, payload, token=None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    c = http.client.HTTPConnection(host, timeout=5)
    c.request("POST", path, body=body, headers=headers)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, json.loads(out)


TOKEN = "s3cr3t"


# ---------------------------------------------------------------------------
# GET /api/agents
# ---------------------------------------------------------------------------

def test_list_agents_empty(open_server):
    host, _ = open_server
    status, body = _get(host, "/api/agents")
    assert status == 200
    assert body == []


def test_list_agents_shows_created_agent(open_server):
    host, _ = open_server
    _post(host, "/api/agents/scout/meta", {"goal": "patrol", "tick": 0}, token=TOKEN)
    status, body = _get(host, "/api/agents")
    assert status == 200
    assert len(body) == 1
    assert body[0]["name"] == "scout"
    assert body[0]["goal"] == "patrol"


def test_list_agents_sorted_by_name(open_server):
    host, _ = open_server
    for name in ("zebra", "alpha", "mike"):
        _post(host, f"/api/agents/{name}/meta", {"goal": name, "tick": 0}, token=TOKEN)
    _, body = _get(host, "/api/agents")
    names = [r["name"] for r in body]
    assert names == sorted(names)


def test_list_agents_requires_auth(server):
    host, _ = server
    status, body = _get(host, "/api/agents")     # no token
    assert status == 401
    assert "error" in body


def test_list_agents_accepts_valid_token(server):
    host, _ = server
    status, _ = _get(host, "/api/agents", token=TOKEN)
    assert status == 200


# ---------------------------------------------------------------------------
# GET /api/agents/<name>/meta
# ---------------------------------------------------------------------------

def test_get_meta_not_found(open_server):
    host, _ = open_server
    status, body = _get(host, "/api/agents/ghost/meta")
    assert status == 404
    assert "error" in body


def test_get_meta_found(open_server):
    host, _ = open_server
    _post(host, "/api/agents/echo/meta", {"goal": "listen", "tick": 3}, token=TOKEN)
    status, body = _get(host, "/api/agents/echo/meta")
    assert status == 200
    assert body["name"] == "echo"
    assert body["goal"] == "listen"
    assert body["tick"] == 3


def test_get_meta_requires_auth(server):
    host, _ = server
    # Create first so 404 vs 401 is not ambiguous
    _post(host, "/api/agents/echo/meta", {"goal": "x", "tick": 0}, token=TOKEN)
    status, body = _get(host, "/api/agents/echo/meta")  # no token
    assert status == 401


# ---------------------------------------------------------------------------
# POST /api/agents/<name>/meta
# ---------------------------------------------------------------------------

def test_post_meta_creates_agent(open_server):
    host, _ = open_server
    status, body = _post(host, "/api/agents/ranger/meta",
                          {"goal": "explore", "tick": 0}, token=TOKEN)
    assert status == 200
    assert body["name"] == "ranger"
    assert body["goal"] == "explore"
    assert body["tick"] == 0


def test_post_meta_upsert_updates_goal(open_server):
    host, _ = open_server
    _post(host, "/api/agents/ranger/meta", {"goal": "old", "tick": 0}, token=TOKEN)
    status, body = _post(host, "/api/agents/ranger/meta",
                          {"goal": "new", "tick": 5}, token=TOKEN)
    assert status == 200
    assert body["goal"] == "new"
    assert body["tick"] == 5


def test_post_meta_idempotent_same_data(open_server):
    host, _ = open_server
    payload = {"goal": "stable", "tick": 1}
    s1, b1 = _post(host, "/api/agents/idem/meta", payload, token=TOKEN)
    s2, b2 = _post(host, "/api/agents/idem/meta", payload, token=TOKEN)
    assert s1 == 200 and s2 == 200
    assert b1["goal"] == b2["goal"] == "stable"


def test_post_meta_bad_body_returns_400(open_server):
    host, _ = open_server
    # Send a JSON array — not an object
    c = http.client.HTTPConnection(host, timeout=5)
    raw = b"[1, 2, 3]"
    c.request("POST", "/api/agents/bad/meta", body=raw, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "Authorization": f"Bearer {TOKEN}",
    })
    r = c.getresponse(); out = r.read(); c.close()
    assert r.status == 400


def test_post_meta_requires_auth(open_server):
    host, _ = open_server
    status, body = _post(host, "/api/agents/x/meta", {"goal": "x", "tick": 0})
    assert status == 401


# ---------------------------------------------------------------------------
# GET /api/agents/<name>/entries
# ---------------------------------------------------------------------------

def test_get_entries_empty(open_server):
    host, _ = open_server
    _post(host, "/api/agents/empty/meta", {"goal": "", "tick": 0}, token=TOKEN)
    status, body = _get(host, "/api/agents/empty/entries")
    assert status == 200
    assert body["entries"] == []
    assert body["count"] == 0


def test_get_entries_nonexistent_agent_returns_empty(open_server):
    host, _ = open_server
    status, body = _get(host, "/api/agents/nobody/entries")
    assert status == 200
    assert body["entries"] == []


def test_get_entries_ordered_by_tick(open_server):
    host, _ = open_server
    name = "ordered"
    for tick in (3, 1, 2):
        _post(host, f"/api/agents/{name}/entries",
              {"tick": tick, "action": "Observe", "event": f"e{tick}",
               "note": "", "hash": f"h{tick}", "prev_hash": ""},
              token=TOKEN)
    _, body = _get(host, f"/api/agents/{name}/entries")
    ticks = [e["tick"] for e in body["entries"]]
    assert ticks == [1, 2, 3]


def test_get_entries_requires_auth(server):
    host, _ = server
    _post(host, "/api/agents/z/meta", {"goal": "", "tick": 0}, token=TOKEN)
    status, _ = _get(host, "/api/agents/z/entries")  # no token
    assert status == 401


# ---------------------------------------------------------------------------
# POST /api/agents/<name>/entries
# ---------------------------------------------------------------------------

_ENTRY = {
    "tick": 1,
    "action": "Observe",
    "event": "filesystem scan",
    "note": "nominal",
    "hash": "abc123",
    "prev_hash": "000000",
}


def test_post_entry_creates_row(open_server):
    host, _ = open_server
    status, body = _post(host, "/api/agents/scout/entries", _ENTRY, token=TOKEN)
    assert status == 201
    assert body["agent_name"] == "scout"
    assert body["tick"] == 1
    assert "id" in body


def test_post_entry_auto_creates_agent_if_missing(open_server):
    """Posting an entry to an unknown agent implicitly upserts the agent row."""
    host, _ = open_server
    status, _ = _post(host, "/api/agents/new_agent/entries", _ENTRY, token=TOKEN)
    assert status == 201
    _, meta = _get(host, "/api/agents/new_agent/meta")
    assert meta["name"] == "new_agent"


def test_post_entry_duplicate_tick_returns_409(open_server):
    host, _ = open_server
    _post(host, "/api/agents/dup/entries", _ENTRY, token=TOKEN)
    status, body = _post(host, "/api/agents/dup/entries", _ENTRY, token=TOKEN)
    assert status == 409
    assert "error" in body


def test_post_entry_advances_agent_tick(open_server):
    host, _ = open_server
    _post(host, "/api/agents/adv/meta", {"goal": "g", "tick": 0}, token=TOKEN)
    for tick in (1, 2, 3):
        entry = {**_ENTRY, "tick": tick, "hash": f"h{tick}"}
        _post(host, "/api/agents/adv/entries", entry, token=TOKEN)
    _, meta = _get(host, "/api/agents/adv/meta")
    assert meta["tick"] == 3


def test_post_entry_missing_tick_returns_400(open_server):
    host, _ = open_server
    status, body = _post(host, "/api/agents/bad/entries",
                          {"action": "Observe"}, token=TOKEN)
    assert status == 400
    assert "tick" in body["error"]


def test_post_entry_multiple_entries_retrievable(open_server):
    host, _ = open_server
    for tick in range(1, 6):
        entry = {**_ENTRY, "tick": tick, "hash": f"h{tick}",
                 "event": f"event_{tick}"}
        _post(host, "/api/agents/multi/entries", entry, token=TOKEN)
    _, body = _get(host, "/api/agents/multi/entries")
    assert body["count"] == 5
    assert [e["tick"] for e in body["entries"]] == list(range(1, 6))


def test_post_entry_requires_auth(open_server):
    host, _ = open_server
    status, body = _post(host, "/api/agents/x/entries", _ENTRY)  # no token
    assert status == 401


def test_post_entry_bad_body_returns_400(open_server):
    host, _ = open_server
    c = http.client.HTTPConnection(host, timeout=5)
    raw = b"[1]"
    c.request("POST", "/api/agents/bad/entries", body=raw, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "Authorization": f"Bearer {TOKEN}",
    })
    r = c.getresponse(); r.read(); c.close()
    assert r.status == 400


# ---------------------------------------------------------------------------
# Cross-route lifecycle
# ---------------------------------------------------------------------------

def test_full_lifecycle(open_server):
    """Create agent, append entries, verify list + meta reflect final state."""
    host, _ = open_server
    name = "lifecycle"

    # Create agent
    s, m = _post(host, f"/api/agents/{name}/meta",
                  {"goal": "map the environment", "tick": 0}, token=TOKEN)
    assert s == 200

    # Append 3 entries
    for tick in (1, 2, 3):
        entry = {**_ENTRY, "tick": tick, "hash": f"h{tick}"}
        s, _ = _post(host, f"/api/agents/{name}/entries", entry, token=TOKEN)
        assert s == 201

    # Meta shows tick advanced
    _, meta = _get(host, f"/api/agents/{name}/meta")
    assert meta["tick"] == 3
    assert meta["goal"] == "map the environment"

    # Entries retrievable in order
    _, entries_resp = _get(host, f"/api/agents/{name}/entries")
    assert entries_resp["count"] == 3
    ticks = [e["tick"] for e in entries_resp["entries"]]
    assert ticks == [1, 2, 3]

    # Agent appears in list
    _, agents = _get(host, "/api/agents")
    names = [a["name"] for a in agents]
    assert name in names
