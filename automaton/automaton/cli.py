"""Thin CLI: register / trigger / worker / inspect / schedule / scheduler / serve / migrate."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from . import db as _db
from . import engine
from . import scheduler as _scheduler
from . import ui as _ui
from . import backup as _backup
from . import logs as _logs
from . import webhooks as _webhooks
from . import prune as _prune
from . import migrate as _mig
from . import tls as _tls
from . import mesh as _mesh
from . import secrets as _secrets
from . import notify as _notify
from . import templates as _templates


def _db_path():
    return os.environ.get("AUTOMATON_DB", "automaton.db")


def _auto_migrate_enabled() -> bool:
    """AUTOMATON_AUTO_MIGRATE=1/true/yes treats the gate as opt-in."""
    return os.environ.get("AUTOMATON_AUTO_MIGRATE", "").lower() in {"1", "true", "yes"}


def _open():
    """Open a DB connection, applying migrations only when safe.

    The migrations gate behaves like this:

    * Brand-new DB (file just created, no schema): auto-bootstrap. This
      keeps the first-run UX simple - users don't have to run
      ``automaton migrate`` before ``automaton register`` on a fresh box.
    * Pre-yoyo DB (legacy schema, no _yoyo_migration table): auto-shim it
      so existing installs upgrade transparently.
    * Pending real migrations on an already-populated DB: refuse to start
      unless AUTOMATON_AUTO_MIGRATE is set. The user has to run
      ``automaton migrate`` explicitly. This is the "don't auto-corrupt a
      multi-host setup" case from the plan.
    """
    path = _db_path()
    conn = _db.connect(path)
    if _auto_migrate_enabled():
        _db.migrate(conn)
        return conn

    if _mig._is_fresh_db(path) or _mig._is_pre_yoyo_db(path):
        # First run or legacy upgrade - safe to auto-apply.
        _db.migrate(conn)
        return conn

    _mig.assert_up_to_date(path)
    return conn



def _print_run_summary(detail):
    """Print a human-readable run summary to stdout."""
    run = detail["run"]
    icons = {"completed": "✓", "failed": "✗", "skipped": "–",
             "cancelled": "⊘", "timed_out": "⏱", "running": "…"}
    for step in detail["steps"]:
        icon = icons.get(step["status"], "?")
        print(f"    {icon}  {step['name']:<28} {step['status']}")
        out = step.get("output")
        if isinstance(out, dict):
            if "returncode" in out:
                stdout_lines = (out.get("stdout") or "").strip().splitlines()
                for line in stdout_lines[:3]:
                    print(f"           {line}")
                if len(stdout_lines) > 3:
                    print(f"           … ({len(stdout_lines) - 3} more lines)")
                if out.get("stderr", "").strip():
                    for line in out["stderr"].strip().splitlines()[:2]:
                        print(f"      stderr: {line}")
            elif "return_value" in out:
                print(f"           → {out['return_value']!r}")
        err = step.get("error")
        if err and isinstance(err, dict):
            print(f"      error: {err.get('message', '')}")
    print()
    icon = icons.get(run["status"], "?")
    print(f"  {icon}  {run['status'].upper()}")


def cmd_run(args):
    """One-shot local execution: register, trigger, drain worker, summarize."""
    import time

    spec_path = Path(args.spec_file)
    if not spec_path.exists():
        print(f"error: file not found: {spec_path}", file=sys.stderr)
        return 1
    try:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: could not parse YAML: {e}", file=sys.stderr)
        return 1

    conn = _open()
    try:
        engine.register_workflow(conn, spec)
    except (ValueError, KeyError) as e:
        print(f"error: invalid workflow spec: {e}", file=sys.stderr)
        return 1

    payload = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"error: --payload is not valid JSON: {e}", file=sys.stderr)
            return 1

    run_id = engine.trigger_run(conn, spec["name"], "cli-run", payload)
    print(f"automaton run  workflow={spec['name']!r}  run_id={run_id}")
    print()

    timeout = args.timeout if args.timeout else None
    deadline = (time.monotonic() + timeout) if timeout else None

    while True:
        engine.worker_loop(conn, stop_when_idle=True)
        detail = engine.run_detail(conn, run_id)
        run_status = detail["run"]["status"]
        if run_status not in ("pending", "running"):
            break
        if deadline and time.monotonic() > deadline:
            print(f"error: timed out after {timeout}s", file=sys.stderr)
            return 1
        time.sleep(0.1)

    _print_run_summary(detail)
    return 0 if run_status == "completed" else 1


def cmd_register(args):
    spec = yaml.safe_load(Path(args.spec_file).read_text(encoding="utf-8"))
    conn = _open()
    wid = engine.register_workflow(conn, spec)
    print(f"registered workflow {spec['name']!r} as workflow_def.id={wid}")
    return 0


def cmd_trigger(args):
    conn = _open()
    payload = json.loads(args.payload) if args.payload else None
    run_id = engine.trigger_run(conn, args.workflow_name, "manual", payload)
    print(f"triggered run {run_id}")
    return 0


def cmd_worker(args):
    conn = _open()
    print("worker running, ctrl-c to stop")
    try:
        engine.worker_loop(conn, stop_when_idle=args.once)
    except KeyboardInterrupt:
        print("\nworker stopped")
    return 0


def cmd_inspect(args):
    conn = _open()
    if args.run_id is None:
        # Use search_runs when any filter flag is set; otherwise list_runs.
        filtering = any([
            getattr(args, "status", None),
            getattr(args, "workflow", None),
            getattr(args, "after", None),
            getattr(args, "before", None),
        ])
        if filtering:
            runs = engine.search_runs(
                conn,
                status=getattr(args, "status", None) or None,
                workflow=getattr(args, "workflow", None) or None,
                after=getattr(args, "after", None) or None,
                before=getattr(args, "before", None) or None,
                limit=getattr(args, "limit", 50) or 50,
            )
        else:
            runs = engine.list_runs(conn, limit=getattr(args, "limit", 20) or 20)
        if not runs:
            print("(no runs)")
            return 0
        for r in runs:
            print(f"  run {r['id']:>4}  {r['workflow']:<20}  {r['status']:<12}  {r['started_at']}")
        return 0
    detail = engine.run_detail(conn, args.run_id)
    print(json.dumps(detail, indent=2, default=str))
    return 0


def cmd_schedule_add(args):
    conn = _open()
    try:
        tid = _scheduler.register_cron(
            conn, args.workflow_name, args.cron_expr,
            timezone=args.timezone,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    tz_note = f" [tz={args.timezone}]" if args.timezone else ""
    print(f"registered cron trigger {tid} for {args.workflow_name!r}: "
          f"{args.cron_expr!r}{tz_note}")
    return 0


def cmd_schedule_list(args):
    conn = _open()
    rows = _scheduler.list_crons(conn)
    if not rows:
        print("(no cron triggers)")
        return 0
    for r in rows:
        enabled = "on " if r["enabled"] else "off"
        last = r["last_fire_at"] or "-"
        tz = r.get("timezone") or "UTC"
        print(f"  [{enabled}] {r['id']:>3}  {r['workflow_name']:<20}  next={r['next_fire_at']}  last={last}  tz={tz}  expr={r['cron_expr']!r}")
    return 0


def cmd_scheduler(args):
    conn = _open()
    print(f"scheduler running, ctrl-c to stop (stop_after={args.stop_after})")
    try:
        _scheduler.scheduler_loop(conn, stop_after_seconds=args.stop_after)
    except KeyboardInterrupt:
        print("\nscheduler stopped")
    return 0


def cmd_scheduler_next(args):
    """Print the next N fire times for a cron expression in both tz + UTC."""
    try:
        previews = _scheduler.preview_fires(args.cron_expr,
                                             tz=args.timezone,
                                             count=args.count)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    tz_label = args.timezone or "UTC"
    print(f"next {args.count} fires of {args.cron_expr!r} (tz={tz_label}):")
    for p in previews:
        if p["local"] == p["utc"]:
            print(f"  {p['utc']}")
        else:
            print(f"  {p['local']}   (UTC: {p['utc']})")
    return 0


def cmd_signal(args):
    conn = _open()
    payload = json.loads(args.payload) if args.payload else None
    sid = engine.send_signal(conn, args.run_id, args.name, payload)
    print(f"signal sent: id={sid} run={args.run_id} name={args.name!r}")
    return 0


def cmd_webhook_add(args):
    conn = _open()
    wid, secret = _webhooks.register_webhook(
        conn, args.name, args.workflow,
        signature_header=args.header,
        signature_algo=args.algo,
    )
    print(f"registered webhook endpoint {wid}: {args.name!r} -> workflow {args.workflow!r}")
    print(f"signature header: {args.header}")
    print(f"signature algo:   {args.algo}")
    print()
    print(f"SECRET (shown only once, store this for the upstream caller):")
    print(f"  {secret}")
    return 0


def cmd_webhook_list(args):
    conn = _open()
    rows = _webhooks.list_webhooks(conn)
    if not rows:
        print("(no webhook endpoints)")
        return 0
    for r in rows:
        on = "on " if r["enabled"] else "off"
        print(f"  [{on}] {r['id']:>3}  {r['name']:<24}  -> {r['workflow_name']:<20}  "
              f"header={r['signature_header']}  algo={r['signature_algo']}")
    return 0


def cmd_webhook_disable(args):
    conn = _open()
    ok = _webhooks.disable_webhook(conn, args.name)
    if ok:
        print(f"disabled webhook endpoint {args.name!r}")
        return 0
    print(f"no webhook endpoint {args.name!r}")
    return 1


def cmd_cancel(args):
    conn = _open()
    ok = engine.cancel_run(conn, args.run_id, reason=args.reason)
    if ok:
        print(f"cancelled run {args.run_id}")
        return 0
    print(f"run {args.run_id}: not found or already in a terminal state")
    return 1


def cmd_prune(args):
    conn = _open()
    summary = _prune.prune(
        conn,
        older_than_days=args.older_than,
        dry_run=args.dry_run,
        vacuum=args.vacuum,
    )
    label = "WOULD prune" if args.dry_run else "pruned"
    print(f"{label} {summary['runs']} runs, {summary['steps']} steps, "
          f"{summary['events']} events, {summary['signals']} signals "
          f"(cutoff: {summary['cutoff']})")
    if summary.get("vacuumed"):
        print("vacuumed database")
    return 0


def cmd_backup(args):
    info = _backup.snapshot(_db_path(), args.dest)
    print(f"snapshot -> {info['destination']}  ({info['size_bytes']} bytes, "
          f"{info['pages']} pages, {info['elapsed_seconds']}s)")
    integrity = info.get("integrity")
    if integrity == "ok":
        print("integrity_check: ok")
        return 0
    if integrity is not None:
        print(f"integrity_check FAILED:\n{integrity}", file=sys.stderr)
        return 1
    return 0


def cmd_restore(args):
    """Restore the live DB from a snapshot file (with safety checks)."""
    dest = _db_path()
    try:
        info = _backup.restore(args.src, dest, force=args.force)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"restored -> {info['destination']}  ({info['size_bytes']} bytes)")
    print(f"integrity_check (source):       {info['integrity_source']}")
    print(f"integrity_check (destination):  {info['integrity_destination']}")
    print(f"schema version:                  {info['schema_version'] or 'unknown'}")
    # Schema gate: if the binary expects newer migrations than the
    # snapshot, refuse to silently boot up against it. The user must
    # run `automaton migrate` explicitly to bring the restored DB
    # forward.
    pending = _mig.pending(dest)
    if pending:
        print()
        print(f"note: {len(pending)} migration(s) pending against the restored DB:")
        for m in pending:
            print(f"  - {m}")
        print("run `automaton migrate` before starting workers/scheduler/serve.")
    return 0


def cmd_migrate(args):
    """Apply pending schema migrations.

    Takes a pre-migration snapshot by default. ``--dry-run`` lists what
    would be applied without touching the DB.
    """
    path = _db_path()
    # Ensure the file exists so yoyo can lock it.
    _db.connect(path).close()
    if args.dry_run:
        pending = _mig.pending(path)
        if not pending:
            print(f"no pending migrations (current: {_mig.current_version(path) or 'fresh'})")
            return 0
        print(f"{len(pending)} pending migration(s):")
        for mid in pending:
            print(f"  - {mid}")
        return 0
    result = _mig.apply(path, snapshot=not args.no_snapshot)
    if not result["applied"]:
        print(f"already up to date (current: {_mig.current_version(path) or 'fresh'})")
        return 0
    if result["snapshot"]:
        print(f"pre-migrate snapshot: {result['snapshot']}")
    print(f"applied {len(result['applied'])} migration(s):")
    for mid in result["applied"]:
        print(f"  + {mid}")
    return 0





def cmd_tls_init(args):
    """Generate a self-signed TLS cert + key under args.out_dir."""
    try:
        info = _tls.init_self_signed(
            args.out_dir,
            hostname=args.hostname,
            extra_sans=args.san or None,
            validity_days=args.validity_days,
            key_size=args.key_size,
        )
    except FileExistsError as e:
        print(f"refusing to overwrite: {e}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"cert:      {info['cert']}")
    print(f"key:       {info['key']}")
    print(f"hostname:  {info['hostname']}")
    print(f"SANs:      {', '.join(info['sans'])}")
    print(f"valid:    until {info['valid_until']}")
    print(f"sha-256:   {info['fingerprint_sha256']}")
    print()
    print("Trust this cert on every device that will hit the UI:")
    print(f"  - macOS:   open '{info['cert']}' in Keychain Access, mark as Always Trust")
    print(f"  - iOS:     AirDrop the .pem to the device, install profile, then")
    print(f"             Settings > General > About > Certificate Trust Settings")
    print(f"  - Android: Settings > Security > Install from storage")
    print(f"  - Windows: certmgr.msc > Trusted Root Certification Authorities")
    print(f"  - Linux:   sudo cp '{info['cert']}' /usr/local/share/ca-certificates/automaton.crt && sudo update-ca-certificates")
    return 0






def cmd_secret_set(args):
    """Store a secret; prompts for the value unless --value is provided."""
    if args.value is not None:
        value = args.value
    else:
        import getpass
        value = getpass.getpass(f"value for {args.name}: ")
    if not value:
        print("refusing to store an empty secret", file=sys.stderr)
        return 1
    try:
        _secrets.set(args.name, value)
    except _secrets.SecretError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"stored secret {args.name!r} (backend: {_secret_backend_name()})")
    return 0


def cmd_secret_get(args):
    try:
        v = _secrets.get(args.name)
    except _secrets.SecretError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.no_newline:
        sys.stdout.write(v)
    else:
        print(v)
    return 0


def cmd_secret_rm(args):
    try:
        existed = _secrets.delete(args.name)
    except _secrets.SecretError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if existed:
        print(f"deleted secret {args.name!r}")
        return 0
    print(f"no secret named {args.name!r}", file=sys.stderr)
    return 1


def cmd_secret_ls(args):
    names = _secrets.list_names()
    if not names:
        print("(no secrets, or this backend doesn't support enumeration)")
        print(f"backend: {_secret_backend_name()}")
        return 0
    for n in names:
        print(n)
    return 0


def cmd_secret_import(args):
    try:
        imported = _secrets.import_env_file(args.file)
    except (_secrets.SecretError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not imported:
        print(f"no entries imported from {args.file}")
        return 0
    print(f"imported {len(imported)} secret(s) from {args.file}:")
    for n in imported:
        print(f"  + {n}")
    return 0


def _secret_backend_name() -> str:
    """Best-effort: name of the keyring backend in use, for status output."""
    try:
        import keyring
        return type(keyring.get_keyring()).__name__
    except Exception:
        return "unknown"


def cmd_init(args):
    """Copy a workflow template into the current directory."""
    if args.list or args.template is None:
        # List templates and exit.
        metas = _templates.discover()
        if not metas:
            print("no templates available")
            return 1
        print("available templates:")
        for m in metas:
            short = m.description or m.title or "(no description)"
            if len(short) > 90:
                short = short[:87] + "..."
            print(f"  {m.slug:<32}  {short}")
        if args.template is None:
            return 0
        return 0
    dest_name = args.name + (".yaml" if not args.name.endswith(".yaml") else "")
    dest = Path(dest_name)
    try:
        out = _templates.copy(args.template, dest)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    meta = _templates.by_slug(args.template)
    print(f"wrote {out}")
    if meta.requires_secrets:
        print(f"required secrets: {meta.requires_secrets}")
    if meta.requires_env:
        print(f"required env: {meta.requires_env}")
    if meta.cron:
        print(f"suggested cron: {meta.cron}")
    print()
    print(f"next: edit {out} to fill in payload defaults, then:")
    print(f"  automaton register {out}")
    return 0


def cmd_notify_test(args):
    """Send a hello message to every configured channel; report results."""
    failure_urls = _notify._split_urls(os.environ.get(_notify.ENV_NOTIFY_ON_FAILURE))
    success_urls = _notify._split_urls(os.environ.get(_notify.ENV_NOTIFY_ON_SUCCESS))
    timeout_urls = _notify._split_urls(os.environ.get(_notify.ENV_NOTIFY_ON_TIMEOUT))
    all_urls = failure_urls + success_urls + timeout_urls
    if not all_urls:
        print(f"no notify URLs configured. Set {_notify.ENV_NOTIFY_ON_FAILURE}, "
              f"{_notify.ENV_NOTIFY_ON_SUCCESS}, and/or {_notify.ENV_NOTIFY_ON_TIMEOUT} "
              f"and re-run.",
              file=sys.stderr)
        return 1
    print(f"sending test message to {len(all_urls)} URL(s):")
    for u in all_urls:
        # Mask credentials in the URL when printing.
        print(f"  - {_redact_url(u)}")
    result = _notify.send(
        all_urls,
        title="automaton: notify test",
        body="If you see this, your notify configuration is working.",
        urgent=True,  # bypass quiet hours for the test
    )
    if result["sent"]:
        print(f"sent to {result['channels']} channel(s).")
        return 0
    reason = result.get("reason") or "unknown failure"
    print(f"send failed: {reason}", file=sys.stderr)
    return 1


def _redact_url(url: str) -> str:
    """Hide the credential portion of a URL when echoing it."""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        creds, sep, host = rest.partition("@")
        if sep:
            return f"{scheme}://***@{host}"
    return url


def cmd_mesh_status(args):
    """Report Tailscale / mesh status and a local-port reachability check."""
    info = _mesh.status()

    def line(k, v):
        print(f"  {k:<14} {v}")

    print("mesh:")
    line("installed", "yes" if info["installed"] else "no")
    line("running", "yes" if info["running"] else "no")
    line("logged in", "yes" if info["logged_in"] else "no")
    line("hostname", info["hostname"] or "-")
    line("IPs", ", ".join(info["ips"]) if info["ips"] else "-")
    line("MagicDNS", info["magic_dns"] or "-")
    line("tailnet", info["tailnet"] or "-")
    line("peers", str(info["peers"]))
    if info["notes"]:
        print()
        print("notes:")
        for n in info["notes"]:
            print(f"  - {n}")

    # Local engine reachability check
    print()
    print("engine:")
    reachable = _mesh.check_port_locally(args.port)
    line("local port", f"{args.port} ({'open' if reachable else 'closed'})")
    if not reachable:
        print()
        print("  port closed - is `automaton serve --port {}` running?".format(args.port))

    # Exit non-zero if anything looks wrong, so scripts can detect "not OK".
    if not info["installed"] or not info["running"] or not info["logged_in"]:
        return 1
    if not reachable:
        return 2
    return 0


def cmd_serve(args):
    # Make sure the DB exists with the schema applied before serving.
    _open().close()
    try:
        httpd = _ui.serve(
            _db_path(),
            host=args.host,
            port=args.port,
            insecure_no_auth=args.insecure_no_auth,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    scheme = "https" if args.tls_cert else "http"
    auth_note = " (insecure: write API open)" if args.insecure_no_auth else ""
    print(f"automaton ui on {scheme}://{args.host}:{args.port}/{auth_note}  (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nui stopped")
        httpd.server_close()
    return 0


def main(argv=None):
    _logs.setup()
    p = argparse.ArgumentParser(prog="automaton")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="register a workflow YAML")
    p_reg.add_argument("spec_file")
    p_reg.set_defaults(func=cmd_register)

    p_trig = sub.add_parser("trigger", help="trigger a run")
    p_trig.add_argument("workflow_name")
    p_trig.add_argument("--payload")
    p_trig.set_defaults(func=cmd_trigger)

    p_work = sub.add_parser("worker", help="run the worker loop")
    p_work.add_argument("--once", action="store_true")
    p_work.set_defaults(func=cmd_worker)

    p_ins = sub.add_parser("inspect", help="list runs or show one run")
    p_ins.add_argument("run_id", nargs="?", type=int)
    p_ins.add_argument("--status", help="filter by status (e.g. failed, completed)")
    p_ins.add_argument("--workflow", help="filter by workflow name (exact)")
    p_ins.add_argument("--after", help="only runs started after this ISO-8601 datetime")
    p_ins.add_argument("--before", help="only runs started before this ISO-8601 datetime")
    p_ins.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    p_ins.set_defaults(func=cmd_inspect)

    p_sch = sub.add_parser("schedule", help="manage cron triggers")
    sch_sub = p_sch.add_subparsers(dest="schedule_cmd", required=True)
    p_sch_add = sch_sub.add_parser("add")
    p_sch_add.add_argument("workflow_name")
    p_sch_add.add_argument("cron_expr")
    p_sch_add.add_argument("--timezone", default=None,
                            help="IANA name (e.g. America/Los_Angeles). "
                                 "Default: UTC.")
    p_sch_add.set_defaults(func=cmd_schedule_add)
    p_sch_list = sch_sub.add_parser("list")
    p_sch_list.set_defaults(func=cmd_schedule_list)

    p_sd = sub.add_parser("scheduler", help="run the scheduler leader loop, or preview fires")
    sd_sub = p_sd.add_subparsers(dest="scheduler_cmd")
    # Backwards-compatible: bare `automaton scheduler [--stop-after S]` still runs the loop.
    p_sd.add_argument("--stop-after", type=float, default=None)
    p_sd.set_defaults(func=cmd_scheduler)

    p_sd_next = sd_sub.add_parser(
        "next",
        help="print the next N fire times for a cron expression (DST debug)",
    )
    p_sd_next.add_argument("cron_expr")
    p_sd_next.add_argument("--timezone", default=None,
                              help="IANA timezone (default UTC)")
    p_sd_next.add_argument("--count", type=int, default=10,
                              help="how many fires to preview (default 10)")
    p_sd_next.set_defaults(func=cmd_scheduler_next)

    p_sig = sub.add_parser("signal", help="send a signal to a run")
    p_sig.add_argument("run_id", type=int)
    p_sig.add_argument("name")
    p_sig.add_argument("--payload", help="JSON payload string")
    p_sig.set_defaults(func=cmd_signal)

    p_wh = sub.add_parser("webhook", help="manage signed webhook endpoints")
    wh_sub = p_wh.add_subparsers(dest="webhook_cmd", required=True)
    p_wh_add = wh_sub.add_parser("add", help="register or update an endpoint")
    p_wh_add.add_argument("name")
    p_wh_add.add_argument("--workflow", required=True)
    p_wh_add.add_argument("--header", default="X-Automaton-Signature")
    p_wh_add.add_argument("--algo", default="sha256",
                          choices=("sha256", "sha1", "sha512"))
    p_wh_add.set_defaults(func=cmd_webhook_add)
    p_wh_list = wh_sub.add_parser("list")
    p_wh_list.set_defaults(func=cmd_webhook_list)
    p_wh_dis = wh_sub.add_parser("disable")
    p_wh_dis.add_argument("name")
    p_wh_dis.set_defaults(func=cmd_webhook_disable)

    p_cancel = sub.add_parser("cancel", help="cancel an in-flight run")
    p_cancel.add_argument("run_id", type=int)
    p_cancel.add_argument("--reason", default=None)
    p_cancel.set_defaults(func=cmd_cancel)

    p_prune = sub.add_parser("prune", help="delete terminal runs older than N days")
    p_prune.add_argument("--older-than", type=float, default=90,
                          help="threshold in days (default: 90)")
    p_prune.add_argument("--dry-run", action="store_true",
                          help="report counts without deleting")
    p_prune.add_argument("--vacuum", action="store_true",
                          help="VACUUM after deleting to reclaim disk space")
    p_prune.set_defaults(func=cmd_prune)

    p_bak = sub.add_parser("backup", help="snapshot the database to a file")
    p_bak.add_argument("dest", help="destination file path")
    p_bak.set_defaults(func=cmd_backup)

    p_rest = sub.add_parser(
        "restore",
        help="restore the live DB from a snapshot file (refuses to clobber)",
    )
    p_rest.add_argument("src", help="snapshot file to restore from")
    p_rest.add_argument("--force", action="store_true",
                          help="overwrite the existing DB if present")
    p_rest.set_defaults(func=cmd_restore)

    p_mig = sub.add_parser(
        "migrate",
        help="apply pending schema migrations (with pre-migrate snapshot)",
    )
    p_mig.add_argument("--dry-run", action="store_true",
                        help="report pending migrations without applying")
    p_mig.add_argument("--no-snapshot", action="store_true",
                        help="skip the pre-migrate DB snapshot (not recommended)")
    p_mig.set_defaults(func=cmd_migrate)

    p_serve = sub.add_parser("serve", help="run the inspection UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--insecure-no-auth", action="store_true",
                         help="disable bearer-token auth on POST routes (DEV ONLY)")
    p_serve.add_argument("--tls-cert", default=None,
                         help="path to a PEM-encoded TLS certificate")
    p_serve.add_argument("--tls-key", default=None,
                         help="path to the matching PEM-encoded private key")
    p_serve.set_defaults(func=cmd_serve)

    p_tls = sub.add_parser("tls", help="manage TLS certs for the UI server")
    tls_sub = p_tls.add_subparsers(dest="tls_cmd", required=True)
    p_tls_init = tls_sub.add_parser(
        "init",
        help="generate a self-signed cert + key (writes cert.pem and key.pem)",
    )
    p_tls_init.add_argument("--out-dir", default="./tls",
                              help="directory to write cert.pem and key.pem (default: ./tls)")
    p_tls_init.add_argument("--hostname", default="automaton.local",
                              help="CommonName + first SAN (default: automaton.local)")
    p_tls_init.add_argument("--san", action="append", default=[],
                              help="extra SubjectAltName (DNS or IP); pass multiple times")
    p_tls_init.add_argument("--validity-days", type=int, default=825,
                              help="cert validity in days (default 825, the macOS/iOS max)")
    p_tls_init.add_argument("--key-size", type=int, default=2048,
                              help="RSA key size in bits (default 2048)")
    p_tls_init.set_defaults(func=cmd_tls_init)

    p_mesh = sub.add_parser("mesh", help="mesh networking (Tailscale / Headscale) status")
    mesh_sub = p_mesh.add_subparsers(dest="mesh_cmd", required=True)
    p_mesh_status = mesh_sub.add_parser(
        "status",
        help="report Tailscale status + local UI port reachability",
    )
    p_mesh_status.add_argument("--port", type=int, default=8080,
                                 help="local port to check (default 8080)")
    p_mesh_status.set_defaults(func=cmd_mesh_status)

    p_sec = sub.add_parser("secret", help="manage secrets in the OS keyring")
    sec_sub = p_sec.add_subparsers(dest="secret_cmd", required=True)

    p_sec_set = sec_sub.add_parser("set", help="store a secret (prompts unless --value)")
    p_sec_set.add_argument("name")
    p_sec_set.add_argument("--value", default=None,
                            help="value (omit for an interactive prompt - safer)")
    p_sec_set.set_defaults(func=cmd_secret_set)

    p_sec_get = sec_sub.add_parser("get", help="print a secret to stdout")
    p_sec_get.add_argument("name")
    p_sec_get.add_argument("--no-newline", action="store_true",
                            help="omit the trailing newline (for piping)")
    p_sec_get.set_defaults(func=cmd_secret_get)

    p_sec_rm = sec_sub.add_parser("rm", help="delete a secret")
    p_sec_rm.add_argument("name")
    p_sec_rm.set_defaults(func=cmd_secret_rm)

    p_sec_ls = sec_sub.add_parser("ls", help="list secret names (no values)")
    p_sec_ls.set_defaults(func=cmd_secret_ls)

    p_sec_imp = sec_sub.add_parser("import",
                                    help="import AUTOMATON_SECRET_* entries from an env file")
    p_sec_imp.add_argument("file")
    p_sec_imp.set_defaults(func=cmd_secret_import)

    p_not = sub.add_parser("notify", help="notifications (Apprise-based)")
    not_sub = p_not.add_subparsers(dest="notify_cmd", required=True)
    p_not_test = not_sub.add_parser(
        "test",
        help="send a hello message to every configured channel (bypasses quiet hours)",
    )
    p_not_test.set_defaults(func=cmd_notify_test)

    p_init = sub.add_parser(
        "init",
        help="scaffold a workflow YAML from a built-in template",
    )
    p_init.add_argument("name", nargs="?", default=None,
                          help="output filename (.yaml appended if missing)")
    p_init.add_argument("--template",
                          help="template slug (e.g. 'health/website-up'). "
                               "Omit to list available templates.")
    p_init.add_argument("--list", action="store_true",
                          help="list available templates and exit")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser(
        "run",
        help="one-shot: register, trigger, run worker, print results, exit 0/1",
    )
    p_run.add_argument("spec_file", help="path to workflow YAML file")
    p_run.add_argument("--payload", default=None,
                        help="JSON object to use as the trigger payload")
    p_run.add_argument("--timeout", type=int, default=0,
                        help="wall-clock timeout in seconds (0 = no limit)")
    p_run.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    if not hasattr(args, "func"):
        p.print_help()
        raise SystemExit(1)
    rc = args.func(args)
    raise SystemExit(rc if isinstance(rc, int) else 0)
