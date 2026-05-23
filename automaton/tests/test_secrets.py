"""Secrets module + ${secret:NAME} template integration + redaction.

A per-test in-memory keyring backend keeps these from touching the
host's real keyring. We swap it in via ``keyring.set_keyring(...)`` in
the fixture and reset to the previous backend after.
"""
from __future__ import annotations

import json

import pytest

from automaton import db as _db
from automaton import engine
from automaton import secrets as _secrets
from automaton import templating


# --- in-memory keyring backend ---

import keyring.backend as _kb


class _InMemoryKeyring(_kb.KeyringBackend):
    """Minimal keyring.backend.KeyringBackend that stores in a dict."""
    priority = 999

    def __init__(self):
        super().__init__()
        self._store = {}

    def set_password(self, service, name, value):
        self._store[(service, name)] = value

    def get_password(self, service, name):
        return self._store.get((service, name))

    def delete_password(self, service, name):
        key = (service, name)
        if key in self._store:
            del self._store[key]
        else:
            # Match the keyring library's contract.
            import keyring.errors
            raise keyring.errors.PasswordDeleteError(f"no {name!r}")


@pytest.fixture
def keyring_backend(monkeypatch):
    """Swap in an in-memory backend and reset _BACKEND_CONFIGURED so
    automaton.secrets doesn't try to apply env-driven overrides."""
    import keyring
    backend = _InMemoryKeyring()
    monkeypatch.setattr(_secrets, "_BACKEND_CONFIGURED", True)  # skip our own reconfig
    prev = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(prev)


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


# ------------------------ basic CRUD ------------------------

def test_set_get_round_trip(keyring_backend):
    _secrets.set("GITHUB_TOKEN", "gho_abc123")
    assert _secrets.get("GITHUB_TOKEN") == "gho_abc123"


def test_get_missing_raises(keyring_backend):
    with pytest.raises(_secrets.SecretError, match="no secret named"):
        _secrets.get("DOES_NOT_EXIST")


def test_delete_round_trip(keyring_backend):
    _secrets.set("TMP", "x")
    assert _secrets.delete("TMP") is True
    assert _secrets.delete("TMP") is False  # idempotent


def test_invalid_name_rejected(keyring_backend):
    with pytest.raises(_secrets.SecretError, match="invalid secret name"):
        _secrets.set("has spaces", "x")
    with pytest.raises(_secrets.SecretError, match="invalid secret name"):
        _secrets.get("/bad/path")


def test_import_env_file(keyring_backend, tmp_path):
    f = tmp_path / "secrets.env"
    f.write_text(
        "# comment line\n"
        "AUTOMATON_SECRET_GITHUB_TOKEN=ghp_xyz\n"
        '\n'
        "AUTOMATON_SECRET_QUOTED=\"with spaces\"\n"
        "PLAIN_NAME=raw_value\n",  # AUTOMATON_SECRET_ prefix is optional
        encoding="utf-8",
    )
    imported = _secrets.import_env_file(f)
    assert set(imported) == {"GITHUB_TOKEN", "QUOTED", "PLAIN_NAME"}
    assert _secrets.get("GITHUB_TOKEN") == "ghp_xyz"
    assert _secrets.get("QUOTED") == "with spaces"
    assert _secrets.get("PLAIN_NAME") == "raw_value"


def test_redact_replaces_known_values():
    out = _secrets.redact("token=abc123 used", {"abc123"})
    assert out == "token=*** used"


def test_redact_skips_tiny_values():
    """A 3-character 'secret' would mangle unrelated text - skip it."""
    out = _secrets.redact("the dog ran", {"the"})
    assert out == "the dog ran"


# --------- template-layer integration ---------

def test_secret_template_resolves(keyring_backend):
    """Use templating.render directly so we don't need a real run row."""
    _secrets.set("API_KEY", "super-secret-key")
    seen = set()
    out = templating.render({"key": "${{ secret:API_KEY }}"}, ctx={}, secret_values=seen)
    assert out == {"key": "super-secret-key"}
    assert "super-secret-key" in seen


def test_secret_template_missing_raises_template_error(keyring_backend):
    with pytest.raises(templating.TemplateError, match="no secret named"):
        templating.render({"key": "${{ secret:NOPE }}"}, ctx={})


# --------- end-to-end: secret value redacted from the event log ---------

def test_secret_value_redacted_from_step_output(keyring_backend, store, tmp_path):
    """A shell step that echoes the secret should NOT leak the value into
    step.output_json. Smoke test of the engine's _scrub pass."""
    _secrets.set("SHELL_TOKEN", "leak-me-please")
    engine.register_workflow(store, {
        "name": "leaks",
        "steps": [{
            "name": "echo_secret",
            "type": "shell",
            "cmd": ["sh", "-c", "echo ${{ secret:SHELL_TOKEN }}"],
        }],
    })
    rid = engine.trigger_run(store, "leaks")
    engine.worker_loop(store, stop_when_idle=True)

    detail = engine.run_detail(store, rid)
    assert detail["run"]["status"] == "completed"
    out = detail["steps"][0]["output"]  # already parsed dict
    # The actual secret value never appears in persisted output.
    assert "leak-me-please" not in json.dumps(detail)
    # The redacted placeholder does appear in the shell stdout.
    assert "***" in out["stdout"]


def test_secret_value_not_in_event_log(keyring_backend, store, tmp_path):
    """Same protection on the run-level event log."""
    _secrets.set("EV_TOKEN", "should-not-appear-in-events")
    target = tmp_path / "out.log"
    engine.register_workflow(store, {
        "name": "eventy",
        "steps": [{
            "name": "writer",
            "type": "file_append",
            "path": str(target),
            "text": "got ${{ secret:EV_TOKEN }}",
        }],
    })
    engine.trigger_run(store, "eventy")
    engine.worker_loop(store, stop_when_idle=True)

    rows = store.execute(
        "SELECT payload_json FROM event_log WHERE payload_json IS NOT NULL"
    ).fetchall()
    blob = "\n".join(r["payload_json"] for r in rows)
    assert "should-not-appear-in-events" not in blob


def test_raw_spec_keeps_secret_reference_not_value(keyring_backend, store):
    """The persisted input_json should hold the ${secret:...} REFERENCE,
    not the resolved value. Regression guard for 'we accidentally stored
    the secret in plaintext'."""
    _secrets.set("REF_TOKEN", "raw-never-stored")
    engine.register_workflow(store, {
        "name": "ref",
        "steps": [{
            "name": "n",
            "type": "shell",
            "cmd": ["sh", "-c", "echo ${{ secret:REF_TOKEN }}"],
        }],
    })
    engine.trigger_run(store, "ref")
    # Don't run the worker yet - check the input_json before resolution.
    row = store.execute(
        "SELECT input_json FROM step WHERE name = 'n'"
    ).fetchone()
    assert "raw-never-stored" not in row["input_json"]
    assert "secret:REF_TOKEN" in row["input_json"]
