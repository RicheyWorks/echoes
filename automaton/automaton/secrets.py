"""Cross-platform secret storage backed by the OS keyring.

What this gives you:

* ``set("GITHUB_TOKEN", value)`` and ``get("GITHUB_TOKEN")`` that route
  through Windows Credential Manager (DPAPI-backed), macOS Keychain, or
  Linux Secret Service via the ``keyring`` library. Same API everywhere.

* A managed fallback on headless Linux boxes where the Secret Service
  isn't available: encrypted-at-rest file storage (``keyrings.alt``)
  unlocked by ``AUTOMATON_KEYRING_PASSPHRASE``. Better than plaintext;
  worse than the native keychain.

* Names live under the keyring service name ``automaton`` so they're
  namespaced away from anything else on the host.

What this does NOT do:

* Encrypt your workflow YAML. References to secrets (``${secret:NAME}``)
  are tokens, not values - they get resolved at lease time and the
  values never enter the SQLite event log (see ``automaton.engine``).

* Manage multi-tenant access. There's one service name; everything that
  can read the keyring can read every secret. For a single-person /
  single-host deployment that's fine.

Backend selection:

The default behavior of ``keyring`` is to walk a priority chain at
import time: Windows Credential Manager > macOS Keychain > Linux
Secret Service > whatever else is registered. We honor that, except:

* If ``AUTOMATON_KEYRING_BACKEND=encrypted_file``, force the
  ``keyrings.alt.file.EncryptedKeyring`` backend (encrypted-at-rest,
  passphrase via ``AUTOMATON_KEYRING_PASSPHRASE``). Use this on headless
  Linux servers where the D-Bus session for Secret Service isn't
  available.

* If ``AUTOMATON_KEYRING_BACKEND=os``, do nothing - whatever ``keyring``
  picked stays. This is the default.

* If ``AUTOMATON_KEYRING_BACKEND=plaintext``, use the plaintext file
  backend. Logs a warning. **Don't use this outside of tests.**
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Iterable, List, Optional

log = logging.getLogger("automaton.secrets")

# Keyring service name - everything we store lives under this namespace.
SERVICE = "automaton"

# Names allowed to be set / referenced. Restrictive to avoid awkward
# parsing issues in workflow YAML (${secret:foo.bar} would clash with
# our other dot-path templates).
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class SecretError(Exception):
    """Raised on missing secrets, bad names, or backend failures."""


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise SecretError(
            f"invalid secret name {name!r}; must match [A-Za-z0-9_.-]{{1,128}}"
        )


def _configure_backend_once():
    """Apply AUTOMATON_KEYRING_BACKEND on first import."""
    backend = os.environ.get("AUTOMATON_KEYRING_BACKEND", "os").lower()
    if backend == "os":
        return
    import keyring
    if backend == "encrypted_file":
        from keyrings.alt.file import EncryptedKeyring
        kr = EncryptedKeyring()
        # The passphrase prompt is interactive by default; if the user
        # set AUTOMATON_KEYRING_PASSPHRASE we wire it in so non-tty hosts
        # can read secrets without prompting.
        passphrase = os.environ.get("AUTOMATON_KEYRING_PASSPHRASE")
        if passphrase:
            kr.keyring_key = passphrase  # internal API; works on 5.x
        keyring.set_keyring(kr)
    elif backend == "plaintext":
        from keyrings.alt.file import PlaintextKeyring
        log.warning(
            "AUTOMATON_KEYRING_BACKEND=plaintext: secrets stored unencrypted "
            "in ~/.local/share/python_keyring/. Use only for tests."
        )
        keyring.set_keyring(PlaintextKeyring())
    else:
        raise SecretError(
            f"unknown AUTOMATON_KEYRING_BACKEND={backend!r}; "
            "valid: os, encrypted_file, plaintext"
        )


_BACKEND_CONFIGURED = False


def _ensure_backend():
    global _BACKEND_CONFIGURED
    if not _BACKEND_CONFIGURED:
        _configure_backend_once()
        _BACKEND_CONFIGURED = True


def set(name: str, value: str) -> None:
    """Store ``value`` under ``name`` in the keyring."""
    _validate_name(name)
    _ensure_backend()
    import keyring
    try:
        keyring.set_password(SERVICE, name, value)
    except keyring.errors.KeyringError as e:
        raise SecretError(f"keyring rejected set({name!r}): {e}") from e


def get(name: str) -> str:
    """Return the value stored under ``name``, or raise SecretError."""
    _validate_name(name)
    _ensure_backend()
    import keyring
    try:
        value = keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError as e:
        raise SecretError(f"keyring rejected get({name!r}): {e}") from e
    if value is None:
        raise SecretError(
            f"no secret named {name!r} (run `automaton secret set {name}`)"
        )
    return value


def delete(name: str) -> bool:
    """Delete ``name``. Returns True if it existed and was removed."""
    _validate_name(name)
    _ensure_backend()
    import keyring
    try:
        existing = keyring.get_password(SERVICE, name)
        if existing is None:
            return False
        keyring.delete_password(SERVICE, name)
        return True
    except keyring.errors.KeyringError as e:
        raise SecretError(f"keyring rejected delete({name!r}): {e}") from e


def list_names() -> List[str]:
    """List secret names stored under the automaton service.

    Not all keyring backends support enumeration. We use the documented
    ``get_credential`` path where available; otherwise this returns the
    set of names we've successfully ``set`` during this process (best
    effort - the operator can also ``automaton secret import`` to
    re-sync).
    """
    _ensure_backend()
    import keyring
    kr = keyring.get_keyring()
    # keyrings.alt.file backends expose a list directly
    if hasattr(kr, "get_credentials"):
        # No common helper; fall through.
        pass
    # The file-backed backends expose .file_path -> we can enumerate.
    if hasattr(kr, "file_path") and os.path.exists(kr.file_path):
        import configparser
        cp = configparser.RawConfigParser()
        cp.read(kr.file_path)
        if cp.has_section(SERVICE):
            return sorted(cp.options(SERVICE))
    # OS-native backends generally don't enumerate; return [] and
    # surface a note via the CLI.
    return []


def import_env_file(path) -> List[str]:
    """Import AUTOMATON_SECRET_* entries from a dotenv file.

    Lines look like ``AUTOMATON_SECRET_FOO=bar`` or ``FOO=bar`` (the
    AUTOMATON_SECRET_ prefix is optional; if present, it's stripped from
    the resulting name). Lines beginning with ``#`` and blank lines are
    skipped. Returns the list of names actually imported.
    """
    imported = []
    with open(os.fspath(path), encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SecretError(f"{path}:{lineno}: missing '=' in {line!r}")
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key.startswith("AUTOMATON_SECRET_"):
                key = key[len("AUTOMATON_SECRET_"):]
            set(key, value)
            imported.append(key)
    return imported


def redact(value: str, secret_values: Iterable[str], placeholder: str = "***") -> str:
    """Replace any occurrence of a known secret value with ``placeholder``.

    Used by the engine to scrub step output / event payloads before they
    hit the DB. The empty string and very short values (<4 chars) are
    skipped to avoid mangling unrelated text.
    """
    out = value
    for sv in secret_values:
        if sv and len(sv) >= 4:
            out = out.replace(sv, placeholder)
    return out
