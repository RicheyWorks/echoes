# automaton

A strongly consistent personal automation platform.

Triggers fire workflows. Workflows are a DAG of steps. Every step's *observable* side effect happens exactly once, even when workers crash mid-step. State lives in a single SQLite database — back that one file up and you've backed up the system.

## What it does

- **Cron and webhook triggers** — schedule workflows or fire them over HTTP
- **DAG step execution** — steps declare `needs:` dependencies; the worker runs independent steps in parallel
- **Exactly-once semantics** — idempotency keys prevent duplicate side effects across worker crashes and retries
- **Signals** — workflows can pause and wait for an external event (the agent loop pattern)
- **Native mobile clients** — iOS and Android apps for monitoring and triggering from your phone
- **Prometheus metrics** — `/metrics` endpoint for Grafana, VictoriaMetrics, or plain `curl`

## Install

```bash
pip install automaton-engine
automaton --help
```

Requires Python 3.10+. See [Getting started → Install](getting-started/install.md) for the full setup.

## Quick links

- [Quickstart](getting-started/quickstart.md) — define and run your first workflow in 5 minutes
- [Workflow YAML reference](reference/workflow-yaml.md) — every field documented
- [CLI reference](reference/cli.md) — every subcommand
- [HTTP API](reference/api.md) — trigger, signal, cancel, inspect over HTTP
- [Deploy on Linux](deployment/linux.md) — systemd units for production
- [Deploy with Docker](deployment/docker.md) — compose stack, one command
- [Backup & restore](operations/backup.md) — Litestream + snapshot strategy

## Design principles

**No external dependencies for the core.** The engine runs on any machine with Python 3.10 and nothing else. SQLite is the state store; no Postgres, no Redis, no message broker.

**One file, one backup.** The entire state of every run, every workflow, every cron trigger lives in a single SQLite file. Copy it somewhere safe and you're done.

**Exactly once or bust.** The worker uses lease-based execution with SHA-256 idempotency keys. A step either completes and commits atomically, or it doesn't — there is no in-between state where a side effect happened but the engine doesn't know about it.
