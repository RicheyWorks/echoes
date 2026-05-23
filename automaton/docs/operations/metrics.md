# Metrics (Prometheus)

`GET /metrics` returns a Prometheus text format 0.0.4 scrape payload. No authentication is required — the endpoint is intentionally open, like `/healthz`.

## Metric families

| Metric | Type | Description |
|---|---|---|
| `automaton_runs_total{status}` | Counter | Runs reaching each terminal status: `completed`, `failed`, `cancelled`, `timed_out` |
| `automaton_runs_active{status}` | Gauge | Runs in non-terminal state: `running`, `pending` |
| `automaton_queue_depth` | Gauge | Steps currently waiting in the step queue |
| `automaton_cron_triggers{enabled}` | Gauge | Registered cron triggers by enabled state (`true`/`false`) |
| `automaton_db_size_bytes` | Gauge | Size of the SQLite database file in bytes |

## Scrape with Prometheus

```yaml
# prometheus.yml
scrape_configs:
  - job_name: automaton
    static_configs:
      - targets: ["localhost:8080"]
    metrics_path: /metrics
```

No `bearer_token` or `tls_config` needed for the metrics endpoint specifically.

## Example output

```
# HELP automaton_runs_total Total runs that have reached each terminal status.
# TYPE automaton_runs_total counter
automaton_runs_total{status="completed"} 142
automaton_runs_total{status="failed"} 3
automaton_runs_total{status="cancelled"} 1
automaton_runs_total{status="timed_out"} 0

# HELP automaton_runs_active Runs currently in a non-terminal state.
# TYPE automaton_runs_active gauge
automaton_runs_active{status="running"} 2
automaton_runs_active{status="pending"} 0

# HELP automaton_queue_depth Number of steps currently waiting in the step queue.
# TYPE automaton_queue_depth gauge
automaton_queue_depth 4

# HELP automaton_cron_triggers Registered cron triggers by enabled state.
# TYPE automaton_cron_triggers gauge
automaton_cron_triggers{enabled="true"} 5
automaton_cron_triggers{enabled="false"} 1

# HELP automaton_db_size_bytes Size of the SQLite database file in bytes.
# TYPE automaton_db_size_bytes gauge
automaton_db_size_bytes 2097152
```

## Grafana

Import any SQLite-friendly Prometheus dashboard, or build your own with panels for:

- Run throughput (`rate(automaton_runs_total[5m])`)
- Active runs gauge
- Queue depth (alert if sustained > 0 with no active workers)
- DB growth (`automaton_db_size_bytes`)
- Failure rate (`rate(automaton_runs_total{status="failed"}[1h])`)

## curl

```bash
curl -s http://localhost:8080/metrics | grep automaton_runs
```
