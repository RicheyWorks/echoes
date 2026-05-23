# CLI reference

All commands are accessed through the `automaton` entry point.

```
automaton <command> [options]
```

Run `automaton <command> --help` for full flag details on any command.

---

## Workflow management

### `register`

Register or update a workflow from a YAML file.

```bash
automaton register hello.yaml
automaton register workflows/daily-backup.yaml
```

Re-registering an existing name creates a new version. Running runs pin to the version they started on.

### `trigger`

Trigger a run of a registered workflow.

```bash
automaton trigger hello
automaton trigger daily-backup --payload '{"target": "/data"}'
```

### `inspect`

List recent runs, or show detail for one run.

```bash
automaton inspect              # list recent runs
automaton inspect 42           # detail for run ID 42
automaton inspect --status failed --limit 20
```

### `cancel`

Cancel an in-flight run.

```bash
automaton cancel 42
```

### `signal`

Send a signal to a run parked on a `wait_for_signal` step.

```bash
automaton signal 42 approval --payload '{"approved": true}'
```

---

## Process management

### `worker`

Run the worker loop — pull steps off the queue and execute them.

```bash
automaton worker                   # run forever
automaton worker --stop-when-idle  # drain queue and exit
```

### `scheduler`

Run the scheduler — fires cron triggers, reaps timed-out runs.

```bash
automaton scheduler
```

### `serve`

Start the HTTP API and web dashboard.

```bash
automaton serve
automaton serve --host 0.0.0.0 --port 8443 --tls-cert cert.pem --tls-key key.pem
automaton serve --auto-migrate     # run pending migrations on startup
```

---

## Cron triggers

### `schedule add`

Register a cron trigger for a workflow.

```bash
automaton schedule add daily-backup "0 3 * * *"
automaton schedule add morning-brief "0 8 * * *" --timezone America/New_York
```

### `schedule list`

List registered cron triggers and their next fire times.

```bash
automaton schedule list
```

### `scheduler next`

Preview the next N fire times for a cron trigger.

```bash
automaton scheduler next daily-backup
automaton scheduler next daily-backup --count 10
```

---

## Webhooks

### `webhook add`

Register a signed webhook endpoint.

```bash
automaton webhook add my-hook hello --secret my-signing-secret
```

### `webhook list`

List registered webhook endpoints.

```bash
automaton webhook list
```

---

## Maintenance

### `migrate`

Apply pending schema migrations.

```bash
automaton migrate
```

Always run this after upgrading `automaton-engine`. A pre-migration snapshot is saved automatically.

### `prune`

Delete terminal runs older than N days.

```bash
automaton prune --before 90
```

### `backup`

Snapshot the database to a file.

```bash
automaton backup /backups/automaton-$(date +%F).db
```

Runs `PRAGMA integrity_check` before copying. Aborts if the check fails.

---

## Secrets

### `secret set / get / rm / ls`

Manage secrets in the OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service).

```bash
automaton secret set GITHUB_TOKEN        # prompts for value
automaton secret get GITHUB_TOKEN        # prints to stdout
automaton secret rm  GITHUB_TOKEN
automaton secret ls                      # list names (no values)
```

### `secret import`

Import `AUTOMATON_SECRET_*` entries from an env file.

```bash
automaton secret import .env
```

---

## TLS

### `tls init`

Generate a self-signed certificate and private key.

```bash
automaton tls init --hostname automaton.local
automaton tls init --hostname automaton.your-tailnet.ts.net
```

---

## Notifications

### `notify test`

Send a test notification to every configured Apprise channel.

```bash
automaton notify test
```

---

## Mesh

### `mesh status`

Print Tailscale / Headscale connection status.

```bash
automaton mesh status
```
