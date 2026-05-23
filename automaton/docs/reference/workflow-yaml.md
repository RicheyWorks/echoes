# Workflow YAML reference

A workflow is a YAML file with a name and a list of steps.

## Top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Unique workflow name. Used in `automaton trigger <name>` and API calls. |
| `steps` | list | Yes | One or more step definitions. |
| `timeout_seconds` | int | No | Maximum seconds a run may take. Runs exceeding this are marked `timed_out`. |
| `timezone` | string | No | IANA timezone name (e.g. `America/New_York`) for cron scheduling. Default: UTC. |

## Step fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Unique within the workflow. Used in `needs:` references. |
| `type` | string | Yes | Step type: `shell`, `http_get`, `file_append`, `python`, `wait_for_signal`, or a plugin type. |
| `needs` | list | No | Step names that must complete before this step runs. |
| `retry` | object | No | Retry policy (see below). |
| `timeout_seconds` | int | No | Per-step timeout. |

## Step types

### `shell`

Run a shell command.

```yaml
- name: snapshot
  type: shell
  cmd: ["pg_dump", "mydb", "-f", "/backups/mydb.sql"]
```

| Field | Type | Required | Description |
|---|---|---|---|
| `cmd` | list or string | Yes | Command and arguments (list) or shell string (`sh -c` on POSIX, `cmd /c` on Windows). |
| `env` | object | No | Extra environment variables. |
| `cwd` | string | No | Working directory. |

### `http_get`

Make an HTTP GET request.

```yaml
- name: check-site
  type: http_get
  url: https://example.com/health
  timeout_seconds: 10
```

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | Yes | URL to fetch. |
| `timeout_seconds` | int | No | Request timeout. Default: 30. |
| `headers` | object | No | Extra request headers. |

### `file_append`

Append text to a file (creates the file if it doesn't exist).

```yaml
- name: log-event
  type: file_append
  path: /var/log/automaton-events.log
  text: "backup completed at {{ now }}\n"
```

| Field | Type | Required | Description |
|---|---|---|---|
| `path` | string | Yes | Filesystem path. |
| `text` | string | Yes | Text to append. Jinja2 templating supported. |

### `python`

Run a Python function.

```yaml
- name: process
  type: python
  module: my_package.tasks
  function: process_data
  kwargs:
    input_path: /data/input.csv
```

| Field | Type | Required | Description |
|---|---|---|---|
| `module` | string | Yes | Dotted module path. |
| `function` | string | Yes | Function name within the module. |
| `kwargs` | object | No | Keyword arguments passed to the function. |

### `wait_for_signal`

Park the run until an external signal arrives. Used for the agent loop pattern.

```yaml
- name: await-approval
  type: wait_for_signal
  signal_name: approval
```

Send the signal via `automaton signal <run_id> approval` or `POST /api/signals/<run_id>/approval`.

| Field | Type | Required | Description |
|---|---|---|---|
| `signal_name` | string | Yes | Signal identifier. Must match what is sent. |
| `timeout_seconds` | int | No | Fail the step if the signal doesn't arrive within this time. |

## Retry policy

```yaml
- name: fetch
  type: http_get
  url: https://api.example.com/data
  retry:
    max: 3
    backoff: exponential
    initial_seconds: 2
```

| Field | Type | Description |
|---|---|---|
| `max` | int | Maximum retry attempts. |
| `backoff` | string | `fixed` or `exponential`. |
| `initial_seconds` | int | First retry delay. Doubles on each attempt for `exponential`. |

## Dependencies (DAG)

Steps without `needs:` run immediately. Steps with `needs:` run once all listed steps have completed successfully.

```yaml
steps:
  - name: fetch          # runs immediately
    type: http_get
    url: https://api.example.com/data

  - name: transform      # runs after fetch
    type: python
    needs: [fetch]
    module: etl
    function: transform

  - name: load           # runs after transform
    type: shell
    needs: [transform]
    cmd: ["psql", "-f", "/tmp/load.sql"]

  - name: notify         # runs after load
    type: shell
    needs: [load]
    cmd: ["curl", "-d", "ETL complete", "ntfy.sh/my-topic"]
```

## Secret references

Reference secrets stored in the OS keychain:

```yaml
- name: deploy
  type: shell
  cmd: ["deploy.sh"]
  env:
    API_KEY: "${secret:DEPLOY_KEY}"
```

Set secrets with `automaton secret set DEPLOY_KEY`. Values are resolved at execution time and never written to the event log.

## Full example

```yaml
name: daily-backup
timeout_seconds: 3600
timezone: America/New_York

steps:
  - name: snapshot
    type: shell
    cmd: ["automaton", "backup", "/backups/automaton-$(date +%F).db"]
    retry:
      max: 2
      backoff: fixed
      initial_seconds: 60

  - name: upload
    type: shell
    needs: [snapshot]
    cmd: ["rclone", "copy", "/backups/", "b2:my-bucket/automaton/"]
    env:
      RCLONE_CONFIG_B2_ACCOUNT: "${secret:B2_ACCOUNT}"
      RCLONE_CONFIG_B2_KEY: "${secret:B2_KEY}"

  - name: notify
    type: shell
    needs: [upload]
    cmd: ["curl", "-d", "Backup complete", "https://ntfy.sh/my-topic"]
```
