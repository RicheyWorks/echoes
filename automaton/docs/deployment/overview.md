# Deployment overview

automaton runs as three long-lived processes. Each can be managed by any process supervisor — systemd on Linux, launchd on macOS, NSSM on Windows, or Docker Compose everywhere.

| Process | Command | Purpose |
|---|---|---|
| Worker | `automaton worker` | Pulls steps off the queue and executes them |
| Scheduler | `automaton scheduler` | Fires cron triggers, reaps timed-out runs |
| UI | `automaton serve` | HTTP API + web dashboard |

All three share the same SQLite database file. Mount it on a fast local disk; do not put it on NFS.

## Choose a deployment target

| Target | Best for |
|---|---|
| [Linux (systemd)](linux.md) | VPS, home server, Raspberry Pi |
| [macOS (launchd)](macos.md) | Mac mini, MacBook as automation host |
| [Windows (NSSM)](windows.md) | Automating tasks on a Windows machine |
| [Docker](docker.md) | Anywhere Docker runs; easiest to version and ship |
| [iOS client](ios.md) | Monitoring and triggering from iPhone/iPad |
| [Android client](android.md) | Monitoring and triggering from Android |
| [Mesh networking](mesh.md) | Reaching the server from outside your LAN |

## Before you start

1. Generate a bearer token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Set `AUTOMATON_TOKEN` in your env file and keep the file mode `600`.
3. Run `automaton migrate` once before starting the processes.
4. Confirm with `curl http://localhost:8080/healthz` → `{"ok": true}`.
