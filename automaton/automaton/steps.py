"""Step types.

Built-in step types live below. External plugins register via the
'automaton.step_types' entry-point group; importing the named module is
expected to call @step_type at module scope.

Step handler signature (current):
    def handler(spec, idempotency_key, context=None) -> dict

`context` is an optional StepContext (see below) carrying run_id, step_name,
attempt, and a DB connection. Handlers that don't need it can keep the
older two-arg signature - the engine introspects and only passes context
when the handler accepts it.

Exceptions handlers may raise:
- StepError: this step failed. The engine records the failure and may retry
  per the workflow's retry policy.
- StepWaiting: this step isn't done yet; come back later. The engine
  re-queues the SAME step row (same idempotency key) with a future ready_at.
  Use for wait-for-signal / wait-for-condition patterns.
"""
from __future__ import annotations

import inspect
import logging
import sqlite3
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("automaton.steps")


class StepError(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class StepWaiting(Exception):
    """Raised by a step that isn't done yet. The engine re-queues it.

    retry_after_seconds: when the step should next be polled.
    reason: optional human-readable note included in the event log.
    """
    def __init__(self, retry_after_seconds: float = 5.0, reason: str = ""):
        super().__init__(reason or "waiting")
        self.retry_after_seconds = float(retry_after_seconds)
        self.reason = reason


@dataclass
class StepContext:
    run_id: int
    step_name: str
    attempt: int
    conn: sqlite3.Connection


_STEP_TYPES: dict[str, Callable] = {}
_PLUGINS_LOADED = False


def step_type(name):
    def decorator(fn):
        _STEP_TYPES[name] = fn
        return fn
    return decorator


def _load_plugins():
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    try:
        eps = entry_points(group="automaton.step_types")
    except TypeError:
        eps = entry_points().get("automaton.step_types", [])
    for ep in eps:
        try:
            ep.load()
            log.info("loaded step-type plugin: %s -> %s", ep.name, ep.value)
        except Exception as e:
            log.error("failed to load step-type plugin %s (%s): %s",
                      ep.name, ep.value, e)


def registered_types():
    _load_plugins()
    return sorted(_STEP_TYPES.keys())


def run_step(spec, idempotency_key, context: Optional[StepContext] = None):
    """Dispatch a step. Step handlers may take (spec, key) or (spec, key, context)."""
    _load_plugins()
    type_name = spec.get("type")
    if type_name not in _STEP_TYPES:
        raise StepError(
            f"unknown step type: {type_name!r}. "
            f"Known types: {sorted(_STEP_TYPES.keys())}"
        )
    handler = _STEP_TYPES[type_name]
    sig = inspect.signature(handler)
    if "context" in sig.parameters:
        return handler(spec, idempotency_key, context=context)
    return handler(spec, idempotency_key)


# --- built-in step types ---

@step_type("http_get")
def _http_get(spec, idempotency_key):
    url = spec["url"]
    timeout = spec.get("timeout", 10.0)
    headers = dict(spec.get("headers") or {})
    headers["Idempotency-Key"] = idempotency_key
    try:
        r = httpx.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        raise StepError(f"http_get failed: {e}", {"url": url}) from e
    return {"status_code": r.status_code, "headers": dict(r.headers),
            "body": r.text[:10000]}


@step_type("file_append")
def _file_append(spec, idempotency_key):
    """Self-deduplicating: writes a line tagged with the idempotency key,
    or no-ops if a marker with that key is already present.

    UTF-8 and \\n line endings are forced for cross-platform consistency
    (Windows would otherwise default to cp1252 / CRLF).
    """
    path = spec["path"]
    text = spec.get("text", "x")
    marker = f"[{idempotency_key}]"
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = ""
    if marker in existing:
        return {"appended": False, "reason": "idempotency_key already present"}
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{marker}{text}\n")
    return {"appended": True}


@step_type("wait_for_signal")
def _wait_for_signal(spec, idempotency_key, context: Optional[StepContext] = None):
    """Park until a signal with `name` arrives for this run, or timeout.

    spec fields:
      signal (required): signal name to wait for
      timeout_seconds (default 3600): give up after this many seconds
      poll_seconds (default 5): how often to re-check

    On signal: returns {payload, sent_at}, signal is marked consumed.
    On timeout: StepError.
    Otherwise: StepWaiting, the engine re-queues this step.
    """
    if context is None:
        raise StepError("wait_for_signal requires the engine to pass a context "
                        "(this is a bug; report it)")
    # 'signal' is the signal name this step waits for. We can't use 'name'
    # because that collides with the step's own identifier in the workflow.
    signal_name = spec.get("signal")
    if not signal_name:
        raise StepError("wait_for_signal: missing required 'signal' field")
    timeout = float(spec.get("timeout_seconds", 3600))
    poll = float(spec.get("poll_seconds", 5.0))
    conn = context.conn

    # Look for an unconsumed signal matching (this run, this name).
    row = conn.execute(
        "SELECT id, payload_json, sent_at FROM signal "
        "WHERE run_id = ? AND name = ? AND consumed_at IS NULL "
        "ORDER BY sent_at LIMIT 1",
        (context.run_id, signal_name),
    ).fetchone()
    if row is not None:
        # Consume it.
        conn.execute(
            "UPDATE signal SET consumed_at = datetime('now'), "
            "  consumed_by_step_id = (SELECT id FROM step WHERE run_id = ? "
            "                          AND name = ? AND attempt = ?) "
            "WHERE id = ?",
            (context.run_id, context.step_name, context.attempt, row["id"]),
        )
        payload = None
        if row["payload_json"]:
            import json
            payload = json.loads(row["payload_json"])
        return {"signal_received": signal_name, "payload": payload, "sent_at": row["sent_at"]}

    # Timeout check: look at the step's started_at to know when we first ran.
    started = conn.execute(
        "SELECT started_at FROM step WHERE run_id = ? AND name = ? AND attempt = ?",
        (context.run_id, context.step_name, context.attempt),
    ).fetchone()["started_at"]
    if started:
        elapsed = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 86400 AS s",
            (started,),
        ).fetchone()["s"]
        if elapsed is not None and elapsed >= timeout:
            raise StepError(f"wait_for_signal timed out after {elapsed:.1f}s",
                            {"signal": signal_name, "timeout_seconds": timeout})

    # Not ready yet - tell the engine to come back later.
    raise StepWaiting(retry_after_seconds=poll, reason=f"waiting for signal {signal_name!r}")


@step_type("http")
def _http(spec, idempotency_key):
    """Generalized HTTP step. Supports GET/POST/PUT/DELETE/PATCH.

    spec fields:
      method (default 'GET'), url (required), headers (optional dict),
      body (optional - dict gets JSON-serialized), timeout (default 30)
    """
    method = (spec.get("method") or "GET").upper()
    url = spec["url"]
    timeout = spec.get("timeout", 30.0)
    headers = dict(spec.get("headers") or {})
    headers.setdefault("Idempotency-Key", idempotency_key)
    body = spec.get("body")
    kwargs = {"headers": headers, "timeout": timeout}
    if body is not None:
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body).encode("utf-8")
    try:
        r = httpx.request(method, url, **kwargs)
    except Exception as e:
        raise StepError(f"http {method} failed: {e}",
                        {"url": url, "method": method}) from e
    return {
        "status_code": r.status_code,
        "headers": dict(r.headers),
        "body": r.text[:10000],
    }


@step_type("shell")
def _shell(spec, idempotency_key):
    """Run a command in a subprocess. Use with care: side effects depend on
    the command being idempotent or rerun-safe.

    spec fields:
      cmd (required): list ['ls', '-la'] or string (no shell=True for safety)
      cwd, env (optional), timeout (default 60),
      ok_returncodes (default [0]): which exit codes count as success

    Cross-platform notes:
      - List form is portable; what you write is what subprocess gets.
      - String form is tokenized with shlex. POSIX rules on macOS/Linux,
        Windows rules (no backslash-as-escape) on Windows. If you need a
        full shell pipeline, write a list like
            ['sh', '-c', '...']      on POSIX, or
            ['cmd.exe', '/c', '...'] on Windows.
    """
    import os
    import subprocess  # local import - shell is opt-in
    cmd = spec["cmd"]
    if isinstance(cmd, str):
        # shlex.split's default POSIX mode treats backslashes as escapes,
        # which corrupts Windows paths like C:\Users\foo. posix=False fixes
        # the tokenization on Windows without changing POSIX behavior.
        import shlex
        cmd = shlex.split(cmd, posix=(os.name != "nt"))
    cwd = spec.get("cwd")
    env_extra = spec.get("env") or {}
    timeout = float(spec.get("timeout", 60))
    ok = set(spec.get("ok_returncodes", [0]))

    env = {**os.environ, **env_extra,
           "AUTOMATON_IDEMPOTENCY_KEY": idempotency_key}
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                           capture_output=True, text=True)
    except FileNotFoundError as e:
        raise StepError(f"shell: command not found: {e}", {"cmd": cmd}) from e
    except subprocess.TimeoutExpired as e:
        raise StepError(f"shell: timeout after {timeout}s",
                        {"cmd": cmd, "timeout": timeout}) from e
    if r.returncode not in ok:
        raise StepError(
            f"shell: returncode {r.returncode} not in {sorted(ok)}",
            {"cmd": cmd, "returncode": r.returncode,
             "stdout": r.stdout[:2000], "stderr": r.stderr[:2000]},
        )
    return {
        "returncode": r.returncode,
        "stdout": r.stdout[:10000],
        "stderr": r.stderr[:2000],
    }


@step_type("python")
def _python(spec, idempotency_key):
    """Run a Python function in the current process.

    stdout (print() calls) is captured and returned alongside the function's
    return value. The return value must be JSON-serialisable; if it isn't,
    its repr() is used instead.

    spec fields:
      module   (required): dotted module path, e.g. ``my_package.tasks``
      function (required): function name within the module
      kwargs   (optional): dict of keyword arguments passed to the function

    Captured output is truncated to 50 000 chars to prevent runaway step
    rows in the DB.

    Notes:
    - The function runs in the worker process with access to whatever is on
      sys.path. If you need an isolated environment, use the ``shell`` step
      type and invoke a subprocess.
    - The step is NOT idempotent by default. If your function has external
      side effects (writing files, calling APIs, sending email) it must guard
      against re-execution itself. The idempotency key is available via the
      AUTOMATON_IDEMPOTENCY_KEY env variable if you need to pass it in.
    """
    import contextlib
    import importlib
    import io
    import os
    import json as _json

    module_name = spec.get("module")
    func_name = spec.get("function")
    kwargs = dict(spec.get("kwargs") or {})

    if not module_name:
        raise StepError("python: missing required field 'module'")
    if not func_name:
        raise StepError("python: missing required field 'function'")

    # Expose the idempotency key as an env var so functions can read it.
    old_val = os.environ.get("AUTOMATON_IDEMPOTENCY_KEY")
    os.environ["AUTOMATON_IDEMPOTENCY_KEY"] = idempotency_key
    try:
        try:
            mod = importlib.import_module(module_name)
        except ImportError as e:
            raise StepError(
                f"python: cannot import module {module_name!r}: {e}",
                {"module": module_name, "error": str(e)},
            ) from e

        try:
            func = getattr(mod, func_name)
        except AttributeError as e:
            raise StepError(
                f"python: no attribute {func_name!r} in {module_name!r}",
                {"module": module_name, "function": func_name},
            ) from e

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buf), \
                 contextlib.redirect_stderr(stderr_buf):
                result = func(**kwargs)
        except StepError:
            raise
        except Exception as e:
            raise StepError(
                f"python: {module_name}.{func_name} raised {type(e).__name__}: {e}",
                {"module": module_name, "function": func_name,
                 "exception": type(e).__name__, "message": str(e)},
            ) from e
    finally:
        if old_val is None:
            os.environ.pop("AUTOMATON_IDEMPOTENCY_KEY", None)
        else:
            os.environ["AUTOMATON_IDEMPOTENCY_KEY"] = old_val

    # Ensure return value is JSON-serialisable; fall back to repr().
    try:
        _json.dumps(result)
        return_value = result
    except (TypeError, ValueError):
        return_value = repr(result)

    stdout_text = stdout_buf.getvalue()[:50_000]
    stderr_text = stderr_buf.getvalue()[:10_000]

    output = {
        "return_value": return_value,
        "stdout": stdout_text,
    }
    if stderr_text:
        output["stderr"] = stderr_text
    return output


@step_type("foreach")
def _foreach(spec, idempotency_key, context=None):
    """Fan-out step: run a nested step spec once per item in a list.

    Spec fields
    -----------
    items     : list (or ${{...}} template that resolves to a list)
    step      : dict — nested step spec (may contain ${{item}} and ${{item_index}})
    fail_fast : bool — stop on first failure (default: True)

    Template context inside ``step``
    ---------------------------------
    ${{ item }}        — the current element
    ${{ item_index }}  — 0-based iteration index

    Output
    ------
    {"results": [...], "count": N, "failed": K}
    Each result entry: {"item": ..., "item_index": ..., "output": ...}
                     or {"item": ..., "item_index": ..., "error": "..."}
    """
    import json as _json
    from . import templating as _templating

    items = spec.get("items")
    step_template = spec.get("step")
    fail_fast = spec.get("fail_fast", True)

    if not isinstance(items, list):
        raise StepError(
            f"foreach: 'items' must be a list, got {type(items).__name__}",
            {"items_type": type(items).__name__},
        )
    if not isinstance(step_template, dict):
        raise StepError("foreach: 'step' must be a dict (nested step spec)")
    if not step_template.get("type"):
        raise StepError("foreach: nested 'step' is missing required field 'type'")

    results: list = []
    failed = 0

    for i, item in enumerate(items):
        # Build per-item template context.
        if context is not None:
            base_ctx = _templating._build_context(context.conn, context.run_id)
        else:
            base_ctx = {"run": {"id": None, "payload": {}}, "steps": {}}
        base_ctx["item"] = item
        base_ctx["item_index"] = i

        # Render the nested step spec with item injected.
        try:
            rendered_step = _templating.render(step_template, base_ctx)
        except _templating.TemplateError as e:
            results.append({"item": item, "item_index": i, "error": str(e)})
            failed += 1
            if fail_fast:
                raise StepError(
                    f"foreach: template error at item {i} ({item!r}): {e}",
                    {"results": results, "count": len(items), "failed": failed},
                ) from e
            continue

        item_key = f"{idempotency_key}:item:{i}"
        try:
            out = run_step(rendered_step, item_key, context=context)
            results.append({"item": item, "item_index": i, "output": out})
        except StepError as e:
            results.append({"item": item, "item_index": i, "error": str(e)})
            failed += 1
            if fail_fast:
                raise StepError(
                    f"foreach: item {i} ({item!r}) failed: {e}",
                    {"results": results, "count": len(items), "failed": failed},
                ) from e

    if failed > 0 and not fail_fast:
        raise StepError(
            f"foreach: {failed}/{len(items)} items failed",
            {"results": results, "count": len(items), "failed": failed},
        )

    return {"results": results, "count": len(items), "failed": failed}


# ============================================================
# echoes_agent step type
# ============================================================

@step_type("echoes_agent")
def _echoes_agent(spec, idempotency_key):
    """Drive an echoes forensic agent as a workflow step.

    Shells out to the ``echoes`` binary (must be on PATH or set via ``binary``).
    Three actions mirror the three CLI subcommands:

    * ``run``    — advance the agent by N ticks, persisting every entry.
    * ``verify`` — reload from DB and assert hash-chain integrity.
    * ``report`` — return the full JSON memory dump as structured step output.

    spec fields
    -----------
    action  : 'run' | 'verify' | 'report'  (default: 'run')
    db      : path to the echoes SQLite DB  (default: 'echoes.db')
    name    : agent name                    (default: 'Echo')
    goal    : agent goal string             (default: 'map environment with cryptographic memory')
    ticks   : ticks to run (run only)       (default: 8)
    binary  : path to echoes binary         (default: auto-discovered)
    timeout : subprocess timeout seconds    (default: 120)

    Outputs by action
    -----------------
    run    -> {agent, ticks_run, total_memories, merkle_root, stdout}
    verify -> {agent, entries, integrity, merkle_root}
    report -> full echoes JSON: {agent, goal, entries, integrity, merkle_root, memory[...]}

    Raises StepError if the binary exits non-zero, is not found, times out,
    or if verify reports a chain integrity failure.
    """
    import json as _json
    import subprocess

    action  = spec.get("action", "run")
    db      = spec.get("db", "echoes.db")
    name    = spec.get("agent_name") or spec.get("name", "Echo")
    goal    = spec.get("goal", "map environment with cryptographic memory")
    ticks   = int(spec.get("ticks", 8))
    timeout = float(spec.get("timeout", 120))
    binary  = spec.get("binary") or _find_echoes_binary()

    if action not in ("run", "verify", "report"):
        raise StepError(
            f"echoes_agent: invalid action {action!r}; "
            "must be 'run', 'verify', or 'report'",
            {"action": action},
        )

    if binary is None:
        raise StepError(
            "echoes_agent: cannot find echoes binary. "
            "Put it on PATH or set 'binary' in the step spec.",
            {"hint": "cd echoes-v1.0-forensic/echoes && cargo build --release"},
        )

    if action == "run":
        cmd = [binary, "run", "--db", db, "--ticks", str(ticks),
               "--name", name, "--goal", goal]
    elif action == "verify":
        cmd = [binary, "verify", "--db", db, "--name", name]
    else:
        cmd = [binary, "report", "--db", db, "--name", name, "--json"]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise StepError(
            f"echoes_agent: binary not found: {e}", {"binary": binary},
        ) from e
    except subprocess.TimeoutExpired as e:
        raise StepError(
            f"echoes_agent: timed out after {timeout}s", {"cmd": cmd},
        ) from e

    if r.returncode != 0:
        raise StepError(
            f"echoes_agent {action!r} failed (exit {r.returncode})",
            {"returncode": r.returncode,
             "stdout": r.stdout[:2000],
             "stderr": r.stderr[:2000]},
        )

    if action == "run":
        return _echoes_parse_run(r.stdout, name, ticks)
    if action == "verify":
        return _echoes_parse_verify(r.stdout, name)
    # report
    try:
        return _json.loads(r.stdout)
    except _json.JSONDecodeError as e:
        raise StepError(
            f"echoes_agent report: JSON parse failed: {e}",
            {"stdout": r.stdout[:500]},
        ) from e


# --- echoes helpers (private) ---

def _find_echoes_binary():
    """Return the echoes binary path: PATH first, then monorepo build dirs."""
    import os
    import shutil

    found = shutil.which("echoes")
    if found:
        return found

    # Monorepo layout: automaton/ is next to echoes-v1.0-forensic/echoes/
    base = os.path.join("..", "echoes-v1.0-forensic", "echoes", "target")
    candidates = [
        os.path.join(base, "release", "echoes"),
        os.path.join(base, "debug",   "echoes"),
    ]
    if os.name == "nt":
        candidates = [c + ".exe" for c in candidates] + candidates
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _echoes_parse_run(stdout: str, name: str, ticks_requested: int) -> dict:
    """Parse human-readable output of ``echoes run``.

    Last line: ``Done — 10 total memories | Merkle root: 5422..b989``
    """
    import re
    result: dict = {
        "agent":          name,
        "ticks_run":      ticks_requested,
        "total_memories": None,
        "merkle_root":    None,
        "stdout":         stdout,
    }
    m = re.search(r"Done — (\d+) total memories \| Merkle root: (\S+)", stdout)
    if m:
        result["total_memories"] = int(m.group(1))
        result["merkle_root"]    = m.group(2)
    return result


def _echoes_parse_verify(stdout: str, name: str) -> dict:
    """Parse human-readable output of ``echoes verify``.

    Expected lines::
        Agent 'Echo' — 10 entries loaded.
        Hash-chain integrity: PASSED ✓
        Merkle root:          5422..b989

    Raises StepError when integrity is FAILED so the workflow step fails.
    """
    import re
    result: dict = {
        "agent":       name,
        "entries":     None,
        "integrity":   "unknown",
        "merkle_root": None,
    }
    m = re.search(r"(\d+) entries loaded", stdout)
    if m:
        result["entries"] = int(m.group(1))
    if "PASSED" in stdout:
        result["integrity"] = "ok"
    elif "FAILED" in stdout:
        result["integrity"] = "failed"
    m = re.search(r"Merkle root:\s+(\S+)", stdout)
    if m:
        result["merkle_root"] = m.group(1)
    if result["integrity"] == "failed":
        raise StepError(
            f"echoes_agent verify: hash-chain integrity FAILED for agent {name!r}",
            result,
        )
    return result
