"""Mesh status helper tests.

We can't talk to a real Tailscale daemon in CI, so we monkeypatch
``_which_tailscale`` and ``_run_tailscale_status_json`` to simulate
each meaningful state:

  - Tailscale not installed (CLI not on PATH)
  - CLI installed but daemon not running
  - Daemon running, BackendState != Running (logged out)
  - Fully up and healthy

Plus a real socket check that proves ``check_port_locally`` reads as
"open" against a listener and "closed" against a free port.
"""
from __future__ import annotations

import socket
import threading

import pytest

from automaton import mesh


# --------------------- Tailscale status fixtures ----------------------

def _patch_state(monkeypatch, installed: bool, daemon_data):
    """Force a particular shape into the status() helper.

    ``installed`` controls whether the CLI is "on PATH"; ``daemon_data``
    is what _run_tailscale_status_json() returns (None = daemon down, or
    a dict mimicking ``tailscale status --json``).
    """
    monkeypatch.setattr(
        mesh, "_which_tailscale",
        lambda: "/usr/local/bin/tailscale" if installed else None,
    )
    monkeypatch.setattr(
        mesh, "_run_tailscale_status_json",
        lambda: daemon_data,
    )


def test_not_installed(monkeypatch):
    _patch_state(monkeypatch, installed=False, daemon_data=None)
    s = mesh.status()
    assert s["installed"] is False
    assert s["running"] is False
    assert s["logged_in"] is False
    assert s["ips"] == []
    assert any("not on PATH" in n for n in s["notes"])


def test_installed_but_daemon_down(monkeypatch):
    _patch_state(monkeypatch, installed=True, daemon_data=None)
    s = mesh.status()
    assert s["installed"] is True
    assert s["running"] is False
    assert s["logged_in"] is False
    assert any("daemon" in n.lower() for n in s["notes"])


def test_logged_out(monkeypatch):
    """Daemon answers status but BackendState says we haven't logged in."""
    _patch_state(monkeypatch, installed=True, daemon_data={
        "BackendState": "NeedsLogin",
        "Self": {
            "HostName": "automaton-host",
            "DNSName": "",
            "TailscaleIPs": [],
        },
        "Peer": {},
    })
    s = mesh.status()
    assert s["installed"] is True
    assert s["running"] is True
    assert s["logged_in"] is False
    assert s["hostname"] == "automaton-host"
    assert s["ips"] == []
    assert any("BackendState" in n for n in s["notes"])


def test_fully_up(monkeypatch):
    _patch_state(monkeypatch, installed=True, daemon_data={
        "BackendState": "Running",
        "Self": {
            "HostName": "automaton-host",
            "DNSName": "automaton-host.your-tailnet.ts.net.",
            "TailscaleIPs": ["100.64.1.5", "fd7a:115c:a1e0::1"],
        },
        "Peer": {
            "nodekey:a": {}, "nodekey:b": {}, "nodekey:c": {},
        },
    })
    s = mesh.status()
    assert s["installed"] is True
    assert s["running"] is True
    assert s["logged_in"] is True
    assert s["hostname"] == "automaton-host"
    assert s["ips"] == ["100.64.1.5", "fd7a:115c:a1e0::1"]
    assert s["magic_dns"] == "automaton-host.your-tailnet.ts.net"
    assert s["tailnet"] == "your-tailnet.ts.net"
    assert s["peers"] == 3
    assert s["notes"] == []


def test_running_but_no_ips_yet(monkeypatch):
    """Edge case: BackendState=Running but TailscaleIPs is empty.
    Sometimes happens for a few seconds right after `tailscale up`."""
    _patch_state(monkeypatch, installed=True, daemon_data={
        "BackendState": "Running",
        "Self": {"HostName": "auto", "DNSName": "", "TailscaleIPs": []},
        "Peer": {},
    })
    s = mesh.status()
    assert s["logged_in"] is True
    assert s["ips"] == []
    assert any("no Tailscale IPs" in n for n in s["notes"])


# ------------------------ port-reachability ---------------------------

def test_check_port_locally_returns_false_for_closed_port():
    # Bind, grab the port, then close: that port is now free.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # We don't get a guarantee no one else claimed it in between, but
    # for a 1-second window in CI the race is fine.
    assert mesh.check_port_locally(port, timeout=0.5) is False


def test_check_port_locally_returns_true_for_open_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted = []

    def accept_once():
        try:
            c, _ = server.accept()
            accepted.append(c)
            c.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    try:
        assert mesh.check_port_locally(port, timeout=2.0) is True
    finally:
        server.close()
        t.join(timeout=2.0)


# ------------------- ConnectionRefusedError isn't propagated ----------

def test_check_port_locally_handles_unroutable_host(monkeypatch):
    """Sanity: connecting to a definitely-unreachable host returns False."""
    # 192.0.2.0/24 is reserved TEST-NET-1 - never routable.
    assert mesh.check_port_locally(80, host="192.0.2.1", timeout=0.5) is False
