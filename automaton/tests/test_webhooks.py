"""Webhook receiver tests.

Cover:
  1. register_webhook: returns a secret, persisted endpoint is enabled.
  2. verify_signature: valid HMAC accepted, tampered body rejected, wrong
     secret rejected, missing header rejected, wrong algo rejected.
  3. Re-registering the same name updates the endpoint (idempotent).
  4. Disable flips enabled=0 and get_endpoint() returns None.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from automaton import db as _db
from automaton import webhooks


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _sign(secret_hex: str, body: bytes, algo: str = "sha256") -> str:
    key = bytes.fromhex(secret_hex)
    digestmod = getattr(hashlib, algo)
    return f"{algo}=" + hmac.new(key, body, digestmod).hexdigest()


def test_register_and_lookup(store):
    wid, secret = webhooks.register_webhook(store, "gh", "my_wf")
    assert wid > 0
    assert len(secret) == 64  # 32 bytes -> 64 hex chars

    ep = webhooks.get_endpoint(store, "gh")
    assert ep["name"] == "gh"
    assert ep["workflow_name"] == "my_wf"
    assert ep["signature_algo"] == "sha256"
    assert ep["enabled"] == 1
    # Secret round-trips
    assert ep["secret_hex"] == secret


def test_register_is_idempotent_and_can_rotate(store):
    wid1, secret1 = webhooks.register_webhook(store, "gh", "wf_a")
    wid2, secret2 = webhooks.register_webhook(store, "gh", "wf_b")
    assert wid1 == wid2     # same row
    assert secret1 != secret2  # secret rotated
    ep = webhooks.get_endpoint(store, "gh")
    assert ep["workflow_name"] == "wf_b"


def test_verify_valid_signature_passes(store):
    _, secret = webhooks.register_webhook(store, "gh", "wf")
    ep = webhooks.get_endpoint(store, "gh")
    body = b'{"event":"push"}'
    sig = _sign(secret, body)
    # Should not raise
    webhooks.verify_signature(ep, body, sig)


def test_verify_tampered_body_rejected(store):
    _, secret = webhooks.register_webhook(store, "gh", "wf")
    ep = webhooks.get_endpoint(store, "gh")
    body = b'{"event":"push"}'
    sig = _sign(secret, body)
    tampered = body + b" "
    with pytest.raises(webhooks.WebhookError) as exc:
        webhooks.verify_signature(ep, tampered, sig)
    assert exc.value.status_code == 401


def test_verify_wrong_secret_rejected(store):
    _, _ = webhooks.register_webhook(store, "gh", "wf")
    ep = webhooks.get_endpoint(store, "gh")
    body = b'whatever'
    wrong_sig = _sign("00" * 32, body)
    with pytest.raises(webhooks.WebhookError):
        webhooks.verify_signature(ep, body, wrong_sig)


def test_missing_header_rejected(store):
    _, _ = webhooks.register_webhook(store, "gh", "wf")
    ep = webhooks.get_endpoint(store, "gh")
    with pytest.raises(webhooks.WebhookError) as exc:
        webhooks.verify_signature(ep, b"body", "")
    assert exc.value.status_code == 401


def test_algo_mismatch_rejected(store):
    _, secret = webhooks.register_webhook(store, "gh", "wf", signature_algo="sha256")
    ep = webhooks.get_endpoint(store, "gh")
    body = b"x"
    bad = _sign(secret, body, algo="sha1")  # endpoint configured for sha256
    with pytest.raises(webhooks.WebhookError):
        webhooks.verify_signature(ep, body, bad)


def test_disable(store):
    webhooks.register_webhook(store, "gh", "wf")
    assert webhooks.get_endpoint(store, "gh") is not None
    assert webhooks.disable_webhook(store, "gh") is True
    assert webhooks.get_endpoint(store, "gh") is None  # filtered out


def test_bare_hex_signature_accepted_as_sha256(store):
    """Some upstreams send just the hex, no 'sha256=' prefix. We accept it
    as long as the endpoint is configured for sha256."""
    _, secret = webhooks.register_webhook(store, "gh", "wf", signature_algo="sha256")
    ep = webhooks.get_endpoint(store, "gh")
    body = b"x"
    expected = hmac.new(bytes.fromhex(secret), body, hashlib.sha256).hexdigest()
    # No '=' in header
    webhooks.verify_signature(ep, body, expected)
