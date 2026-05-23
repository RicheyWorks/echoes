# Changelog

All notable changes to automaton-engine are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- **`python` step type** — execute any callable in a dotted module path
  (`module: my_package.tasks`, `function: process_data`). `print()` output
  is captured and stored in the step's output as `stdout`; the function's
  return value (JSON-serialised, or `repr()` for non-serialisable objects)
  is stored as `return_value`. `stderr` is also captured when non-empty.
- **Step output parsed in `run_detail()`** — `GET /api/run/<id>` now
  returns each step's `output` and `error` as structured dicts instead of
  raw JSON strings. Consumers no longer need to call `json.loads()`.
- **Intelligent step output display in the web UI** — the run-detail page
  now renders shell stdout/stderr as labelled blocks with exit-code badges,
  HTTP responses with status-code badges and scrollable bodies, `python`
  step return values and stdout, and `file_append` write/no-op indicators.
- **Workflow YAML editor in the web UI** — `GET /workflows` lists every
  registered workflow (name, version, timeout) as cards with a one-click
  trigger button. A built-in editor lets you paste or hand-write a YAML
  workflow spec and register it immediately via `POST /api/workflows`
  without restarting the server.
- **`engine.list_workflows(conn)`** — returns the latest version of every
  named workflow definition with the spec parsed from JSON, used by the
  new `/workflows` page.
- **Workflow templates library** (`templates/`) — 10 curated starter YAMLs
  across six categories (backup, health, dev, infra, media, agent,
  personal). Each ships with a structured comment header (description,
  required secrets/env, cron, last-verified date).
- **`automaton init [NAME] [--template SLUG]`** — copies a template into
  the current directory, prints required secrets and suggested cron, then
  prompts the next step (`automaton register`). `--list` or omitting
  `--template` prints all available templates.
- **`templates/INDEX.md`** — auto-generated catalog of all templates with
  one-line descriptions; verified fresh by a CI job on every PR.
- **CI template validation** (`validate-templates` job) — every template
  is run through `validate_spec` and INDEX.md staleness is checked on
  every push.
- **Run search and filter** — `engine.search_runs()` filters by status,
  workflow name, and date range. The runs list page (`GET /`) now has a
  filter bar (status dropdown, workflow name input, after/before date
  pickers) that drives query-string-parameterised searches.
- **Re-run button** — completed, failed, timed-out, and cancelled runs
  show a ↺ Re-run button in the run-detail page. Clicking it POSTs to
  `POST /api/trigger/<workflow>` with the original trigger payload and
  redirects to the new run.
- **`automaton inspect` filter flags** — `--status`, `--workflow`,
  `--after`, `--before`, `--limit` narrow the CLI run listing via
  `search_runs()`.

- Docker deployment: multi-stage `Dockerfile` (builder + slim runtime),
  `docker-compose.yml` with worker / scheduler / ui services, health-check
  on `/healthz`, named volume for the SQLite file.
- PyPI packaging: `automaton-engine` distribution name, full classifiers,
  project URLs, `py.typed` marker (PEP 561).

---

## [0.3.0] — 2026-05-22

### Added
- **Prometheus `/metrics` endpoint** — `GET /metrics` returns Prometheus
  text format 0.0.4 with five metric families: `automaton_runs_total`,
  `automaton_runs_active`, `automaton_queue_depth`, `automaton_cron_triggers`,
  `automaton_db_size_bytes`. No auth required (mirrors `/healthz` convention).
- **Android client app** (`deploy/android/`) — Jetpack Compose app with
  OkHttp + kotlinx.serialization, three-tab navigation (Runs, Workflows,
  Settings), SHA-256 cert pinning, `EncryptedSharedPreferences` token
  storage, `WorkManager` background polling.
- **iOS client app** (`deploy/ios/`) — SwiftUI app with async/await
  URLSession, Keychain token storage, three-tab navigation, cert pinning,
  `wait_for_signal` responder UX.
- **Browser-local timezone display** — all timestamps in the web UI now
  render in the visitor's local timezone via a lightweight JS snippet;
  original UTC value shown in parentheses.
- **`automaton scheduler next`** debug command — prints the next N fire
  times for a cron trigger in both the configured timezone and UTC.
- **Per-migration data-preservation tests** — `step_migrations` pytest
  fixture applies real SQL migration files incrementally; two tests guard
  that existing rows survive `0002` (timezone column) and `0003`
  (timed_out status + table recreation).

### Changed
- Version bumped to 0.3.0.

---

## [0.2.0] — 2025-06-01

### Added
- **`timed_out` run status** — `timeout_seconds` column on workflow spec,
  `reap_timed_out_runs()` in the scheduler, `AUTOMATON_NOTIFY_ON_TIMEOUT`
  env var.
- **Schema migrations** via yoyo-migrations — `automaton migrate` applies
  SQL files in `automaton/migrations/`; `AUTOMATON_AUTO_MIGRATE` for
  automatic startup migration.
- **Time / timezone correctness** — per-cron `timezone:` field (IANA),
  all next-fire timestamps stored as UTC, `croniter` DST-safe mode.
- **Web UI mobile responsiveness + PWA shell** — Tailwind CDN, card +
  table responsive layouts, `manifest.json`, service worker, SSE live
  updates on run-detail, `?token=` query-string auth for browser use.
- **Workflow templates library** (`templates/`) — 10 starter YAMLs
  (backup, health, dev, infra, personal, agent-loop), `automaton init
  --template` subcommand, `templates/INDEX.md`.
- **Load tests** (`tests/load/`) — steady-state, burst, long-tail scripts;
  `docs/scale.md` documents the measured operating envelope.
- **Secrets management** — `automaton secret set/get/rm/ls/import`
  subcommands backed by `keyring`; `${secret:NAME}` spec references.
- **Notifications** — Apprise integration, `AUTOMATON_NOTIFY_ON_FAILURE`,
  quiet hours, `automaton notify test` self-check.
- **Backup & restore** — Litestream config template, `automaton restore`,
  `PRAGMA integrity_check` on snapshot, CI restore drill.
- **macOS host** (`deploy/macos/`) — launchd plists, install/uninstall
  script, path conventions.
- **Windows host** (`deploy/windows/`) — NSSM service wrapper, PowerShell
  install script, path conventions.
- **TLS** — `automaton serve --tls-cert --tls-key`, `automaton tls init`
  self-signed cert helper, HSTS header.
- **Mesh networking guide** (`deploy/mesh/`) — Tailscale + Headscale setup
  for cross-device access.
- **HTTP write API** — `POST /api/workflows`, `/api/trigger/NAME`,
  `/api/crons`, `/api/signals`, `/api/cancel`. Bearer token auth.
- **Structured logging** — JSON or text format; `AUTOMATON_LOG_FORMAT`,
  `AUTOMATON_LOG_LEVEL`, `AUTOMATON_LOG_FILE`.
- **Plugin step types** via entry points (`automaton.step_types`).
- **Workflow signals** — `wait_for_signal` step type, `POST /api/signals`.
- **Retry policy** — per-step `retry:` with `max` and `backoff`.
- **Webhook trigger** — HMAC-signed receiver, `automaton webhook add`.
- **Cancel** — `automaton cancel <run_id>`, `POST /api/cancel`.
- **Prune** — `automaton prune --before DAYS`.

### Changed
- `automaton backup` now runs `PRAGMA integrity_check` and aborts on
  corruption rather than silently copying a bad file.

---

## [0.1.0] — 2025-01-15

### Added
- Initial release.
- SQLite state store with WAL mode, exactly-once step execution via
  idempotency keys (`sha256(run_id + step_name + attempt)`).
- Step types: `shell`, `http_get`, `file_append`, `python`.
- Cron scheduler with single-leader election via DB row lock.
- Worker with lease-based queue, crash recovery, configurable timeout.
- `automaton` CLI: `register`, `trigger`, `worker`, `scheduler`, `serve`,
  `inspect`, `schedule`, `backup`.
- Web dashboard — runs list, run detail, workflows list, cron list.
- Bearer token auth on write routes; `/healthz` open.
- 68 tests.

[Unreleased]: https://github.com/RicheyWorks/echoes/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/RicheyWorks/echoes/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/RicheyWorks/echoes/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RicheyWorks/echoes/releases/tag/v0.1.0
- `automaton backup` now runs `PRAGMA integrity_check` and aborts on
  corruption rather than silently copying a bad file.

---

## [0.1.0] — 2025-01-15

### Added
- Initial release.
- SQLite state store with WAL mode, exactly-once step execution via
  idempotency keys (`sha256(run_id + step_name + attempt)`).
- Step types: `shell`, `http_get`, `file_append`, `python`.
- Cron scheduler with single-leader election via DB row lock.
- Worker with lease-based queue, crash recovery, configurable timeout.
- `automaton` CLI: `register`, `trigger`, `worker`, `scheduler`, `serve`,
  `inspect`, `schedule`, `backup`.
- Web dashboard — runs list, run detail, workflows list, cron list.
- Bearer token auth on write routes; `/healthz` open.
- 68 tests.

[Unreleased]: https://github.com/RicheyWorks/echoes/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/RicheyWorks/echoes/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/RicheyWorks/echoes/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RicheyWorks/echoes/releases/tag/v0.1.0
