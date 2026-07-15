# Workflow templates

Curated starter workflows. Copy one into your own repo with `automaton init <name> [--template <category>/<name>]`, then edit the payload defaults to taste.

Each template ships with a comment block explaining what it does, what secrets/env it needs, and when it was last manually verified.
 CI runs every template through `validate_spec` on every PR so the catalog never drifts from the engine's schema.

## agent

- **`agent/claude-loop`** — Starts a task, pauses with wait_for_signal, then records the result once a human (or external agent) resume...
- **`agent/echoes-daily`** — Advances the Echo agent by a configurable number of ticks, then immediately verifies hash-chain integrity a...
- **`agent/echoes-monitor`** — Every 5 minutes, advances a persistent echoes agent with real sensors (file watch + process + network scan)...
- **`agent/echoes-verify`** — Runs `echoes verify` against ECHOES_DB every hour. The step fails (raising StepError) if the chain does not...

## backup

- **`backup/home-folder`** — Syncs SOURCE_DIR to a remote rclone destination every night at 03:00. Appends the exit code to a log file a...

## dev

- **`dev/docker-prune`** — Runs docker container/image/volume/buildx prune every Sunday at 04:00, then appends a summary to LOG_FILE.
- **`dev/git-mirror`** — Clones or updates a bare mirror of REPO_URL to MIRROR_DIR every night. Supports private repos via the GITHU...

## health

- **`health/cert-expiry`** — Checks the TLS cert for DOMAIN every Monday morning. Fails (and alerts) when fewer than WARN_DAYS days rema...
- **`health/website-up`** — Probes TARGET_URL every 15 minutes. Exits non-zero (triggering AUTOMATON_NOTIFY_ON_FAILURE if set) when the...

## infra

- **`infra/letsencrypt-renew`** — Runs certbot renew twice a week (Mon/Thu at 05:00) for DOMAIN. Reloads nginx if any cert was actually renew...
- **`infra/log-rotation`** — Compresses *.log files in LOG_DIR at 00:05 nightly, then deletes compressed logs older than KEEP_DAYS days.

## media

- **`media/photo-import`** — Walks SD_MOUNT looking for common photo and video formats. Copies new files into LIBRARY_DIR/YYYY/YYYY-MM-D...

## personal

- **`personal/morning-brief`** — Fetches weather from WEATHER_URL (wttr.in or similar) every morning at 07:00 and appends a formatted digest...
