"""Signed webhook receiver.

Each endpoint has its own HMAC secret. A POST /webhook/<name> request must
include a signature header whose value is `<algo>=<hex>` where the HMAC is
computed over the raw request body using that endpoint's secret. On success,
the body is passed to the registered workflow as the run's trigger payload.

This is different from the /api/* bearer-token auth: each external integration
(GitHub, Stripe, Twilio, your own agents) gets its own per-endpoint secret,
so they can be revoked independently and never share a credential.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from typing import Optional

from . import db as _db

log = logging.getLogger("automaton.webhooks")


class WebhookError(Exception):
    """Generic webhook-handling error. status_code drives the HTTP response."""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def register_webhook(
    conn: sqlite3.Connection,
    name: str,
    workflow_name: str,
    signature_header: str = "X-Automaton-Signature",
    signature_algo: str = "sha256",
    secret_hex: Optional[str] = None,
) -> tuple[int, str]:
    """Register or update an endpoint. Returns (id, secret_hex).

    If secret_hex is None, a fresh 32-byte secret is generated. Always returned
    so the CLI can print it once — it's never recoverable after that.
    """
    if signature_algo not in ("sha256", "sha1", "sha512"):
        raise ValueError(f"unsupported signature_algo: {signature_algo!r}")
    if secret_hex is None:
        secret_hex = secrets.token_hex(32)
    with _db.transaction(conn):
        existing = conn.execute(
            "SELECT id FROM webhook_endpoint WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE webhook_endpoint SET workflow_name = ?, secret_hex = ?, "
                "  signature_header = ?, signature_algo = ?, enabled = 1 "
                "WHERE id = ?",
                (workflow_name, secret_hex, signature_header, signature_algo,
                 existing["id"]),
            )
            return existing["id"], secret_hex
        cur = conn.execute(
            "INSERT INTO webhook_endpoint "
            "  (name, workflow_name, secret_hex, signature_header, signature_algo) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, workflow_name, secret_hex, signature_header, signature_algo),
        )
        return cur.lastrowid, secret_hex


def list_webhooks(conn: sqlite3.Connection) -> list[dict]:
    """List endpoints. Secrets are NOT returned - only an indicator."""
    rows = conn.execute(
        "SELECT id, name, workflow_name, signature_header, signature_algo, "
        "  enabled, created_at FROM webhook_endpoint ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def disable_webhook(conn: sqlite3.Connection, name: str) -> bool:
    with _db.transaction(conn):
        cur = conn.execute(
            "UPDATE webhook_endpoint SET enabled = 0 WHERE name = ?", (name,)
        )
        return cur.rowcount > 0


def get_endpoint(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT * FROM webhook_endpoint WHERE name = ? AND enabled = 1",
        (name,),
    ).fetchone()


def _parse_signature(header_value: str) -> tuple[str, str]:
    """Parse '<algo>=<hex>' into (algo, hex). Accepts bare hex as sha256
    fallback for callers that omit the algo prefix."""
    if not header_value:
        raise WebhookError("missing signature header", status_code=401)
    if "=" in header_value:
        algo, _, sig_hex = header_value.partition("=")
        return algo.strip().lower(), sig_hex.strip()
    return "sha256", header_value.strip()


def verify_signature(endpoint, body_bytes: bytes, header_value: str) -> None:
    """Raise WebhookError if the body's HMAC doesn't match the header.
    Constant-time comparison."""
    algo, supplied_hex = _parse_signature(header_value)
    if algo != endpoint["signature_algo"]:
        raise WebhookError(
            f"signature algo mismatch: got {algo!r}, expected {endpoint['signature_algo']!r}",
            status_code=401,
        )
    key = bytes.fromhex(endpoint["secret_hex"])
    digestmod = {"sha256": hashlib.sha256, "sha1": hashlib.sha1,
                 "sha512": hashlib.sha512}[algo]
    expected = hmac.new(key, body_bytes, digestmod).hexdigest()
    if not hmac.compare_digest(expected, supplied_hex.lower()):
        raise WebhookError("signature mismatch", status_code=401)
