"""Inspection UI + JSON API over the automaton state store.

Read routes (require Authorization: Bearer <TOKEN> when AUTOMATON_TOKEN is set,
unless --insecure-read-no-auth is passed):
  GET  /                      list recent runs (HTML)
  GET  /run/<id>              one run (HTML)
  GET  /crons                 cron triggers (HTML)
  GET  /api/runs              list runs
  GET  /api/run/<id>          run detail
  GET  /api/crons             cron triggers
  GET  /api/step_types        list registered step types (built-in + plugins)
  GET  /metrics               Prometheus metrics

Always-open routes (no auth, safe to expose for liveness checks / PWA):
  GET  /healthz               liveness probe
  GET  /manifest.json         PWA manifest
  GET  /sw.js                 PWA service worker

Write routes (require Authorization: Bearer <TOKEN> when AUTOMATON_TOKEN is set):
  POST /api/workflows         body: workflow YAML/JSON spec  -> {workflow_def_id}
  POST /api/trigger/<name>    body: optional {"payload": ...} -> {run_id}
  POST /api/crons             body: {"workflow_name", "cron_expr"} -> {trigger_id}

Agent memory routes (echoes Option C integration):
  GET  /api/agents                     list all agents (name, goal, tick, updated_at)
  GET  /api/agents/<name>/meta         one agent's metadata row
  POST /api/agents/<name>/meta         upsert agent metadata  body: {goal, tick}
  GET  /api/agents/<name>/entries      all memory entries ordered by tick ASC
  POST /api/agents/<name>/entries      append one entry  body: MemoryEntry JSON

Auth model: a single shared bearer token in env var AUTOMATON_TOKEN.
Reads and writes both require it unless their respective --insecure-* flag is
passed. Browser bookmarks can pass the token as ?token=<TOKEN> in the URL —
this is logged at startup as a reminder that it leaks the token into server logs.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import yaml

from . import agents as _agents
from . import auth as _auth
from . import db as _db
from . import engine
from . import mesh as _mesh
from . import metrics as _metrics
from . import scheduler as _scheduler
from . import steps as _steps
from . import webhooks as _webhooks

log = logging.getLogger("automaton.ui")


# ----- HTML rendering -----

# Tailwind via CDN: no build step, semantic markup, responsive
# breakpoints. Combined with the PWA manifest below, the UI installs to
# home screen on iOS Safari and Android Chrome with a working app icon.
TAILWIND_CDN = "https://cdn.tailwindcss.com"

# Status color map - used by both the table and the card layouts.
_STATUS_TEXT = {
    "completed": "text-emerald-600",
    "failed":    "text-rose-600",
    "running":   "text-amber-600 animate-pulse",
    "pending":   "text-slate-500",
    "cancelled": "text-slate-400 line-through",
    "skipped":   "text-slate-400 italic",
}


def _status_pill(status: str) -> str:
    """Inline span used both in tables and cards."""
    cls = _STATUS_TEXT.get(status or "", "text-slate-500")
    return f'<span class="font-medium {cls}">{html.escape(status or "-")}</span>'


def _page(title, body, auto_refresh=None, extra_head=""):
    """Wrap body in a tailwind + PWA shell.

    auto_refresh kept for backward compat but we prefer live updates via
    EventSource where possible (see render_run_detail).
    """
    refresh = (f'<meta http-equiv="refresh" content="{auto_refresh}">'
               if auto_refresh else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0f172a">
  <title>{html.escape(title)}</title>
  <link rel="manifest" href="/manifest.json">
  {refresh}
  <script src="{TAILWIND_CDN}"></script>
  {extra_head}
  <script>
    if ("serviceWorker" in navigator) {{
      navigator.serviceWorker.register("/sw.js").catch(() => {{}});
    }}
  </script>
  {_TS_JS}
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen">
  <div class="max-w-4xl mx-auto px-4 py-4 sm:py-6">
    <nav class="flex items-center gap-4 mb-6 text-sm">
      <a href="/" class="font-semibold text-slate-900 hover:text-blue-700">Runs</a>
      <a href="/workflows" class="text-slate-600 hover:text-blue-700">Workflows</a>
      <a href="/crons" class="text-slate-600 hover:text-blue-700">Cron triggers</a>
      <span class="ml-auto text-xs text-slate-400">automaton</span>
    </nav>
    {body}
  </div>
</body></html>"""


def _status_cell(status):
    s = html.escape(status or "")
    return f'<td class="status-{s}">{s}</td>'


_TS_JS = """<script>
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('time.utc-ts').forEach(function(el) {
    var iso = el.getAttribute('datetime');
    if (!iso) return;
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return;
      var local = d.toLocaleString();
      var utc = el.textContent.trim();
      el.textContent = local + ' (​' + utc + ' UTC)';
    } catch(e) {}
  });
});
</script>"""


def _ts(val) -> str:
    """Render a UTC timestamp as a <time> element browsers can localise.

    The JS in _TS_JS finds .utc-ts elements on DOMContentLoaded and rewrites
    them to show browser-local time with the raw UTC value in parens. SQLite
    stores timestamps as "YYYY-MM-DD HH:MM:SS[.mmm]"; we normalise to ISO 8601
    with a Z suffix so ``new Date()`` parses them correctly.
    """
    if not val:
        return "-"
    s = str(val)
    iso = s.replace(" ", "T", 1)
    if "." in iso:
        iso = iso[:iso.index(".")]
    if not iso.endswith("Z"):
        iso += "Z"
    return f'<time class="utc-ts" datetime="{html.escape(iso)}">{html.escape(s)}</time>'


def render_mesh_card(mesh_info: dict) -> str:
    """Compact Tailscale reachability card for the run-list page.

    Only rendered when Tailscale is fully up (installed + running + logged in).
    When the mesh is not configured, returns an empty string so the page stays
    clean for local-only deployments.
    """
    if not (mesh_info.get("installed") and mesh_info.get("running")
            and mesh_info.get("logged_in")):
        return ""

    ip = html.escape(mesh_info["ips"][0] if mesh_info["ips"] else "")
    magic = html.escape(mesh_info.get("magic_dns") or "")
    tailnet = html.escape(mesh_info.get("tailnet") or "")
    peers = mesh_info.get("peers", 0)
    hostname = html.escape(mesh_info.get("hostname") or "")

    # Build the recommended Tailscale Serve URL (HTTPS via the ts.net cert).
    serve_url = f"https://{magic}" if magic else (f"http://{ip}:8080" if ip else "")

    copy_btn = ""
    if serve_url:
        escaped_url = html.escape(serve_url, quote=True)
        copy_btn = (
            f'<button onclick="navigator.clipboard.writeText(\'{escaped_url}\')" '
            'class="text-xs text-slate-500 hover:text-blue-600 px-1.5 py-0.5 rounded '
            'border border-slate-200 hover:border-blue-300" title="Copy URL">⎘ copy</button>'
        )

    serve_link = (
        f'<a href="{html.escape(serve_url)}" target="_blank" rel="noopener" '
        f'class="font-mono text-xs text-blue-700 hover:underline">{html.escape(serve_url)}</a> '
        + copy_btn
        if serve_url else '<span class="text-slate-400 text-xs">no URL yet</span>'
    )

    detail_parts = []
    if ip:
        detail_parts.append(f'IP <span class="font-mono">{ip}</span>')
    if tailnet:
        detail_parts.append(f'tailnet <span class="font-mono">{tailnet}</span>')
    if hostname:
        detail_parts.append(f'host <span class="font-mono">{hostname}</span>')
    detail_parts.append(f'{peers} peer{"s" if peers != 1 else ""}')
    details = " · ".join(detail_parts)

    return (
        '<div class="mt-6 bg-white border border-slate-200 rounded-lg p-3">'
        '<div class="flex items-center justify-between gap-2 flex-wrap">'
        '<div class="flex items-center gap-2">'
        '<span class="inline-block w-2 h-2 rounded-full bg-emerald-500"></span>'
        '<span class="text-sm font-medium text-slate-700">Connected to Tailscale</span>'
        '</div>'
        f'<div class="flex items-center gap-2">{serve_link}</div>'
        '</div>'
        f'<p class="text-xs text-slate-400 mt-1">{details}</p>'
        '</div>'
    )


def render_run_list(conn, *, status=None, workflow=None, after=None, before=None,
                    mesh_info: Optional[dict] = None):
    filtering = any(x is not None for x in (status, workflow, after, before))
    if filtering:
        runs = engine.search_runs(
            conn,
            status=status or None,
            workflow=workflow or None,
            after=after or None,
            before=before or None,
            limit=100,
        )
    else:
        runs = engine.list_runs(conn, limit=50)

    statuses = ["", "pending", "running", "completed", "failed", "timed_out", "cancelled"]
    status_opts = "".join(
        f'<option value="{s}" {"selected" if s == (status or "") else ""}>{s or "all"}</option>'
        for s in statuses
    )
    wf_val = html.escape(workflow or "")
    after_val = html.escape(after or "")
    before_val = html.escape(before or "")
    filter_bar = (
        '<form method="get" action="/" class="flex flex-wrap gap-2 mb-4 items-end">'
        '<div class="flex flex-col gap-0.5"><label class="text-xs text-slate-500">Status</label>'
        f'<select name="status" class="border border-slate-300 rounded px-2 py-1 text-sm">' + status_opts + '</select></div>'
        '<div class="flex flex-col gap-0.5"><label class="text-xs text-slate-500">Workflow</label>'
        f'<input name="workflow" value="{wf_val}" placeholder="name\u2026" class="border border-slate-300 rounded px-2 py-1 text-sm w-40"></div>'
        '<div class="flex flex-col gap-0.5"><label class="text-xs text-slate-500">After (UTC)</label>'
        f'<input name="after" type="datetime-local" value="{after_val}" class="border border-slate-300 rounded px-2 py-1 text-sm"></div>'
        '<div class="flex flex-col gap-0.5"><label class="text-xs text-slate-500">Before (UTC)</label>'
        f'<input name="before" type="datetime-local" value="{before_val}" class="border border-slate-300 rounded px-2 py-1 text-sm"></div>'
        '<button type="submit" class="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Filter</button>'
        '<a href="/" class="text-sm text-slate-500 hover:underline self-end pb-0.5">Clear</a>'
        '</form>'
    )

    if not runs:
        return _page("automaton - runs",
                     '<h1 class="text-xl font-semibold mb-4">Runs</h1>' + filter_bar +
                     '<p class="text-slate-500">No matching runs.</p>' +
                     render_mesh_card(mesh_info or {}),
                     auto_refresh=0 if filtering else 5)

    # Desktop: table. Mobile: card list. Same data twice, with the
    # complementary visibility classes; cheap enough not to bother with
    # a JS renderer.
    table_rows = []
    cards = []
    for r in runs:
        rid = r["id"]
        wf = html.escape(r["workflow"])
        started = html.escape(str(r["started_at"] or ""))
        finished = html.escape(str(r["finished_at"] or "-"))
        status = r["status"]
        table_rows.append(
            f'<tr class="border-b border-slate-200 hover:bg-slate-50">'
            f'<td class="py-2 px-3"><a class="text-blue-700 hover:underline" href="/run/{rid}">{rid}</a></td>'
            f'<td class="py-2 px-3">{wf}</td>'
            f'<td class="py-2 px-3">{_status_pill(status)}</td>'
            f'<td class="py-2 px-3 text-xs text-slate-500">{started}</td>'
            f'<td class="py-2 px-3 text-xs text-slate-500">{finished}</td>'
            '</tr>'
        )
        cards.append(
            f'<a href="/run/{rid}" class="block bg-white border border-slate-200 rounded-lg p-3 hover:border-blue-400">'
            f'<div class="flex items-baseline justify-between">'
            f'<span class="font-semibold text-slate-900">#{rid} <span class="text-slate-500">{wf}</span></span>'
            f'{_status_pill(status)}</div>'
            f'<div class="text-xs text-slate-500 mt-1">{started}</div>'
            '</a>'
        )

    body = (
        '<h1 class="text-xl font-semibold mb-4">Runs</h1>'
        + filter_bar
        + '<div class="flex flex-col gap-2 sm:hidden">' + "".join(cards) + '</div>'
        + '<div class="hidden sm:block overflow-x-auto">'
        + '<table class="w-full text-sm bg-white border border-slate-200 rounded-lg overflow-hidden">'
        + '<thead class="bg-slate-100 text-slate-600 text-xs uppercase">'
        + '<tr><th class="text-left py-2 px-3">#</th><th class="text-left py-2 px-3">Workflow</th>'
        + '<th class="text-left py-2 px-3">Status</th>'
        + '<th class="text-left py-2 px-3">Started</th>'
        + '<th class="text-left py-2 px-3">Finished</th></tr></thead>'
        + f'<tbody>{"".join(table_rows)}</tbody></table></div>'
        + '<p class="text-xs text-slate-400 mt-4">' + (f'{len(runs)} run(s) found.' if filtering else 'Auto-refreshes every 5 seconds.') + '</p>'
        + render_mesh_card(mesh_info or {})
    )
    return _page("automaton - runs", body, auto_refresh=0 if filtering else 5)


def _render_step_output(out, err):
    """Render step output+error as HTML. Detects step type from output keys."""
    import json as _json_rso
    blocks = []

    def _pre(label, text, cls="bg-slate-50"):
        if not text:
            return ""
        label_html = f'<span class="text-xs font-medium text-slate-500 mb-0.5 block">{html.escape(label)}</span>'
        return (f'<div class="mt-2">{label_html}'
                f'<pre class="text-xs {cls} rounded p-2 overflow-x-auto whitespace-pre-wrap break-words">'
                + html.escape(str(text)) + '</pre></div>')

    if out is None and err is None:
        return ""

    if isinstance(err, dict) and err.get("message"):
        blocks.append(_pre("Error", err.get("message", ""), "bg-red-50"))
        if err.get("detail"):
            blocks.append(_pre("Detail", _json_rso.dumps(err["detail"], indent=2), "bg-red-50"))
        return "".join(blocks)

    if isinstance(out, dict):
        if "returncode" in out:
            rc = out.get("returncode", "?")
            rc_cls = "bg-green-100 text-green-800" if rc == 0 else "bg-red-100 text-red-800"
            blocks.append(f'<span class="inline-block mt-2 text-xs font-mono px-2 py-0.5 rounded {rc_cls}">exit {rc}</span>')
            blocks.append(_pre("stdout", out.get("stdout", "")))
            blocks.append(_pre("stderr", out.get("stderr", ""), "bg-yellow-50"))
        elif "status_code" in out:
            code = out.get("status_code", "?")
            ok = isinstance(code, int) and 200 <= code < 300
            cc = "bg-green-100 text-green-800" if ok else "bg-red-100 text-red-800"
            blocks.append(f'<span class="inline-block mt-2 text-xs font-mono px-2 py-0.5 rounded {cc}">HTTP {code}</span>')
            blocks.append(_pre("body", out.get("body", "") or out.get("text", "")))
        elif "return_value" in out:
            blocks.append(_pre("stdout", out.get("stdout", "")))
            blocks.append(_pre("stderr", out.get("stderr", ""), "bg-yellow-50"))
            rv = out.get("return_value")
            if rv is not None:
                rv_str = _json_rso.dumps(rv, indent=2) if not isinstance(rv, str) else rv
                blocks.append(_pre("return value", rv_str, "bg-blue-50"))
        elif "appended" in out:
            flag = out.get("appended", False)
            lbl_cls = "bg-green-100 text-green-800" if flag else "bg-slate-100 text-slate-600"
            blocks.append(f'<span class="inline-block mt-2 text-xs px-2 py-0.5 rounded {lbl_cls}">'
                          + ("written" if flag else "no-op") + '</span>')
        else:
            blocks.append(_pre("output", _json_rso.dumps(out, indent=2)))
    elif out is not None:
        blocks.append(_pre("output", str(out)))

    if isinstance(err, str) and err:
        blocks.append(_pre("error", err, "bg-red-50"))

    return "".join(blocks)


def _rerun_button(run):
    """HTML Re-run button for terminal runs."""
    import json as _json_rb
    wf = html.escape(run["workflow"])
    rid = run["id"]
    payload_js = html.escape(_json_rb.dumps(run.get("trigger_payload") or {}), quote=True)
    return (
        f'<button onclick="rerunRun({rid},\'{wf}\',\'{payload_js}\')" '
        'class="text-sm bg-slate-100 hover:bg-slate-200 border border-slate-300 rounded px-3 py-1">&#x21ba; Re-run</button>'
        '<script>'
        'function rerunRun(rid,wf,ps){'
        'var p={};try{p=JSON.parse(ps);}catch(e){}'
        'var t=(new URLSearchParams(window.location.search)).get("token")||"";'
        'fetch("/api/trigger/"+wf,{method:"POST",'
        'headers:{"Content-Type":"application/json","Authorization":"Bearer "+t},'
        'body:JSON.stringify({payload:p})})'
        '.then(r=>r.json()).then(d=>{'
        'if(d.run_id)window.location.href="/run/"+d.run_id+window.location.search;'
        'else alert(JSON.stringify(d));}).catch(e=>alert(e));}'
        '</script>'
    )


def render_workflows(conn):
    """Registered workflow definitions + inline YAML editor."""
    workflows = engine.list_workflows(conn)
    cards = []
    for wf in workflows:
        name = html.escape(wf["name"])
        spec = wf.get("spec") or {}
        steps = spec.get("steps") or []
        step_names = ", ".join(html.escape(s["name"]) for s in steps[:5])
        if len(steps) > 5:
            step_names += f" +{len(steps) - 5} more"
        ver = wf["version"]
        cards.append(
            '<div class="bg-white border border-slate-200 rounded-lg p-3 flex flex-col gap-2">'
            '<div class="flex items-baseline justify-between gap-2">'
            f'<span class="font-semibold">{name}</span>'
            f'<span class="text-xs text-slate-400">v{ver}</span>'
            '</div>'
            f'<p class="text-xs text-slate-500">{len(steps)} step{"s" if len(steps) != 1 else ""}'
            + (f': {step_names}' if step_names else '') +
            '</p>'
            f'<button onclick="triggerWorkflow(\'{name}\')" '
            'class="self-start text-xs bg-blue-600 text-white px-2 py-1 rounded hover:bg-blue-700">'
            '&#9654; Trigger</button>'
            '</div>'
        )

    editor = (
        '<div class="bg-white border border-slate-200 rounded-lg p-4 mt-6">'
        '<h2 class="text-sm font-semibold mb-2">Register / update workflow</h2>'
        '<textarea id="wf-editor" rows="14" '
        'class="w-full font-mono text-xs border border-slate-300 rounded p-2" '
        'placeholder="Paste YAML workflow spec here\u2026"></textarea>'
        '<div class="flex gap-2 mt-2">'
        '<button onclick="registerWorkflow()" '
        'class="text-sm bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700">Register</button>'
        '<span id="wf-msg" class="text-xs self-center text-slate-500"></span>'
        '</div></div>'
        '<script>'
        'var _tok=(new URLSearchParams(window.location.search)).get("token")||"";'
        'function registerWorkflow(){'
        'var txt=document.getElementById("wf-editor").value;'
        'var msg=document.getElementById("wf-msg");'
        'fetch("/api/workflows",{method:"POST",'
        'headers:{"Content-Type":"application/yaml","Authorization":"Bearer "+_tok},'
        'body:txt}).then(r=>r.json()).then(d=>{'
        'msg.textContent=d.error||("Registered "+d.name+" (id "+d.workflow_def_id+")");'
        'if(!d.error)setTimeout(()=>location.reload(),800);'
        '}).catch(e=>{msg.textContent=String(e);});}'
        'function triggerWorkflow(n){'
        'fetch("/api/trigger/"+n,{method:"POST",'
        'headers:{"Content-Type":"application/json","Authorization":"Bearer "+_tok},'
        'body:JSON.stringify({})}).then(r=>r.json()).then(d=>{'
        'if(d.run_id)window.location.href="/run/"+d.run_id+(window.location.search||"");'
        'else alert(JSON.stringify(d));}).catch(e=>alert(e));}'
        '</script>'
    )

    body = (
        '<h1 class="text-xl font-semibold mb-4">Workflows</h1>'
        + ('<div class="grid sm:grid-cols-2 gap-3 mb-2">' + "".join(cards) + '</div>' if cards
           else '<p class="text-slate-500 mb-2">No workflows registered yet.</p>')
        + editor
    )
    return _page("automaton - workflows", body)


def render_run_detail(conn, run_id):
    try:
        d = engine.run_detail(conn, run_id)
    except KeyError:
        body = f'<h1 class="text-xl font-semibold">No run {run_id}</h1>'
        return _page("not found", body), 404
    run = d["run"]

    step_rows = []
    for s in d["steps"]:
        out = s.get("output")
        err = s.get("error")
        body_pre = _render_step_output(out, err)
        step_rows.append(
            '<div class="bg-white border border-slate-200 rounded-lg p-3">'
            '<div class="flex items-baseline justify-between gap-2">'
            f'<span class="font-semibold">{html.escape(s["name"])} '
            f'<span class="text-xs text-slate-500 font-normal">attempt {s["attempt"]}</span></span>'
            f'{_status_pill(s["status"])}</div>'
            f'<div class="text-xs text-slate-500 mt-1">{_ts(s["started_at"])} → {_ts(s["finished_at"])}</div>'
            f'{body_pre}'
            '</div>'
        )

    event_rows = []
    for e in d["events"]:
        event_rows.append(
            '<tr class="border-b border-slate-100">'
            f'<td class="py-1 px-2 text-xs text-slate-400">{e["id"]}</td>'
            f'<td class="py-1 px-2 text-xs text-slate-500 whitespace-nowrap">{_ts(e["ts"])}</td>'
            f'<td class="py-1 px-2 text-xs font-medium">{html.escape(e["kind"])}</td>'
            f'<td class="py-1 px-2 text-xs"><code class="text-xs">{html.escape(e.get("payload_json") or "")}</code></td>'
            '</tr>'
        )

    # Inline EventSource: live-update the status pill while pending or
    # running; close the connection once we reach a terminal state.
    live_js = ""
    if run["status"] in ("running", "pending"):
        live_js = f"""<script>
(function() {{
  var es = new EventSource("/api/run/{run["id"]}/events");
  var pill = document.getElementById("run-status");
  es.onmessage = function(ev) {{
    try {{
      var data = JSON.parse(ev.data);
      if (data.status && pill) {{ pill.textContent = data.status; }}
      if (data.status && ["completed","failed","cancelled"].indexOf(data.status) >= 0) {{
        es.close();
        // Reload once so steps + events refresh; future improvement
        // would diff in-place but a one-shot reload is fine here.
        setTimeout(function() {{ window.location.reload(); }}, 250);
      }}
    }} catch (e) {{ /* ignore */ }}
  }};
  es.onerror = function() {{ es.close(); }};
}})();
</script>"""

    body = (
        f'<div class="flex items-baseline justify-between gap-4 flex-wrap mb-4">'
        f'<h1 class="text-xl font-semibold">Run {run["id"]}'
        f' <span class="text-base text-slate-500 font-normal">{html.escape(run["workflow"])} v{run["version"]}</span></h1>'
        f'<div class="flex items-center gap-3">'
        f'<div>Status: <strong id="run-status" class="{_STATUS_TEXT.get(run["status"], "")}">{run["status"]}</strong></div>'
        + (_rerun_button(run) if run["status"] in ("completed", "failed", "timed_out", "cancelled") else "")
        + '</div>'
        '</div>'

        '<h2 class="text-sm uppercase tracking-wide text-slate-500 mb-2">Steps</h2>'
        '<div class="flex flex-col gap-2 mb-6">' + "".join(step_rows) + '</div>'

        '<h2 class="text-sm uppercase tracking-wide text-slate-500 mb-2">Event log</h2>'
        '<div class="overflow-x-auto bg-white border border-slate-200 rounded-lg">'
        '<table class="w-full text-sm">'
        '<thead class="bg-slate-100 text-xs text-slate-600 uppercase">'
        '<tr><th class="text-left py-1 px-2">#</th><th class="text-left py-1 px-2">Time</th>'
        '<th class="text-left py-1 px-2">Kind</th><th class="text-left py-1 px-2">Payload</th></tr>'
        '</thead><tbody>' + "".join(event_rows) + '</tbody></table></div>'

        + live_js
    )
    return _page(f"run {run_id}", body), 200


def render_crons(conn):
    rows = _scheduler.list_crons(conn)
    if not rows:
        body = ('<h1 class="text-xl font-semibold mb-4">Cron triggers</h1>'
                '<p class="text-slate-500">None registered.</p>')
        return _page("automaton - crons", body)

    cards = []
    table_rows = []
    for r in rows:
        enabled = r["enabled"]
        on_pill = ('<span class="text-emerald-600 text-xs font-medium">on</span>'
                   if enabled else
                   '<span class="text-slate-400 text-xs">off</span>')
        tz = html.escape(r.get("timezone") or "UTC")
        expr = html.escape(r["cron_expr"])
        wf = html.escape(r["workflow_name"])
        cards.append(
            '<div class="bg-white border border-slate-200 rounded-lg p-3">'
            f'<div class="flex items-baseline justify-between"><span class="font-semibold">#{r["id"]} {wf}</span>{on_pill}</div>'
            f'<code class="text-xs bg-slate-50 px-1 rounded">{expr}</code> <span class="text-xs text-slate-500">tz={tz}</span>'
            f'<div class="text-xs text-slate-500 mt-1">next: {_ts(r["next_fire_at"])}</div>'
            f'<div class="text-xs text-slate-500">last: {_ts(r.get("last_fire_at"))}</div>'
            '</div>'
        )
        table_rows.append(
            f'<tr class="border-b border-slate-200"><td class="py-2 px-3">{r["id"]}</td>'
            f'<td class="py-2 px-3">{wf}</td>'
            f'<td class="py-2 px-3"><code class="text-xs bg-slate-50 px-1 rounded">{expr}</code></td>'
            f'<td class="py-2 px-3 text-xs">{tz}</td>'
            f'<td class="py-2 px-3 text-xs text-slate-500">{_ts(r["next_fire_at"])}</td>'
            f'<td class="py-2 px-3 text-xs text-slate-500">{_ts(r.get("last_fire_at"))}</td>'
            f'<td class="py-2 px-3">{on_pill}</td></tr>'
        )

    body = (
        '<h1 class="text-xl font-semibold mb-4">Cron triggers</h1>'
        '<div class="flex flex-col gap-2 sm:hidden">' + "".join(cards) + '</div>'
        '<div class="hidden sm:block overflow-x-auto">'
        '<table class="w-full text-sm bg-white border border-slate-200 rounded-lg overflow-hidden">'
        '<thead class="bg-slate-100 text-slate-600 text-xs uppercase">'
        '<tr><th class="text-left py-2 px-3">#</th><th class="text-left py-2 px-3">Workflow</th>'
        '<th class="text-left py-2 px-3">Expression</th><th class="text-left py-2 px-3">TZ</th>'
        '<th class="text-left py-2 px-3">Next fire</th><th class="text-left py-2 px-3">Last fire</th>'
        '<th class="text-left py-2 px-3">On</th></tr></thead>'
        f'<tbody>{"".join(table_rows)}</tbody></table></div>'
    )
    return _page("automaton - crons", body, auto_refresh=5)


# ----- PWA: manifest + minimal service worker -----

def render_manifest_json() -> str:
    """Minimal Web App Manifest. Lets iOS Safari / Android Chrome
    install the UI to home screen with a meaningful icon + title."""
    return json.dumps({
        "name": "automaton",
        "short_name": "automaton",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "icons": [
            # Inline SVG icon; browsers accept it as an app icon and we
            # avoid shipping a binary asset.
            {
                "src": "data:image/svg+xml;utf8,"
                       "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
                       "<rect width='64' height='64' rx='12' fill='%230f172a'/>"
                       "<text x='32' y='42' text-anchor='middle' font-family='monospace' "
                       "font-size='28' fill='%2360a5fa'>a</text></svg>",
                "sizes": "any", "type": "image/svg+xml",
            }
        ],
    })


def render_service_worker() -> str:
    """One-page service worker: caches the runs list so the home
    screen icon opens to something even when offline. Deliberately
    tiny - if anything goes wrong, the page still works without us."""
    return """
const CACHE = "automaton-v1";
const SHELL = ["/"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  // Network-first for everything except the runs list, which we serve
  // from cache as a fallback so the app icon opens even offline.
  if (u.pathname === "/") {
    e.respondWith(
      fetch(e.request)
        .then((r) => { caches.open(CACHE).then((c) => c.put(e.request, r.clone())); return r; })
        .catch(() => caches.match(e.request))
    );
  }
});
""".lstrip()


_RUN_RE = re.compile(r"^/run/(\d+)/?$")
_API_RUN_RE = re.compile(r"^/api/run/(\d+)/?$")
_API_RUN_EVENTS_RE = re.compile(r"^/api/run/(\d+)/events/?$")
_API_CANCEL_RE = re.compile(r"^/api/run/(\d+)/cancel/?$")
_API_TRIGGER_RE = re.compile(r"^/api/trigger/([A-Za-z0-9_.-]+)/?$")
_API_SIGNAL_RE = re.compile(r"^/api/signals/(\d+)/([A-Za-z0-9_.-]+)/?$")
_WEBHOOK_RE = re.compile(r"^/webhook/([A-Za-z0-9_.-]+)/?$")
_API_AGENT_META_RE = re.compile(r"^/api/agents/([A-Za-z0-9_.-]+)/meta/?$")
_API_AGENT_ENTRIES_RE = re.compile(r"^/api/agents/([A-Za-z0-9_.-]+)/entries/?$")


def make_handler(db_path: str, auth_token: Optional[str], require_auth: bool,
                 tls_enabled: bool = False,
                 require_read_auth: bool = True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.command, self.path)

        # --- helpers ---
        def _send(self, status, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if tls_enabled:
                # 180 days. Conservative for personal infra - browsers cache
                # this aggressively. includeSubDomains intentionally omitted.
                self.send_header(
                    "Strict-Transport-Security", "max-age=15552000"
                )
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, default=str), "application/json")

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _get_role(self) -> Optional[str]:
            """
            Resolve the caller's role from the request, or return ``None``
            if the request is unauthenticated / carries an invalid token.

            Resolution order:
              1. ``insecure_no_auth`` flag → "admin" (dev mode, no token needed)
              2. ``Authorization: Bearer <token>`` header
                 a. Matches ``AUTOMATON_TOKEN`` env var → "admin"  (back-compat)
                 b. Matches an active row in ``api_keys``  → that row's role
              3. GET-only: ``?token=<token>`` query-string fallback (same checks)
              4. Otherwise → None (caller should return 401)
            """
            if not require_auth:
                return "admin"

            raw = None
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer "):
                raw = got[len("Bearer "):]
            elif self.command == "GET":
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                raw = (qs.get("token") or [""])[0] or None

            if not raw:
                return None

            # AUTOMATON_TOKEN always resolves to admin (backward compat).
            if auth_token and raw == auth_token:
                return "admin"

            # DB-stored API keys.
            try:
                conn = _db.connect(db_path)
                row = _auth.authenticate(conn, raw)
                if row:
                    _auth.touch_last_used(conn, row["id"])
                    return row["role"]
            except Exception:
                pass
            return None

        def _check_auth(self) -> bool:
            """Legacy helper — True when caller has operator-or-better role."""
            role = self._get_role()
            return role is not None and _auth.role_can_write(role)

        def _check_read_auth(self) -> bool:
            """Auth check for GET routes. Skipped when require_read_auth is False."""
            if not require_read_auth:
                return True
            role = self._get_role()
            return role is not None and _auth.role_can_read(role)

        # --- routing ---
        def _handle_webhook(self, name):
            body_bytes = self._read_body()
            conn = _db.connect(db_path)
            try:
                endpoint = _webhooks.get_endpoint(conn, name)
                if endpoint is None:
                    self._json(404, {"error": f"no webhook endpoint {name!r}"})
                    return
                header_value = self.headers.get(endpoint["signature_header"]) or ""
                try:
                    _webhooks.verify_signature(endpoint, body_bytes, header_value)
                except _webhooks.WebhookError as e:
                    self._json(e.status_code, {"error": str(e)})
                    return
                # Parse the body as JSON (best effort) so the workflow sees structured data.
                payload = None
                if body_bytes:
                    try:
                        payload = json.loads(body_bytes.decode("utf-8"))
                    except Exception:
                        payload = {"raw_body": body_bytes.decode("utf-8", errors="replace")}
                try:
                    run_id = engine.trigger_run(
                        conn, endpoint["workflow_name"],
                        trigger_kind="webhook",
                        trigger_payload={"endpoint": name, "body": payload},
                    )
                except KeyError:
                    self._json(500, {"error":
                        f"webhook endpoint {name!r} points to unknown workflow "
                        f"{endpoint['workflow_name']!r}"})
                    return
                self._json(202, {"run_id": run_id, "endpoint": name})
            finally:
                conn.close()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            # Always-open routes: liveness probe and PWA assets must not
            # require auth so that health-checkers and the service-worker
            # install path work without a token.
            _OPEN_PATHS = {"/healthz", "/health", "/manifest.json", "/sw.js"}
            if path not in _OPEN_PATHS and not self._check_read_auth():
                self._json(401, {"error": "unauthorized"})
                return
            conn = _db.connect(db_path)
            try:
                if path in ("/", "/runs", "/runs/"):
                    from urllib.parse import urlparse as _up2, parse_qs as _pqs2
                    _qs2 = _pqs2(_up2(self.path).query, keep_blank_values=False)
                    def _first2(k): v = _qs2.get(k); return v[0] if v else None
                    self._send(200, render_run_list(
                        conn,
                        status=_first2("status"),
                        workflow=_first2("workflow"),
                        after=_first2("after"),
                        before=_first2("before"),
                        mesh_info=_mesh.cached_status(),
                    )); return
                if path in ("/workflows", "/workflows/"):
                    self._send(200, render_workflows(conn)); return
                m = _RUN_RE.match(path)
                if m:
                    body, st = render_run_detail(conn, int(m.group(1)))
                    self._send(st, body); return
                if path in ("/crons", "/crons/"):
                    self._send(200, render_crons(conn)); return
                if path == "/api/runs":
                    self._json(200, engine.list_runs(conn)); return
                m = _API_RUN_RE.match(path)
                if m:
                    try:
                        self._json(200, engine.run_detail(conn, int(m.group(1))))
                    except KeyError:
                        self._json(404, {"error": "not found"})
                    return
                m = _API_SIGNAL_RE.match(path)
                if m:
                    rid, sname = int(m.group(1)), m.group(2)
                    payload = (body or {}).get("payload") if isinstance(body, dict) else None
                    try:
                        sid = engine.send_signal(conn, rid, sname, payload)
                    except sqlite3.IntegrityError:
                        self._json(404, {"error": f"no run {rid}"})
                        return
                    self._json(201, {"signal_id": sid})
                    return
                if path == "/api/crons":
                    self._json(200, _scheduler.list_crons(conn)); return
                if path == "/api/signals":
                    rows = conn.execute(
                        "SELECT id, run_id, name, payload_json, sent_at, consumed_at "
                        "FROM signal ORDER BY id DESC LIMIT 50"
                    ).fetchall()
                    self._json(200, [dict(r) for r in rows]); return
                if path == "/api/webhooks":
                    self._json(200, _webhooks.list_webhooks(conn)); return
                if path == "/api/step_types":
                    self._json(200, {"types": _steps.registered_types()}); return
                if path in ("/healthz", "/health"):
                    self._json(200, {"ok": True}); return
                if path == "/metrics":
                    payload = _metrics.collect(conn, db_path)
                    self._send(200, payload,
                               content_type=_metrics.CONTENT_TYPE)
                    return
                if path == "/manifest.json":
                    self._send(200, render_manifest_json(),
                               content_type="application/manifest+json")
                    return
                if path == "/sw.js":
                    self._send(200, render_service_worker(),
                               content_type="application/javascript")
                    return
                m = _API_RUN_EVENTS_RE.match(path)
                if m:
                    self._stream_run_events(int(m.group(1)))
                    return
                # --- agent memory routes (read) ---
                if path == "/api/agents":
                    self._json(200, _agents.list_agents(conn)); return
                m = _API_AGENT_META_RE.match(path)
                if m:
                    agent = _agents.get_agent(conn, m.group(1))
                    if agent is None:
                        self._json(404, {"error": "agent not found"})
                    else:
                        self._json(200, agent)
                    return
                m = _API_AGENT_ENTRIES_RE.match(path)
                if m:
                    entries = _agents.get_entries(conn, m.group(1))
                    self._json(200, {"entries": entries, "count": len(entries)}); return
                # --- API key list (admin only) ---
                if path == "/api/keys":
                    get_role = self._get_role()
                    if get_role is None:
                        self._json(401, {"error": "unauthorized"}); return
                    if not _auth.role_is_admin(get_role):
                        self._json(403, {"error": "admin role required"}); return
                    keys = _auth.list_api_keys(conn)
                    self._json(200, {"keys": keys, "count": len(keys)}); return
                self._send(404, _page("not found", '<h1 class="text-xl font-semibold">404</h1>'))
            finally:
                conn.close()

        def _stream_run_events(self, run_id: int):
            """Server-Sent Events stream of run status changes.

            Holds the response open for up to 60 seconds, polling the
            DB every 500 ms; emits a frame whenever the run's status
            string changes (or, every 10s, a heartbeat to keep proxies
            from cutting the idle connection). Closes itself on
            terminal status so the browser doesn't reconnect needlessly.

            Python's stdlib http.server is one-thread-per-request via
            BaseHTTPServer, which is fine for a personal-infra UI but
            don't put many tabs on a single connection.
            """
            import time
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            if tls_enabled:
                self.send_header("Strict-Transport-Security",
                                 "max-age=15552000")
            self.end_headers()

            stream_conn = _db.connect(db_path)
            try:
                last_status = None
                start = time.monotonic()
                last_heartbeat = start
                while time.monotonic() - start < 60:
                    row = stream_conn.execute(
                        "SELECT status FROM run WHERE id = ?", (run_id,)
                    ).fetchone()
                    if row is None:
                        # Unknown run id; emit a one-shot frame and bail.
                        self.wfile.write(b'data: {"error": "not found"}\n\n')
                        self.wfile.flush()
                        return
                    status = row["status"]
                    if status != last_status:
                        payload = json.dumps({"status": status,
                                               "run_id": run_id})
                        self.wfile.write(
                            f"data: {payload}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                        last_status = status
                        last_heartbeat = time.monotonic()
                        if status in ("completed", "failed", "cancelled"):
                            return
                    elif time.monotonic() - last_heartbeat > 10:
                        # SSE heartbeat comment - ignored by EventSource
                        # but keeps any proxy in the middle from
                        # treating the connection as idle and closing it.
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                # Client navigated away or closed the page.
                pass
            finally:
                stream_conn.close()


        def do_POST(self):
            path = self.path.split("?", 1)[0]
            # Webhook routes have their own per-endpoint signature auth.
            m = _WEBHOOK_RE.match(path)
            if m:
                self._handle_webhook(m.group(1))
                return
            _post_role = self._get_role()
            if _post_role is None:
                self._json(401, {"error": "unauthorized"})
                return
            if not _auth.role_can_write(_post_role):
                self._json(403, {"error": "forbidden: insufficient role"})
                return
            path = self.path.split("?", 1)[0]
            try:
                body_bytes = self._read_body()
                body = yaml.safe_load(body_bytes.decode("utf-8")) if body_bytes else None
            except Exception as e:
                self._json(400, {"error": f"invalid body: {e}"})
                return

            conn = _db.connect(db_path)
            try:
                if path == "/api/workflows":
                    if not isinstance(body, dict) or "name" not in body or "steps" not in body:
                        self._json(400, {"error": "body must be a workflow spec with 'name' and 'steps'"})
                        return
                    wid = engine.register_workflow(conn, body)
                    self._json(201, {"workflow_def_id": wid, "name": body["name"]})
                    return
                m = _API_CANCEL_RE.match(path)
                if m:
                    run_id = int(m.group(1))
                    reason = None
                    if isinstance(body, dict):
                        reason = body.get("reason")
                    ok = engine.cancel_run(conn, run_id, reason=reason)
                    if ok:
                        self._json(200, {"cancelled": True, "run_id": run_id})
                    else:
                        self._json(404, {"error": "run not found or already terminal"})
                    return
                m = _API_TRIGGER_RE.match(path)
                if m:
                    name = m.group(1)
                    payload = (body or {}).get("payload") if isinstance(body, dict) else None
                    trigger_kind = (body or {}).get("trigger_kind", "api") if isinstance(body, dict) else "api"
                    try:
                        run_id = engine.trigger_run(conn, name, trigger_kind, payload)
                    except KeyError:
                        self._json(404, {"error": f"no workflow {name!r}"})
                        return
                    self._json(202, {"run_id": run_id})
                    return
                m = _API_SIGNAL_RE.match(path)
                if m:
                    rid, sname = int(m.group(1)), m.group(2)
                    payload = (body or {}).get("payload") if isinstance(body, dict) else None
                    try:
                        sid = engine.send_signal(conn, rid, sname, payload)
                    except sqlite3.IntegrityError:
                        self._json(404, {"error": f"no run {rid}"})
                        return
                    self._json(201, {"signal_id": sid})
                    return
                if path == "/api/crons":
                    if not isinstance(body, dict) or "workflow_name" not in body or "cron_expr" not in body:
                        self._json(400, {"error": "body must include workflow_name and cron_expr"})
                        return
                    try:
                        tid = _scheduler.register_cron(conn, body["workflow_name"], body["cron_expr"])
                    except ValueError as e:
                        self._json(400, {"error": str(e)})
                        return
                    self._json(201, {"trigger_id": tid})
                    return
                # --- agent memory routes (write) ---
                m = _API_AGENT_META_RE.match(path)
                if m:
                    agent_name = m.group(1)
                    if not isinstance(body, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    goal = body.get("goal", "")
                    tick = int(body.get("tick", 0))
                    row = _agents.upsert_agent(conn, agent_name, goal, tick)
                    self._json(200, row)
                    return
                m = _API_AGENT_ENTRIES_RE.match(path)
                if m:
                    agent_name = m.group(1)
                    if not isinstance(body, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    tick = body.get("tick")
                    if tick is None:
                        self._json(400, {"error": "body must include 'tick'"})
                        return
                    tick = int(tick)
                    # Ensure the agent row exists; only create if it doesn't
                    # already exist so we never clobber an existing goal.
                    if _agents.get_agent(conn, agent_name) is None:
                        _agents.upsert_agent(conn, agent_name,
                                             body.get("goal", ""), tick)
                    try:
                        import sqlite3 as _sq3
                        row_id = _agents.append_entry(conn, agent_name, tick, body)
                    except _sq3.IntegrityError:
                        self._json(409, {"error": f"tick {tick} already exists for agent {agent_name!r}"})
                        return
                    self._json(201, {"id": row_id, "agent_name": agent_name, "tick": tick})
                    return
                # --- API key management (admin only) ---
                if path == "/api/keys":
                    role = self._get_role()
                    if role is None:
                        self._json(401, {"error": "unauthorized"})
                        return
                    if not _auth.role_is_admin(role):
                        self._json(403, {"error": "admin role required"})
                        return
                    if not isinstance(body, dict):
                        self._json(400, {"error": "body must be a JSON object"})
                        return
                    name = (body.get("name") or "").strip()
                    key_role = body.get("role", "")
                    if not name:
                        self._json(400, {"error": "body must include 'name'"})
                        return
                    if key_role not in _auth.ROLES:
                        self._json(400, {"error": f"role must be one of {list(_auth.ROLES)}"})
                        return
                    try:
                        key_id, raw_key = _auth.create_api_key(conn, name, key_role)
                    except ValueError as e:
                        self._json(400, {"error": str(e)})
                        return
                    except Exception as e:
                        if "UNIQUE" in str(e):
                            self._json(409, {"error": f"key name {name!r} already exists"})
                        else:
                            self._json(500, {"error": str(e)})
                        return
                    self._json(201, {
                        "id": key_id, "name": name, "role": key_role,
                        "key": raw_key,
                        "note": "Store this key — it will not be shown again.",
                    })
                    return

                self._json(404, {"error": "not found"})
            finally:
                conn.close()

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            role = self._get_role()
            if role is None:
                self._json(401, {"error": "unauthorized"})
                return
            if not _auth.role_is_admin(role):
                self._json(403, {"error": "admin role required"})
                return
            conn = _db.connect(db_path)
            try:
                import re as _re
                m = _re.match(r"^/api/keys/([^/]+)/?$", path)
                if m:
                    name_or_id = m.group(1)
                    revoked = _auth.revoke_api_key(conn, name_or_id)
                    if revoked:
                        self._json(200, {"revoked": True, "key": name_or_id})
                    else:
                        self._json(404, {"error": f"key {name_or_id!r} not found or already revoked"})
                    return
                self._json(404, {"error": "not found"})
            finally:
                conn.close()

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080,
          auth_token=None,
          insecure_no_auth: bool = False,
          insecure_read_no_auth: bool = False,
          tls_cert=None,
          tls_key=None):
    """Boot the http server. Returns the server object (call serve_forever()).

    auth_token defaults to env var AUTOMATON_TOKEN.
    If neither is set and insecure_no_auth is False, all routes return 401.

    insecure_no_auth=True  -- disables auth on write AND read routes.
    insecure_read_no_auth=True -- disables auth on read routes only; writes
                                  still require the token. Useful for local
                                  Prometheus scrapers that cannot send headers.

                                  token while keeping the write API protected.

    Pass tls_cert and tls_key together to wrap the listening socket in TLS.
    Either both or neither - one without the other raises ValueError. When
    TLS is on, responses include a Strict-Transport-Security header.
    """
    if auth_token is None:
        auth_token = os.environ.get("AUTOMATON_TOKEN")
    require_auth = not insecure_no_auth
    # require_read_auth is False when either --insecure-no-auth or
    # --insecure-read-no-auth is passed.
    require_read_auth = require_auth and not insecure_read_no_auth
    if require_auth and not auth_token:
        log.warning(
            "no AUTOMATON_TOKEN set and no api_keys exist; all routes will "
            "return 401 until a key is created or AUTOMATON_TOKEN is set. "
            "Pass insecure_no_auth=True (or --insecure-no-auth) to allow "
            "unauthenticated access."
        )
    if (tls_cert is None) != (tls_key is None):
        raise ValueError(
            "TLS needs both --tls-cert and --tls-key, or neither"
        )
    tls_enabled = tls_cert is not None
    httpd = HTTPServer(
        (host, port),
        make_handler(db_path, auth_token, require_auth,
                     tls_enabled=tls_enabled,
                     require_read_auth=require_read_auth),
    )
    if tls_enabled:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        except (ssl.SSLError, FileNotFoundError, OSError) as e:
            httpd.server_close()
            raise ValueError(
                f"could not load TLS cert/key: {e}"
            ) from e
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    return httpd
