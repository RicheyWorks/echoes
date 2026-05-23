"""TLS for the built-in UI server.

Covers:
  - tls.init_self_signed produces a parseable cert/key pair
  - SANs include the requested hostname plus loopback aliases
  - extra_sans accept IPs and DNS names
  - ui.serve(tls_cert, tls_key) actually accepts HTTPS connections
  - Strict-Transport-Security header is present under TLS
  - ...and absent over plain HTTP
  - ui.serve raises ValueError on cert without key (and vice versa)
  - ui.serve raises ValueError on a bad/missing cert
  - refusal to overwrite existing cert files
"""
from __future__ import annotations

import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

# cryptography is optional; skip if it's not installed
cryptography = pytest.importorskip("cryptography")

from automaton import db as _db
from automaton import tls as _tls
from automaton import ui as _ui


@pytest.fixture
def tls_pair(tmp_path):
    info = _tls.init_self_signed(
        tmp_path / "tls",
        hostname="automaton.test",
        extra_sans=["100.64.1.2"],
    )
    return info


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    conn = _db.connect(p)
    _db.migrate(conn)
    conn.close()
    return str(p)


def _free_port() -> int:
    """Return a port the OS just freed (race-y but fine for tests)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_cert_and_key_files_written(tls_pair, tmp_path):
    assert Path(tls_pair["cert"]).exists()
    assert Path(tls_pair["key"]).exists()
    cert_bytes = Path(tls_pair["cert"]).read_bytes()
    assert b"BEGIN CERTIFICATE" in cert_bytes
    key_bytes = Path(tls_pair["key"]).read_bytes()
    assert b"BEGIN PRIVATE KEY" in key_bytes


def test_cert_loads_into_ssl_context(tls_pair):
    """The most important contract: ssl.SSLContext can use the generated pair."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=tls_pair["cert"], keyfile=tls_pair["key"])


def test_sans_include_hostname_and_loopback(tls_pair):
    assert "automaton.test" in tls_pair["sans"]
    assert "localhost" in tls_pair["sans"]
    assert "127.0.0.1" in tls_pair["sans"]
    assert "100.64.1.2" in tls_pair["sans"]  # IP from extra_sans


def test_refuses_to_overwrite(tmp_path):
    out = tmp_path / "tls"
    _tls.init_self_signed(out, hostname="a")
    with pytest.raises(FileExistsError):
        _tls.init_self_signed(out, hostname="a")


def test_validity_cap_at_825_days(tmp_path):
    with pytest.raises(ValueError, match="825"):
        _tls.init_self_signed(tmp_path / "tls", validity_days=826)


def test_serve_over_tls_actually_speaks_https(db_path, tls_pair, tmp_path):
    """End-to-end: spin up the UI on TLS and hit /healthz with verification."""
    port = _free_port()
    httpd = _ui.serve(
        db_path, host="127.0.0.1", port=port, auth_token="t",
        tls_cert=tls_pair["cert"], tls_key=tls_pair["key"],
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # Build a client context that trusts our generated cert.
        client_ctx = ssl.create_default_context()
        client_ctx.load_verify_locations(cafile=tls_pair["cert"])
        # 127.0.0.1 is in our SAN so hostname check works.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="localhost") as s:
                s.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        text = data.decode("utf-8", errors="replace")
        assert text.startswith("HTTP/1.0 200 OK"), text[:200]
        assert '"ok": true' in text
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_hsts_header_present_under_tls(db_path, tls_pair):
    port = _free_port()
    httpd = _ui.serve(
        db_path, host="127.0.0.1", port=port, auth_token="t",
        tls_cert=tls_pair["cert"], tls_key=tls_pair["key"],
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=tls_pair["cert"])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost") as s:
                s.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        text = data.decode("utf-8", errors="replace")
        assert "Strict-Transport-Security: max-age=15552000" in text
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_no_hsts_header_without_tls(db_path):
    """HSTS must be absent on plain HTTP - it would lie about the next visit."""
    port = _free_port()
    httpd = _ui.serve(db_path, host="127.0.0.1", port=port, auth_token="t")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/healthz")
        r = c.getresponse()
        r.read()
        assert r.getheader("Strict-Transport-Security") is None
        c.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_one_tls_arg_without_other_raises(db_path, tls_pair):
    port = _free_port()
    with pytest.raises(ValueError, match="both --tls-cert and --tls-key"):
        _ui.serve(db_path, host="127.0.0.1", port=port,
                  auth_token="t", tls_cert=tls_pair["cert"])
    with pytest.raises(ValueError, match="both --tls-cert and --tls-key"):
        _ui.serve(db_path, host="127.0.0.1", port=port,
                  auth_token="t", tls_key=tls_pair["key"])


def test_missing_cert_file_raises_cleanly(db_path, tmp_path):
    port = _free_port()
    with pytest.raises(ValueError, match="could not load TLS"):
        _ui.serve(db_path, host="127.0.0.1", port=port, auth_token="t",
                  tls_cert=str(tmp_path / "nope.pem"),
                  tls_key=str(tmp_path / "nope-key.pem"))
