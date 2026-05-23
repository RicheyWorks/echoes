# Workflow templates

Curated starter workflows. Copy one into your own repo with `automaton init <name> [--template <category>/<name>]`, then edit the payload defaults to taste.

Each template ships with a comment block explaining what it does, what secrets/env it needs, and when it was last manually verified.
 CI runs every template through `validate_spec` on every PR so the catalog never drifts from the engine's schema.

## agent

- **`agent/claude-loop`** — Logs the kickoff, parks on `agent_response`, then uses the agent's reply in a follow-up step. Trigger with ...

## backup

- **`backup/home-folder`** — Sync a local directory to a B2 bucket nightly. Sends a notification on failure.

## dev

- **`dev/docker-prune`** — Frees disk on dev boxes that accumulate cruft. Reports before/after disk usage in the step output.
- **`dev/git-mirror`** — nightly clone --mirror + remote update against a local storage path. Useful for self-hosted backups of code...

## health

- **`health/cert-expiry`** — Connects to a host:port, reads the server cert, fails the run (triggering the failure notification) if not_...
- **`health/website-up`** — Polls a website and the engine's notification config sends a ping if it stops returning 2xx. Pairs with AUT...

## infra

- **`infra/letsencrypt-renew`** — Runs `certbot renew` twice-weekly. If anything was renewed, reloads nginx so the new cert is picked up.
- **`infra/log-rotation`** — For a target directory, gzip files older than N days and delete files older than M days. Self-contained: do...

## media

- **`media/photo-import`** — Reads ${run.payload.source} (e.g. /Volumes/SD) and copies JPG/RAW into ${run.payload.dest}/YYYY-MM-DD/. Sou...

## personal

- **`personal/morning-brief`** — Fetches a feed URL, parses the top N entries, appends titles + links to a markdown file you read with your ...
