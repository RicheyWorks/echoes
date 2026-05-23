# Docker deployment

The repo ships a multi-stage `Dockerfile` and a `docker-compose.yml` that bring up all three processes with one command.

## Quick start

```bash
# 1. Clone and enter the automaton directory
git clone https://github.com/730richey730/echoes && cd echoes/automaton

# 2. Create your env file (never commit this)
cp deploy/automaton.env.example .env
# Edit .env: set AUTOMATON_TOKEN to a random string

# 3. Build and start
docker compose up -d

# 4. Verify
curl http://localhost:8080/healthz
```

The web UI is at `http://localhost:8080`.

## What the compose stack includes

| Service | Command | Notes |
|---|---|---|
| `ui` | `automaton serve --host 0.0.0.0 --port 8080 --auto-migrate` | Runs migrations on startup, publishes :8080, has a healthcheck |
| `worker` | `automaton worker --loop` | Waits for `ui` to be healthy before starting |
| `scheduler` | `automaton scheduler` | Waits for `ui` to be healthy before starting |

All three share the `automaton-data` named volume at `/data/automaton.db`.

## Persistent data

The SQLite file lives in the `automaton-data` Docker volume. To inspect or back it up:

```bash
# Copy DB out of the volume
docker run --rm -v automaton_automaton-data:/data -v $(pwd):/out \
    alpine cp /data/automaton.db /out/automaton.db.bak

# Mount the volume to a shell for inspection
docker run --rm -it -v automaton_automaton-data:/data alpine sh
```

Do **not** `docker compose down --volumes` unless you intend to wipe all run history.

## Building the image

```bash
docker build -t automaton:latest .
# Or for a specific target stage:
docker build --target runtime -t automaton:latest .
```

The image is ~200 MB (python:3.11-slim base + deps). The final stage runs as a non-root `automaton` user.

## Environment variables

All variables from [Configuration](../getting-started/configuration.md) work via the `.env` file or `environment:` in compose. The compose file sets sensible defaults:

```
AUTOMATON_DB=/data/automaton.db
AUTOMATON_LOG_FORMAT=json
AUTOMATON_LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

Set `AUTOMATON_TOKEN` in your `.env` file.

## Metrics

Prometheus can scrape `http://localhost:8080/metrics` directly — no auth required. See [Metrics →](../operations/metrics.md).
