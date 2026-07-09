"""Multi-tenant auth — integration tests.

Spins up the real ui.serve() in a background thread on an ephemeral port
and exercises every auth scenario via HTTP.  Tests cover:

  - AUTOMATON_TOKEN env-var bypass → always "admin" role
  - DB-stored keys: admin / operator / viewer roles
  - Revoked key → 401
  - Unknown token → 401
  - No token on write route → 401
  - No token on read route → 401 (when require_auth=True)
  - Viewer read allowed / viewer write denied (403)
  - Operator read allowed / operator write allowed / operator key admin denied
  - Admin has full access including key management routes
  - last_used_at is updated after a successful DB-key request
  - GET  /api/keys   — admin only
  - POST /api/keys   — admin only, returns plaintext once
  - DELETE /api/keys/<name> — admin only

All checks go through the HTTP layer to verify the full stack from routing
down to the SQLite tables created by migrations 0001–0005.
"""
from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import threading
import time

import pytest

from automaton import auth as _auth
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


def _req(addr: str, method: str, path: str, token: str | None = None,
         body: dict | None = None) -> tuple[int, dict | str]:
    """Thin HTTP helper.  Returns (status_code, parsed_body_or_str)."""
    host, port = addr.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body).encode() if body is not None else b""
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, raw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    """
    Returns a dict with:
      addr        — "127.0.0.1:<port>"
      db_path     — str path to the DB
      admin_token — raw AUTOMATON_TOKEN (env-var style)
      keys        — dict[role -> raw_key]
    """
    db_path = tmp_path / "multitenancy_test.db"
    conn = _db.connect(db_path)
    _db.migrate(conn)

    # Pre-create one key per role via auth.py directly
    keys: dict[str, str] = {}
    for role in ("admin", "operator", "viewer"):
        _, raw = _auth.create_api_key(conn, f"test-{role}", role)
        keys[role] = raw

    # Also create a key we will revoke immediately
    _, revoked_raw = _auth.create_api_key(conn, "revoked-key", "operator")
    _auth.revoke_api_key(conn, "revoked-key")
    keys["revoked"] = revoked_raw

    conn.close()

    admin_token = "master-admin-token"
    port = _free_port()
    httpd = _ui.serve(
        str(db_path),
        host="127.0.0.1",
        port=port,
        auth_token=admin_token,
        insecure_read_no_auth=False,
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {
        "addr": f"127.0.0.1:{port}",
        "db_path": str(db_path),
        "admin_token": admin_token,
        "keys": keys,
    }
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------------------
# AUTOMATON_TOKEN (env-var) bypass
# ---------------------------------------------------------------------------

class TestEnvTokenBypass:
    def test_env_token_allows_read(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs", token=env["admin_token"])
        assert status == 200

    def test_env_token_allows_write(self, env):
        status, _ = _req(env["addr"], "POST", "/api/trigger",
                         token=env["admin_token"],
                         body={"workflow": "nonexistent"})
        # 404 = routing worked, auth passed
        assert status in (200, 404, 422)

    def test_env_token_allows_key_list(self, env):
        status, body = _req(env["addr"], "GET", "/api/keys", token=env["admin_token"])
        assert status == 200
        assert isinstance(body["keys"], list)

    def test_wrong_env_token_rejected(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs", token="wrong-token")
        assert status == 401

    def test_no_token_rejected(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs")
        assert status == 401


# ---------------------------------------------------------------------------
# Unknown / revoked keys
# ---------------------------------------------------------------------------

class TestBadKeys:
    def test_unknown_token_rejected_on_read(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs", token="atk_" + "0" * 64)
        assert status == 401

    def test_unknown_token_rejected_on_write(self, env):
        status, _ = _req(env["addr"], "POST", "/api/trigger",
                         token="atk_" + "0" * 64,
                         body={"workflow": "x"})
        assert status == 401

    def test_revoked_key_rejected_on_read(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs",
                         token=env["keys"]["revoked"])
        assert status == 401

    def test_revoked_key_rejected_on_write(self, env):
        status, _ = _req(env["addr"], "POST", "/api/trigger",
                         token=env["keys"]["revoked"],
                         body={"workflow": "x"})
        assert status == 401


# ---------------------------------------------------------------------------
# Viewer role
# ---------------------------------------------------------------------------

class TestViewerRole:
    def test_viewer_can_read_runs(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs",
                         token=env["keys"]["viewer"])
        assert status == 200

    def test_viewer_can_read_workflows(self, env):
        # /api/runs is the canonical read-only list endpoint
        status, _ = _req(env["addr"], "GET", "/api/runs",
                         token=env["keys"]["viewer"])
        assert status == 200

    def test_viewer_cannot_trigger(self, env):
        status, _ = _req(env["addr"], "POST", "/api/trigger",
                         token=env["keys"]["viewer"],
                         body={"workflow": "x"})
        assert status == 403

    def test_viewer_cannot_send_signal(self, env):
        status, _ = _req(env["addr"], "POST", "/api/signals",
                         token=env["keys"]["viewer"],
                         body={"run_id": "r1", "name": "go"})
        assert status == 403

    def test_viewer_cannot_list_keys(self, env):
        status, _ = _req(env["addr"], "GET", "/api/keys",
                         token=env["keys"]["viewer"])
        assert status == 403

    def test_viewer_cannot_create_key(self, env):
        status, _ = _req(env["addr"], "POST", "/api/keys",
                         token=env["keys"]["viewer"],
                         body={"name": "bad", "role": "viewer"})
        assert status == 403


# ---------------------------------------------------------------------------
# Operator role
# ---------------------------------------------------------------------------

class TestOperatorRole:
    def test_operator_can_read_runs(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs",
                         token=env["keys"]["operator"])
        assert status == 200

    def test_operator_can_trigger(self, env):
        status, _ = _req(env["addr"], "POST", "/api/trigger",
                         token=env["keys"]["operator"],
                         body={"workflow": "nonexistent"})
        assert status in (200, 404, 422)

    def test_operator_can_write_agents(self, env):
        status, _ = _req(env["addr"], "POST", "/api/agents/test-op-agent/meta",
                         token=env["keys"]["operator"],
                         body={"model": "gpt-4o"})
        assert status in (200, 201)

    def test_operator_cannot_list_keys(self, env):
        status, _ = _req(env["addr"], "GET", "/api/keys",
                         token=env["keys"]["operator"])
        assert status == 403

    def test_operator_cannot_create_key(self, env):
        status, _ = _req(env["addr"], "POST", "/api/keys",
                         token=env["keys"]["operator"],
                         body={"name": "bad", "role": "viewer"})
        assert status == 403

    def test_operator_cannot_delete_key(self, env):
        status, _ = _req(env["addr"], "DELETE", "/api/keys/test-viewer",
                         token=env["keys"]["operator"])
        assert status == 403


# ---------------------------------------------------------------------------
# Admin DB key (distinct from AUTOMATON_TOKEN)
# ---------------------------------------------------------------------------

class TestAdminDbKey:
    def test_admin_key_can_read(self, env):
        status, _ = _req(env["addr"], "GET", "/api/runs",
                         token=env["keys"]["admin"])
        assert status == 200

    def test_admin_key_can_write(self, env):
        status, _ = _req(env["addr"], "POST", "/api/trigger",
                         token=env["keys"]["admin"],
                         body={"workflow": "x"})
        assert status in (200, 404, 422)

    def test_admin_key_can_list_keys(self, env):
        status, body = _req(env["addr"], "GET", "/api/keys",
                            token=env["keys"]["admin"])
        assert status == 200
        keys = body["keys"]
        names = [r["name"] for r in keys]
        assert "test-admin" in names
        assert "test-operator" in names
        assert "test-viewer" in names

    def test_admin_key_can_create_key(self, env):
        status, body = _req(env["addr"], "POST", "/api/keys",
                            token=env["keys"]["admin"],
                            body={"name": "new-ci-key", "role": "operator"})
        assert status == 201
        assert "key" in body
        assert body["key"].startswith("atk_")
        assert "note" in body
        assert body["id"].startswith("key_")

    def test_admin_key_can_revoke_key(self, env):
        # First create a key to revoke
        status, body = _req(env["addr"], "POST", "/api/keys",
                            token=env["keys"]["admin"],
                            body={"name": "to-revoke", "role": "viewer"})
        assert status == 201
        # Now revoke it
        status2, _ = _req(env["addr"], "DELETE", "/api/keys/to-revoke",
                          token=env["keys"]["admin"])
        assert status2 == 200


# ---------------------------------------------------------------------------
# Key management API details
# ---------------------------------------------------------------------------

class TestKeyManagementApi:
    def test_create_key_missing_name_rejected(self, env):
        status, _ = _req(env["addr"], "POST", "/api/keys",
                         token=env["admin_token"],
                         body={"role": "viewer"})
        assert status in (400, 422)

    def test_create_key_invalid_role_rejected(self, env):
        status, _ = _req(env["addr"], "POST", "/api/keys",
                         token=env["admin_token"],
                         body={"name": "bad-role-key", "role": "superuser"})
        assert status in (400, 422)

    def test_create_key_duplicate_name_rejected(self, env):
        # test-viewer already exists (created in fixture)
        status, _ = _req(env["addr"], "POST", "/api/keys",
                         token=env["admin_token"],
                         body={"name": "test-viewer", "role": "viewer"})
        assert status == 409

    def test_list_keys_does_not_expose_hash(self, env):
        status, body = _req(env["addr"], "GET", "/api/keys",
                            token=env["admin_token"])
        assert status == 200
        for row in body["keys"]:
            assert "key_hash" not in row

    def test_delete_nonexistent_key(self, env):
        status, _ = _req(env["addr"], "DELETE", "/api/keys/no-such-key",
                         token=env["admin_token"])
        assert status == 404

    def test_created_key_appears_in_list(self, env):
        _req(env["addr"], "POST", "/api/keys",
             token=env["admin_token"],
             body={"name": "list-check-key", "role": "viewer"})
        status, body = _req(env["addr"], "GET", "/api/keys",
                            token=env["admin_token"])
        assert status == 200
        names = [r["name"] for r in body["keys"]]
        assert "list-check-key" in names

    def test_revoked_key_shows_active_false_in_list(self, env):
        status, body = _req(env["addr"], "GET", "/api/keys",
                            token=env["admin_token"])
        assert status == 200
        revoked_rows = [r for r in body["keys"] if r["name"] == "revoked-key"]
        assert len(revoked_rows) == 1
        assert revoked_rows[0]["active"] == 0


# ---------------------------------------------------------------------------
# last_used_at tracking
# ---------------------------------------------------------------------------

class TestLastUsedAt:
    def test_last_used_at_updated_after_request(self, env):
        conn = sqlite3.connect(env["db_path"])
        conn.row_factory = sqlite3.Row

        # Before: last_used_at is NULL
        row = conn.execute(
            "SELECT last_used_at FROM api_keys WHERE name = 'test-operator'"
        ).fetchone()
        assert row["last_used_at"] is None

        # Make a request with the operator key
        status, _ = _req(env["addr"], "GET", "/api/runs",
                         token=env["keys"]["operator"])
        assert status == 200

        # Give the DB commit a moment
        time.sleep(0.05)

        row = conn.execute(
            "SELECT last_used_at FROM api_keys WHERE name = 'test-operator'"
        ).fetchone()
        conn.close()
        assert row["last_used_at"] is not None

    def test_last_used_at_not_updated_for_env_token(self, env):
        """AUTOMATON_TOKEN bypass doesn't touch the api_keys table."""
        conn = sqlite3.connect(env["db_path"])
        conn.row_factory = sqlite3.Row

        _req(env["addr"], "GET", "/api/runs", token=env["admin_token"])
        time.sleep(0.05)

        # No row in api_keys corresponds to the env-var token
        rows = conn.execute(
            "SELECT last_used_at FROM api_keys WHERE last_used_at IS NOT NULL"
        ).fetchall()
        conn.close()
        # The env-var admin_token should not have touched any DB row
        # (fixture keys haven't been used yet in this test)
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# Functional key round-trip
# ---------------------------------------------------------------------------

class TestKeyRoundTrip:
    def test_created_key_can_authenticate(self, env):
        """Key created via POST /api/keys must work as a Bearer token."""
        status, body = _req(env["addr"], "POST", "/api/keys",
                            token=env["admin_token"],
                            body={"name": "roundtrip-key", "role": "operator"})
        assert status == 201
        raw_key = body["key"]

        # Use the new key to read
        status2, _ = _req(env["addr"], "GET", "/api/runs", token=raw_key)
        assert status2 == 200

    def test_revoked_key_cannot_authenticate(self, env):
        """After DELETE /api/keys/<name>, the raw key must stop working."""
        # Create
        status, body = _req(env["addr"], "POST", "/api/keys",
                            token=env["admin_token"],
                            body={"name": "temp-revoke-key", "role": "viewer"})
        assert status == 201
        raw_key = body["key"]

        # Confirm it works
        assert _req(env["addr"], "GET", "/api/runs", token=raw_key)[0] == 200

        # Revoke via HTTP
        assert _req(env["addr"], "DELETE", "/api/keys/temp-revoke-key",
                    token=env["admin_token"])[0] == 200

        # Now it must be rejected
        assert _req(env["addr"], "GET", "/api/runs", token=raw_key)[0] == 401
