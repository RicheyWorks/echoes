"""Notifications & alerting via Apprise.

When a run reaches a terminal state, the engine optionally pushes a
message to one or more channels. We use `Apprise <https://github.com/caronc/apprise>`_
as the dispatch layer because it speaks 110+ backends with one API:
ntfy, Pushover, Discord, Slack, Telegram, email, gotify, the lot.

Configuration is env-var driven, so multiple workers/schedulers/UI
processes on the same host can share it without each carrying its own
config file:

* ``AUTOMATON_NOTIFY_ON_FAILURE``  - Apprise URL(s) fired on terminal
  ``failed`` runs. Multiple URLs go in a single env var separated by a
  newline or by space - apprise accepts both via ``add()``.

* ``AUTOMATON_NOTIFY_ON_SUCCESS``  - same shape, fired on ``completed``
  runs. Off by default (would be noisy).

* ``AUTOMATON_NOTIFY_QUIET_HOURS``  - ``HH:MM-HH:MM`` (24h, local time).
  Non-urgent notifications during this window are dropped. ``urgent``
  notifications go through anyway. Wraps midnight: ``22:00-07:00`` is
  the obvious "while I sleep" interpretation.

* Workflows can opt into "urgent" by setting a top-level
  ``urgent: true`` in the YAML; the engine reads it from the workflow
  spec when building the notification context.

This module is intentionally fire-and-forget: a notification failure
is logged at WARNING and does not affect run state. The cost of getting
this wrong is "I missed an alert," not "my workflow got stuck."
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any, Iterable, List, Optional

log = logging.getLogger("automaton.notify")


# Env var names (constants so the CLI test command can reuse them)
ENV_NOTIFY_ON_FAILURE = "AUTOMATON_NOTIFY_ON_FAILURE"
ENV_NOTIFY_ON_SUCCESS = "AUTOMATON_NOTIFY_ON_SUCCESS"
ENV_NOTIFY_ON_TIMEOUT = "AUTOMATON_NOTIFY_ON_TIMEOUT"
ENV_NOTIFY_QUIET_HOURS = "AUTOMATON_NOTIFY_QUIET_HOURS"


# --- helpers ---------------------------------------------------------

def _split_urls(blob: Optional[str]) -> List[str]:
    """Split an env-var value into a list of Apprise URLs."""
    if not blob:
        return []
    parts = []
    for chunk in blob.replace("\n", " ").split():
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _parse_quiet_hours(spec: Optional[str]):
    """Parse 'HH:MM-HH:MM'. Returns (start, end) as time objects, or None."""
    if not spec:
        return None
    try:
        start_s, end_s = spec.split("-", 1)
        start = _dt.time.fromisoformat(start_s.strip())
        end = _dt.time.fromisoformat(end_s.strip())
        return start, end
    except (ValueError, AttributeError):
        log.warning(
            "ignoring malformed %s=%r (expected HH:MM-HH:MM)",
            ENV_NOTIFY_QUIET_HOURS, spec,
        )
        return None


def is_quiet_hours(now: Optional[_dt.datetime] = None) -> bool:
    """True if the current local time is inside the configured quiet window."""
    window = _parse_quiet_hours(os.environ.get(ENV_NOTIFY_QUIET_HOURS))
    if window is None:
        return False
    start, end = window
    if now is None:
        now = _dt.datetime.now()
    t = now.time()
    if start <= end:
        return start <= t < end
    # Wraps midnight (e.g. 22:00-07:00).
    return t >= start or t < end


# --- core dispatch ---------------------------------------------------

def send(urls: Iterable[str], title: str, body: str,
         urgent: bool = False, now: Optional[_dt.datetime] = None) -> dict:
    """Send a notification to the given Apprise URLs.

    Returns a dict like ``{"sent": True, "channels": 2, "skipped": False,
    "reason": None}``. ``skipped`` is True if quiet hours suppressed a
    non-urgent send; ``sent`` is False if Apprise reported an outright
    failure.
    """
    urls = list(urls)
    if not urls:
        return {"sent": False, "channels": 0, "skipped": False,
                "reason": "no notify URLs configured"}

    if not urgent and is_quiet_hours(now):
        return {"sent": False, "channels": len(urls), "skipped": True,
                "reason": "quiet hours active"}

    try:
        import apprise
    except ImportError:
        log.warning("apprise not installed; skipping notify")
        return {"sent": False, "channels": 0, "skipped": False,
                "reason": "apprise not installed"}

    ap = apprise.Apprise()
    for url in urls:
        if not ap.add(url):
            log.warning("apprise rejected URL %r (typo? unsupported scheme?)", url)
    if len(ap) == 0:
        return {"sent": False, "channels": 0, "skipped": False,
                "reason": "all URLs were rejected by apprise"}

    try:
        ok = ap.notify(title=title, body=body)
    except Exception as e:  # apprise can raise on transport errors
        log.warning("apprise notify raised: %s", e)
        return {"sent": False, "channels": len(ap), "skipped": False,
                "reason": str(e)}

    return {"sent": bool(ok), "channels": len(ap),
            "skipped": False,
            "reason": None if ok else "apprise reported send failure"}


# --- per-run dispatch ------------------------------------------------

def build_run_context(conn, run_id: int) -> dict:
    """Snapshot the run state into a template-friendly dict."""
    row = conn.execute(
        "SELECT r.id, r.status, r.started_at, r.finished_at, r.trigger_kind, "
        "       wf.name AS workflow, wf.spec_json "
        "FROM run r JOIN workflow_def wf ON wf.id = r.workflow_def_id "
        "WHERE r.id = ?", (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    spec = {}
    if row["spec_json"]:
        try:
            spec = json.loads(row["spec_json"])
        except json.JSONDecodeError:
            pass

    # Find the most recent FAILED step name for context, if any.
    failed_step = None
    fail_row = conn.execute(
        "SELECT name FROM step WHERE run_id = ? AND status = 'failed' "
        "ORDER BY finished_at DESC LIMIT 1", (run_id,),
    ).fetchone()
    if fail_row:
        failed_step = fail_row["name"]

    # Duration in whole seconds, when finished.
    duration = None
    if row["started_at"] and row["finished_at"]:
        dur_row = conn.execute(
            "SELECT CAST((julianday(?) - julianday(?)) * 86400 AS INTEGER) AS s",
            (row["finished_at"], row["started_at"]),
        ).fetchone()
        duration = dur_row["s"]

    return {
        "run_id": row["id"],
        "workflow": row["workflow"],
        "status": row["status"],
        "trigger": row["trigger_kind"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_seconds": duration,
        "failed_step": failed_step or "",
        "urgent": bool(spec.get("urgent", False)),
    }


def render(template: str, ctx: dict) -> str:
    """Render a ``{var}`` template against ctx, tolerating missing keys."""
    class _Defaulting(dict):
        def __missing__(self, key):
            return f"{{{key}}}"
    return template.format_map(_Defaulting(ctx))


_DEFAULT_TITLE = "automaton: run {run_id} {status}"
_DEFAULT_BODY = (
    "workflow: {workflow}\n"
    "run: {run_id} ({status})\n"
    "trigger: {trigger}\n"
    "failed step: {failed_step}\n"
    "duration: {duration_seconds}s\n"
)


def dispatch_for_run(conn, run_id: int,
                     title_template: Optional[str] = None,
                     body_template: Optional[str] = None,
                     now: Optional[_dt.datetime] = None) -> Optional[dict]:
    """Read env config and fire a notification for ``run_id``.

    Returns the result dict from ``send()``, or None if no channel is
    configured for this run's terminal status.
    """
    ctx = build_run_context(conn, run_id)
    status = ctx["status"]
    if status == "failed":
        urls = _split_urls(os.environ.get(ENV_NOTIFY_ON_FAILURE))
    elif status == "completed":
        urls = _split_urls(os.environ.get(ENV_NOTIFY_ON_SUCCESS))
    elif status == "timed_out":
        urls = _split_urls(os.environ.get(ENV_NOTIFY_ON_TIMEOUT))
    else:
        return None
    if not urls:
        return None
    title = render(title_template or _DEFAULT_TITLE, ctx)
    body = render(body_template or _DEFAULT_BODY, ctx)
    return send(urls, title, body, urgent=ctx["urgent"], now=now)
