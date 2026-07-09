"""Tailscale / Headscale mesh status helper.

The actual mesh runs as a separate daemon (the `tailscaled` process).
This module shells out to the `tailscale` CLI to report whether it's up
and what IP / hostname the engine should advertise. Used by both the CLI
and operators trying to debug why the UI can't be reached from a phone.

We don't try to do this via Tailscale's HTTP API because:
- That requires an OAuth token, which adds setup friction.
- The CLI is on PATH wherever the daemon is installed.
- The output of `tailscale status --json` is stable across recent versions.

Returns plain dicts; callers format for humans or machines as needed.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

# ---------------------------------------------------------------------------
# TTL cache — avoids hitting the tailscale CLI on every HTTP request.
# The status changes only when the daemon is restarted or the network changes,
# so a 60-second window is more than fresh enough for the UI card.
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache_value: Optional[dict] = None
_cache_ts: float = 0.0
_CACHE_TTL = 60.0  # seconds


def cached_status(ttl: float = _CACHE_TTL) -> dict:
    """Return mesh status, refreshing at most once per ``ttl`` seconds."""
    global _cache_value, _cache_ts
    with _cache_lock:
        if _cache_value is None or (time.monotonic() - _cache_ts) > ttl:
            _cache_value = status()
            _cache_ts = time.monotonic()
        return dict(_cache_value)


def _which_tailscale() -> Optional[str]:
    """Return the path to the `tailscale` CLI, or None if not on PATH."""
    return shutil.which("tailscale")


def _run_tailscale_status_json() -> Optional[dict]:
    """Run `tailscale status --json` and parse it. Returns None on failure."""
    cli = _which_tailscale()
    if cli is None:
        return None
    try:
        r = subprocess.run(
            [cli, "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def status() -> dict:
    """Report the engine's mesh-side reachability.

    Returns a dict like::

        {
            "installed": True,
            "running": True,
            "logged_in": True,
            "ips": ["100.64.1.5"],
            "hostname": "automaton-host",
            "magic_dns": "automaton-host.your-tailnet.ts.net",
            "tailnet": "your-tailnet.ts.net",
            "peers": 7,
            "notes": [],
        }

    Each ``installed`` / ``running`` / ``logged_in`` is False with an
    explanation in ``notes`` when something's off, so the CLI can present
    a focused remediation message.
    """
    info: dict = {
        "installed": False,
        "running": False,
        "logged_in": False,
        "ips": [],
        "hostname": None,
        "magic_dns": None,
        "tailnet": None,
        "peers": 0,
        "notes": [],
    }

    if _which_tailscale() is None:
        info["notes"].append(
            "tailscale CLI not on PATH. Install Tailscale from "
            "https://tailscale.com/download and re-run."
        )
        return info
    info["installed"] = True

    data = _run_tailscale_status_json()
    if data is None:
        info["notes"].append(
            "tailscale daemon doesn't appear to be running. "
            "Try: sudo tailscale up"
        )
        return info
    info["running"] = True

    # 'Self' carries our local node info; 'BackendState' tells us if we
    # finished logging in.
    self_node = data.get("Self") or {}
    backend = data.get("BackendState", "")
    if backend != "Running":
        info["notes"].append(
            f"tailscale BackendState={backend!r} (expected 'Running'). "
            "Run: tailscale up"
        )
        # Fall through - still useful to report what we have.

    info["logged_in"] = backend == "Running"
    info["ips"] = list(self_node.get("TailscaleIPs") or [])
    info["hostname"] = self_node.get("HostName")

    dns_name = self_node.get("DNSName", "")
    # DNSName looks like "host.tailnet.ts.net." (trailing dot). Strip and
    # split for the magic DNS / tailnet display.
    if dns_name:
        dns_name = dns_name.rstrip(".")
        info["magic_dns"] = dns_name
        # tailnet = everything after the first hostname segment
        parts = dns_name.split(".", 1)
        if len(parts) == 2:
            info["tailnet"] = parts[1]

    info["peers"] = len(data.get("Peer") or {})

    if info["logged_in"] and not info["ips"]:
        info["notes"].append(
            "logged in but no Tailscale IPs assigned yet - this is rare; "
            "wait a few seconds and retry."
        )

    return info


def check_port_locally(port: int, host: str = "127.0.0.1",
                       timeout: float = 2.0) -> bool:
    """TCP-connect to (host, port) to verify the engine is listening.

    Useful as a sanity check alongside the mesh status: if Tailscale is
    fine but this returns False, the problem is `automaton serve` not
    being started, not the mesh.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False
