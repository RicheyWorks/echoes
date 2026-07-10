"""Shared test configuration.

socket.getfqdn() does a reverse-DNS lookup of the local hostname. On
GitHub's macOS runners (and other sandboxed CI environments) that lookup
black-holes for minutes, which used to hang the whole suite: yoyo calls
getfqdn() when logging each applied migration, and http.server calls it
in server_bind. Patch it process-wide for tests to the plain hostname —
no test asserts on the fully-qualified form.
"""
from __future__ import annotations

import socket

_real_getfqdn = socket.getfqdn


def _fast_getfqdn(name: str = "") -> str:
    return name or socket.gethostname()


socket.getfqdn = _fast_getfqdn
