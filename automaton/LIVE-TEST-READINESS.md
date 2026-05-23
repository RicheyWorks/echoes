# Live-test readiness

This document is for the first real-machine deployment of automaton. It assumes you've ordered hardware and want a clear-eyed view of what to set up, what to monitor, what could go wrong, and how you'll know the test succeeded.

## Audit findings

The codebase as of v0.2.0:

| Area | Status | Notes |
|---|---|---|
| Consistency (exactly-once observable effects) | Solid | 15 tests pass, including the crash-mid-step proof. |
| Single-leader scheduling | Solid | Racing-schedulers test confirms exactly-one-fire. |
| SQLite as state store | Solid | WAL mode, sub-second timestamps, per-request connections in the UI. |
| Plugin system (step types via entry points) | New in v0.2 | External packages register handlers; tested. |
| HTTP write API for agents | New in v0.2 | POST /api/workflows, /api/trigger/NAME, /api/crons. Bearer token auth. |
| Structured logging | New in v0.2 | Text or JSON; env var controlled. |
| Backups | Online snapshot + Litestream doc | See BACKUP.md. |
| Process supervision | Provided in `deploy/systemd/` — see deploy/README.md. |
| Retry policy on failure | Done in v0.2 |
| Webhook trigger (signed receiver) | Done in v0.2 |
| Auth on read routes | Open — fine on localhost, dangerous otherwise. |
| Workflow signals (wait_for_signal + POST /api/signals) | Done — agents can park and resume runs. |
| Metrics endpoint | Done — `GET /metrics` returns Prometheus text 0.0.4; no auth required (like /healthz). 11 tests pass. |

## Setup checklist for the test machine

### 1. Filesystem layout

```
/opt/automaton/              # source / venv
  venv/
  src/
/var/lib/automaton/          # state
  automaton.db
  automaton.db-wal
  automaton.db-shm
/var/log/automaton/          # logs
  automaton.log
  automaton.log.1 .. .5      # rotating
/etc/automaton/
  automaton.env              # config (token, log level, etc.)
```

Make `/var/lib/automaton` a directory the automaton user owns and nothing else writes to. Litestream and your daily snapshot cron both read from here.

### 2. systemd units

Three long-running processes: worker, scheduler, UI. Each gets its own unit so you can restart them independently.

`/etc/systemd/system/automaton-worker.service`:

```ini
[Unit]
Description=automaton worker
After=network.target

[Service]
Type=simple
User=automaton
Group=automaton
EnvironmentFile=/etc/automaton/automaton.env
ExecStart=/opt/automaton/venv/bin/automaton worker
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Same shape for `automaton-scheduler.service` (ExecStart: `automaton scheduler`) and `automaton-ui.service` (ExecStart: `automaton serve --host 127.0.0.1 --port 8080`).

For multiple workers, use a systemd template (`automaton-worker@.service`) and start `automaton-worker@1`, `automaton-worker@2` etc. They'll cooperate on the queue without configuration.

### 3. The env file

`/etc/automaton/automaton.env`:

```
AUTOMATON_DB=/var/lib/automaton/automaton.db
AUTOMATON_LOG_FILE=/var/log/automaton/automaton.log
AUTOMATON_LOG_LEVEL=INFO
AUTOMATON_LOG_FORMAT=json
AUTOMATON_TOKEN=<a long random string>
```

Generate the token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Anything that POSTs to the API needs this string in `Authorization: Bearer …`. Keep the file mode at 600.

### 4. Backups

Two layers, per BACKUP.md:

Daily snapshot to a separate volume:

```cron
0 3 * * *  automaton  /opt/automaton/venv/bin/automaton backup /var/backups/automaton/$(date +\%F).db
```

Continuous Litestream replication to S3 (or another local disk). Pick one based on how much loss you can tolerate. For a first test, daily snapshot is fine.

### 5. UI exposure

Default `127.0.0.1:8080` binding is the safe choice. If you need it reachable from another machine:

1. Put it behind a reverse proxy that does TLS and an auth layer (basic auth, OIDC, mTLS, whatever you already run).
2. Keep automaton bound to localhost; the reverse proxy connects locally.
3. Never run `--host 0.0.0.0 --insecure-no-auth`. The write API would be world-callable.

## What to monitor

During the test, watch four things:

**1. The event_log table.** Every step transition writes a row. If it stops growing while workflows are still being triggered, the worker has stalled.

```sql
SELECT COUNT(*), MAX(ts) FROM event_log;
```

**2. The queue table.** Should empty within seconds of work being available. A queue with rows but no `leased_by` means workers can't reach the DB. A queue with old `leased_until` values means workers are crashing mid-step (recovery will pick them up, but it's a signal).

```sql
SELECT step_id, leased_by, leased_until FROM queue;
```

**3. Stuck runs.** A run still in `running` state long after `started_at` means at least one step is stuck.

```sql
SELECT id, workflow_def_id, started_at FROM run
WHERE status = 'running' AND started_at < datetime('now', '-1 hour');
```

**4. Failed steps.** Inspect them; the failure mode tells you what to fix.

```sql
SELECT s.name, s.error_json, r.id AS run_id, w.name AS workflow
FROM step s JOIN run r ON s.run_id = r.id
JOIN workflow_def w ON r.workflow_def_id = w.id
WHERE s.status = 'failed' ORDER BY s.id DESC LIMIT 20;
```

`automaton serve` already shows this in the UI; the queries above are the same data for headless monitoring.

## What success looks like

For a multi-day test, success is:

- No data loss across worker restarts. Trigger a workflow, kill the worker mid-step (with `kill -9`), restart it, confirm the side effect happened exactly once. This is the same test that runs in `tests/test_consistency.py` but on real hardware.
- Cron triggers fire on schedule, no double-fires when you restart the scheduler.
- The UI dashboard is responsive; backups don't lock writers.
- DB size grows linearly with runs (this is fine; pruning is a future enhancement — see "what to do about the event log" below).

## Rollback plan

If the live system gets into a state you can't reason about:

1. Stop all three units: `systemctl stop automaton-worker automaton-scheduler automaton-ui`.
2. Move the live DB aside: `mv /var/lib/automaton/automaton.db{,.broken-$(date +%s)}`.
3. Restore from the most recent backup snapshot.
4. Start the units back up.

Because each step's idempotency key is a hash of `(run_id, step_name, attempt)`, restoring from yesterday's snapshot does **not** create duplicate side effects for workflows that already ran — the steps' idempotency keys are unchanged, so external systems that honor those keys will dedupe.

## What to capture for postmortems

If something interesting happens during the test:

- `/var/log/automaton/automaton.log*` — the JSON logs, gzipped.
- The DB file at the moment of failure (`automaton backup /tmp/forensic.db`).
- `systemctl status automaton-worker automaton-scheduler automaton-ui` output.
- The output of the four monitoring SQL queries above.

That's enough to reconstruct what happened. The event_log table alone tells you the full story of every run.

## Known foot-guns

- **Editing a YAML workflow file does nothing.** You must re-`register` it to bump the version. Existing runs pin to the version they started with.
- **`http_get` failures don't auto-retry.** A failed step stays failed. If you depend on retries today, wrap the side effect in a workflow that re-triggers via the API on failure. The proper fix is the retry policy on the roadmap.
- **A worker crash between `lease` and `execute` is fine.** A crash between `execute` and `commit_step` is also fine — the next worker re-runs the step, and the idempotency key prevents duplicate observable effects. But this only holds if the step type respects the key. New step types must be reviewed for this.
- **Two scheduler processes on one box is fine** (they elect a leader). Two scheduler processes on two boxes pointing at the same SQLite file over NFS is **not** fine — SQLite's locking over NFS is unreliable. Pick one box for the scheduler.

## What to do about the event log

It grows unbounded. For a multi-week test that's fine — call it ~100 rows per run. For a multi-year deployment you'll want to prune old events. The simplest pattern: a nightly job that copies events older than 90 days to `event_log_archive` (a separate table or a Parquet file), then deletes them from `event_log`. The unbounded-log behavior is intentional for now — easier to debug than a too-eager pruner.

## What to do about retries

The single biggest missing feature. Today, a failed step stops the run. The intended shape (per design doc §10.8 spirit):

```yaml
- name: fetch
  type: http_get
  url: https://api.example.com/...
  retry:
    max: 3
    backoff: exponential
    initial_seconds: 2
```

The worker would see the failed step, increment `attempt`, create a new step row with a new idempotency key (rotated per attempt — the design doc says this explicitly), and re-queue with `ready_at = now + backoff`. Add this before depending on it.

## Quick test plan

Day 1: deploy, smoke-test, leave a heartbeat workflow on a cron (every 5 minutes, file_append).
Day 2: kill a worker, confirm recovery; restart the scheduler, confirm exactly-one-fire.
Day 3: take a backup, restore to a second machine, confirm the second machine sees the same runs.
Day 4-7: leave it running with whatever real workflows you want to try.
End of test: dump