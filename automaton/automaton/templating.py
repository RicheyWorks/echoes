"""Template resolution for step specs.

Before handing a step's spec to its handler, the engine walks the spec dict
and resolves any ${{ ... }} placeholders against the current run state. This
is what lets workflow author chain steps:

    - name: fetch
      type: http
      method: GET
      url: https://api.example.com/users/42
    - name: notify
      type: http
      needs: [fetch]
      method: POST
      url: https://hooks.example.com/notify
      body:
        user: "${{ steps.fetch.output.body }}"
        run_id: "${{ run.id }}"

Available references:
  steps.<step_name>.output[.PATH]    most recent completed step's output
  steps.<step_name>.status           'completed' | 'failed' | ...
  run.id
  run.payload[.PATH]                 the trigger payload

Path is dot-separated. Falls through dicts and lists by index.

Templates are string-only - a value of "${{ run.id }}" gets replaced with
the literal int. A string with embedded templates ("Run ${{ run.id }} done")
gets the templates interpolated as their str() form.

Missing references raise TemplateError, which the engine surfaces as a
StepError so the run fails cleanly (and respects retry policy).
"""
from __future__ import annotations

import json
import re
from typing import Any


class TemplateError(Exception):
    pass


_TEMPLATE_RE = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")
_SECRET_PREFIX = "secret:"


def _resolve_one(path: str, ctx: dict[str, Any],
                 secret_values: Optional[set] = None) -> Any:
    """Resolve a single ${{ ... }} expression.

    ``path`` is the body inside the braces. If it starts with
    ``secret:``, we look up the named secret via automaton.secrets. The
    resolved value is also added to ``secret_values`` if provided, so
    callers can redact it from outputs / logs.

    Other paths fall through to the run-state walker (_lookup).
    """
    stripped = path.strip()
    if stripped.startswith(_SECRET_PREFIX):
        name = stripped[len(_SECRET_PREFIX):].strip()
        # Local import keeps `secrets` truly optional for callers that
        # never reference one (avoids paying its keyring import cost).
        from . import secrets as _secrets
        try:
            value = _secrets.get(name)
        except _secrets.SecretError as e:
            raise TemplateError(str(e)) from e
        if secret_values is not None:
            secret_values.add(value)
        return value
    return _lookup(stripped, ctx)


def _lookup(path: str, ctx: dict[str, Any]) -> Any:
    parts = [p for p in path.split(".") if p]
    cur: Any = ctx
    for p in parts:
        if isinstance(cur, dict):
            if p not in cur:
                raise TemplateError(f"template path {path!r}: no key {p!r} in {sorted(cur.keys())}")
            cur = cur[p]
        elif isinstance(cur, list):
            try:
                idx = int(p)
            except ValueError:
                raise TemplateError(f"template path {path!r}: list index must be int, got {p!r}")
            if idx >= len(cur) or idx < -len(cur):
                raise TemplateError(f"template path {path!r}: index {idx} out of range")
            cur = cur[idx]
        else:
            raise TemplateError(f"template path {path!r}: can't descend into {type(cur).__name__} at {p!r}")
    return cur


def _build_context(conn, run_id: int) -> dict[str, Any]:
    """Snapshot the relevant run state into a dict for resolution.

    Only the LATEST attempt of each step name contributes; earlier failed
    attempts are not in 'steps' (their outputs aren't authoritative).
    """
    run_row = conn.execute(
        "SELECT id, trigger_payload FROM run WHERE id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        raise TemplateError(f"no run {run_id}")
    payload = None
    if run_row["trigger_payload"]:
        payload = json.loads(run_row["trigger_payload"])

    # Latest attempt per step name
    steps_data: dict[str, Any] = {}
    rows = conn.execute(
        "SELECT name, status, output_json, attempt FROM step "
        "WHERE run_id = ? AND attempt = ("
        "  SELECT MAX(attempt) FROM step s2 WHERE s2.run_id = step.run_id "
        "    AND s2.name = step.name)",
        (run_id,),
    ).fetchall()
    for r in rows:
        output = None
        if r["output_json"]:
            output = json.loads(r["output_json"])
        steps_data[r["name"]] = {
            "status": r["status"],
            "output": output,
            "attempt": r["attempt"],
        }

    return {
        "run": {"id": run_row["id"], "payload": payload},
        "steps": steps_data,
    }


def _render_string(s: str, ctx: dict[str, Any],
                   secret_values: Optional[set] = None) -> Any:
    """Resolve ${{ ... }} in a string.

    If the whole string is a single template ("${{ foo }}"), return the
    resolved value as-is (preserving its type). Otherwise, interpolate
    str() of each match into the original string.

    Pass ``secret_values`` as a set to collect any values that came from
    ``${{ secret:NAME }}`` lookups; the engine uses this to redact them
    from outputs and event-log payloads.
    """
    matches = list(_TEMPLATE_RE.finditer(s))
    if not matches:
        return s
    # Sole-template case: return the typed value
    if len(matches) == 1 and matches[0].group(0) == s.strip():
        return _resolve_one(matches[0].group(1), ctx, secret_values)
    # Interpolation case: stringify each match into the surrounding text
    out: list[str] = []
    last_end = 0
    for m in matches:
        out.append(s[last_end:m.start()])
        value = _resolve_one(m.group(1), ctx, secret_values)
        out.append(str(value))
        last_end = m.end()
    out.append(s[last_end:])
    return "".join(out)


def render(value: Any, ctx: dict[str, Any],
           secret_values: Optional[set] = None) -> Any:
    """Recursively resolve templates inside dicts, lists, and strings."""
    if isinstance(value, str):
        return _render_string(value, ctx, secret_values)
    if isinstance(value, dict):
        # For foreach steps, leave the nested "step" template unrendered so
        # the foreach handler can render it per-item with item/item_index injected.
        if value.get("type") == "foreach":
            return {k: (v if k == "step" else render(v, ctx, secret_values))
                    for k, v in value.items()}
        return {k: render(v, ctx, secret_values) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx, secret_values) for v in value]
    return value


def resolve_spec(conn, run_id: int, spec: dict[str, Any]):
    """Entry point used by the engine: build context, render the whole spec.

    Returns ``(rendered_spec, secret_values)`` where ``secret_values`` is
    a set of strings that came from ``${{ secret:NAME }}`` references.
    The engine should redact those from any persisted output.
    """
    ctx = _build_context(conn, run_id)
    secret_values: set = set()
    rendered = render(spec, ctx, secret_values)
    return rendered, secret_values
