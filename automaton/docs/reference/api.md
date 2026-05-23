# HTTP API reference

The automaton HTTP API is served by `automaton serve` (default `http://localhost:8080`).

## Authentication

Write routes require a `Bearer` token in the `Authorization` header:

```
Authorization: Bearer your-token-here
```

Read routes (`GET /api/*`) and open routes (`/healthz`, `/metrics`) require no token.

Set `AUTOMATON_TOKEN` in your env to configure the token. Disable auth with `AUTOMATON_REQUIRE_AUTH=false` (localhost-only setups).

---

## Open endpoints

### `GET /healthz`

Returns `{"ok": true}` with status 200. No auth. Used by Docker healthchecks and load balancers.

### `GET /metrics`

Returns a Prometheus text format 0.0.4 scrape payload. No auth. See [Metrics](../operations/metrics.md).

---

## Runs

### `GET /api/runs`

List recent runs.

**Response**

```json
[
  {
    "id": 42,
    "workflow": "daily-backup",
    "status": "completed",
    "started_at": "2026-05-22T03:00:01.234Z",
    "finished_at": "2026-05-22T03:00:05.891Z"
  }
]
```

### `GET /api/run/<id>`

Run detail with steps and event log.

**Response**

```json
{
  "id": 42,
  "workflow": "daily-backup",
  "status": "completed",
  "started_at": "2026-05-22T03:00:01.234Z",
  "finished_at": "2026-05-22T03:00:05.891Z",
  "steps": [
    {
      "name": "snapshot",
      "status": "completed",
      "started_at": "2026-05-22T03:00:01.500Z",
      "finished_at": "2026-05-22T03:00:04.100Z",
      "output": "backup written to /backups/2026-05-22.db"
    }
  ],
  "events": [
    {"ts": "2026-05-22T03:00:01.234Z", "event": "run_started"},
    {"ts": "2026-05-22T03:00:05.891Z", "event": "run_completed"}
  ]
}
```

### `GET /api/run/<id>/events`

Server-Sent Events stream. Emits a JSON object on every step or run state transition while the run is active. The connection closes when the run reaches a terminal state.

```
data: {"event": "step_completed", "step": "snapshot", "ts": "..."}

data: {"event": "run_completed", "ts": "..."}
```

### `POST /api/run/<id>/cancel`

Cancel a run. **Requires auth.**

**Response** `{"ok": true}`

---

## Workflows

### `GET /api/step_types`

List registered step types (built-in + any installed plugins).

**Response** `["shell", "http_get", "file_append", "python", ...]`

### `POST /api/workflows`

Register or update a workflow. **Requires auth.**

**Body** — workflow YAML or JSON spec:

```yaml
name: hello
steps:
  - name: greet
    type: file_append
    path: /tmp/hello.log
    text: "hi\n"
```

**Response** `{"workflow_def_id": 7}`

### `POST /api/trigger/<name>`

Trigger a run of a workflow by name. **Requires auth.**

**Body** (optional):

```json
{"payload": {"key": "value"}}
```

**Response** `{"run_id": 42}`

---

## Cron triggers

### `GET /api/crons`

List registered cron triggers.

### `POST /api/crons`

Register a cron trigger. **Requires auth.**

**Body**:

```json
{
  "workflow_name": "daily-backup",
  "cron_expr": "0 3 * * *",
  "timezone": "America/New_York"
}
```

**Response** `{"trigger_id": 3}`

---

## Signals

### `GET /api/signals`

List pending signals (runs parked on `wait_for_signal`).

### `POST /api/signals/<run_id>/<signal_name>`

Send a signal to a parked run. **Requires auth.**

**Body** (optional):

```json
{"payload": {"approved": true, "comment": "looks good"}}
```

**Response** `{"ok": true}`

---

## Webhooks

### `GET /api/webhooks`

List registered webhook endpoints.

### `POST /api/webhooks` (via CLI)

Use `automaton webhook add` — the API endpoint is reserved for CLI use.

---

## Error responses

All errors return JSON with a `"error"` key:

```json
{"error": "workflow not found: hello"}
```

HTTP status codes: `400` bad request, `401` missing/invalid token, `404` not found, `500` internal error.
