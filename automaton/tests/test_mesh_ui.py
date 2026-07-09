"""Mesh reachability card — UI rendering tests.

We prove:
  1. render_mesh_card returns empty string when Tailscale is not installed.
  2. render_mesh_card returns empty string when installed but not logged in.
  3. render_mesh_card returns a card with the magic DNS URL when fully up.
  4. render_mesh_card falls back to IP:port when magic_dns is absent.
  5. The card is present in the GET / response when mesh is healthy
     (monkeypatching cached_status so no real tailscale process is needed).
  6. The card is absent from GET / when mesh is not available.
  7. cached_status() returns a dict and caches the call (second call is instant).
"""
from __future__ import annotations

import time

import pytest

from automaton.ui import render_mesh_card


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _mesh_up(**overrides) -> dict:
    base = {
        "installed": True,
        "running": True,
        "logged_in": True,
        "ips": ["100.64.1.5"],
        "hostname": "my-host",
        "magic_dns": "my-host.example.ts.net",
        "tailnet": "example.ts.net",
        "peers": 3,
        "notes": [],
    }
    base.update(overrides)
    return base


def _mesh_off(**overrides) -> dict:
    base = {
        "installed": False,
        "running": False,
        "logged_in": False,
        "ips": [],
        "hostname": None,
        "magic_dns": None,
        "tailnet": None,
        "peers": 0,
        "notes": ["tailscale CLI not on PATH."],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ #
# 1–4: render_mesh_card unit tests                                     #
# ------------------------------------------------------------------ #

def test_card_hidden_when_not_installed():
    assert render_mesh_card(_mesh_off()) == ""


def test_card_hidden_when_not_logged_in():
    info = _mesh_off(installed=True, running=True)
    assert render_mesh_card(info) == ""


def test_card_hidden_when_running_but_not_logged_in():
    info = _mesh_up(logged_in=False)
    assert render_mesh_card(info) == ""


def test_card_shown_when_fully_up():
    html = render_mesh_card(_mesh_up())
    assert "Connected to Tailscale" in html
    assert "https://my-host.example.ts.net" in html
    assert "example.ts.net" in html
    assert "3 peers" in html


def test_card_shows_ip_when_no_magic_dns():
    info = _mesh_up(magic_dns=None, tailnet=None)
    html = render_mesh_card(info)
    assert "Connected to Tailscale" in html
    # Falls back to IP-based URL
    assert "100.64.1.5" in html


def test_card_copy_button_present():
    html = render_mesh_card(_mesh_up())
    assert "copy" in html
    assert "navigator.clipboard" in html


def test_card_empty_dict_is_safe():
    """render_mesh_card must not raise on an empty / partial dict."""
    assert render_mesh_card({}) == ""


# ------------------------------------------------------------------ #
# 5–6: card in GET / response                                         #
# ------------------------------------------------------------------ #

from automaton import db as _db, mesh as _mesh
from automaton.ui import make_handler


def _run_get(db_path, path, monkeypatch, mesh_data, token="tok"):
    monkeypatch.setattr(_mesh, "cached_status", lambda: mesh_data)
    Handler = make_handler(db_path, auth_token=token,
                           require_auth=True, require_read_auth=False)

    class H(Handler):
        def __init__(self):
            self.path = path
            self.command = "GET"
            self.headers = {}
            self._code = None
            self._body = b""

        def send_response(self, code, msg=None): self._code = code
        def send_header(self, *a): pass
        def end_headers(self): pass

        @property
        def wfile(outer=None):
            class W:
                def write(s, data): h_inst._body += data
                def flush(s): pass
            return W()

        def log_message(self, *a): pass

    h_inst = H()
    H.wfile = property(lambda self: type('W', (), {
        'write': lambda s, d: setattr(h_inst, '_body', h_inst._body + d),
        'flush': lambda s: None,
    })())
    h_inst.do_GET()
    return h_inst._code, h_inst._body.decode("utf-8", errors="replace")


def test_run_list_contains_mesh_card_when_up(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ui.db")
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()

    code, body = _run_get(db_path, "/", monkeypatch, _mesh_up())
    assert code == 200
    assert "Connected to Tailscale" in body
    assert "https://my-host.example.ts.net" in body


def test_run_list_no_mesh_card_when_down(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ui.db")
    conn = _db.connect(db_path)
    _db.migrate(conn)
    conn.close()

    code, body = _run_get(db_path, "/", monkeypatch, _mesh_off())
    assert code == 200
    assert "Connected to Tailscale" not in body


# ------------------------------------------------------------------ #
# 7: cached_status caching behaviour                                  #
# ------------------------------------------------------------------ #

def test_cached_status_returns_dict(monkeypatch):
    monkeypatch.setattr(_mesh, "_which_tailscale", lambda: None)
    result = _mesh.cached_status()
    assert isinstance(result, dict)
    assert "installed" in result
    assert result["installed"] is False


def test_cached_status_uses_cache(monkeypatch):
    """Second call within TTL must not invoke status() again."""
    call_count = {"n": 0}
    original = _mesh.status

    def counting_status():
        call_count["n"] += 1
        return original()

    # Reset the cache first
    import automaton.mesh as _m
    _m._cache_value = None
    _m._cache_ts = 0.0

    monkeypatch.setattr(_mesh, "_which_tailscale", lambda: None)
    monkeypatch.setattr(_mesh, "status", counting_status)

    _mesh.cached_status(ttl=60)
    _mesh.cached_status(ttl=60)
    assert call_count["n"] == 1, "status() should only be called once within the TTL"
