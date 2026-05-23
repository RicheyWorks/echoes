# Configuration

All configuration is via environment variables. No config file format — just set them in your shell, a `.env` file, or a systemd `EnvironmentFile`.

## Core

| Variable | Default | Description |
|---|---|---|
| `AUTOMATON_DB` | `automaton.db` (cwd) | Path to the SQLite database file |
| `AUTOMATON_TOKEN` | *(none)* | Bearer token for the HTTP API. Required when `AUTOMATON_REQUIRE_AUTH=true` |
| `AUTOMATON_REQUIRE_AUTH` | `true` | Enforce bearer token on write routes |
| `AUTOMATON_AUTO_MIGRATE` | `false` | Run pending migrations automatically on startup |

## Logging

| Variable | Default | Description |
|---|---|---|
| `AUTOMATON_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AUTOMATON_LOG_FORMAT` | `text` | `text` or `json` |
| `AUTOMATON_LOG_FILE` | *(stderr)* | Path to a log file; rotates at 10 MB, keeps 5 |

## Notifications

| Variable | Default | Description |
|---|---|---|
| `AUTOMATON_NOTIFY_ON_FAILURE` | *(none)* | Apprise URL — fires when a run reaches `failed` |
| `AUTOMATON_NOTIFY_ON_TIMEOUT` | *(none)* | Apprise URL — fires when a run reaches `timed_out` |
| `AUTOMATON_NOTIFY_QUIET_HOURS` | *(none)* | e.g. `22:00-07:00` — buffer non-critical notifications |

See [Apprise URL formats](https://github.com/caronc/apprise/wiki) for supported destinations (`ntfy://`, `slack://`, `discord://`, `mailto://`, and 100+ more).

## TLS

| Variable | Default | Description |
|---|---|---|
| `AUTOMATON_TLS_CERT` | *(none)* | Path to PEM certificate |
| `AUTOMATON_TLS_KEY` | *(none)* | Path to PEM private key |

Or pass `--tls-cert` / `--tls-key` to `automaton serve` directly.

## Example env file

```bash
# /etc/automaton/automaton.env
AUTOMATON_DB=/var/lib/automaton/automaton.db
AUTOMATON_TOKEN=your-long-random-token-here
AUTOMATON_LOG_FORMAT=json
AUTOMATON_LOG_LEVEL=INFO
AUTOMATON_LOG_FILE=/var/log/automaton/automaton.log
AUTOMATON_NOTIFY_ON_FAILURE=ntfy://your-ntfy-host/automaton-alerts
```

Generate a token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
