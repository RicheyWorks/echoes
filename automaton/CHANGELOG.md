# Changelog

All notable changes to automaton-engine are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

Nothing yet.

---

## [0.5.1] — 2026-07-10

### Fixed
- `automaton.__version__` said `0.1.0`; it now matches the package version
  and a test pins it to `pyproject.toml`.
- `release.yml`: the CHANGELOG-extraction step never substituted the version
  into its heredoc, so every GitHub Release body would have read
  "Release $VERSION".
*(all found by the first-ever live execution of CI — the workflows previously
lived under `automaton/.github/` where GitHub Actions never ran them)*

- **Postgres backend:** `_translate()` no longer appends `RETURNING id` to
  INSERTs on tables whose primary key isn't `id` (the `queue` table); SQLite's
  `julianday()` seconds-between idiom is now translated to
  `EXTRACT(EPOCH FROM ...)` — timeout sweep, notify durations, and wait steps
  previously raised `UndefinedFunction` on Postgres.
- **Windows:** `backup.snapshot()` and every `migrate.py` entry point now close
  their SQLite/yoyo connections explicitly (a `with sqlite3.connect(...)` block
  is a transaction scope, not a close) — leaked handles kept DB files locked,
  breaking `restore()` and temp-dir cleanup. String-form `shell` step commands
  no longer retain surrounding quotes after tokenization on Windows.
- **UI server:** early rejections (401/400) on POST/PUT/PATCH drain the unread
  request body before responding, so clients get the status code instead of a
  connection reset.
- **Test suite:** `socket.getfqdn` is patched in `conftest.py` (reverse DNS
  black-holes on GitHub's macOS runners and hung the suite inside yoyo's
  migration logging); macOS deploy-script checks are skipped on Windows;
  `/dev/null` → `os.devnull`; scheduler race test uses a yearly cron so a
  minute-boundary crossing can't double-fire.
- **CI:** `test.yml`'s truncated `twine c` restored to `twine check dist/*`;
  pytest jobs get `timeout-minutes` + `pytest-timeout`; the mobile workflow
  selects the newest stable Xcode dynamically and provisions Gradle directly;
  Android app gained the missing `gradle.properties`, adaptive launcher
  icons, and a platform (non-AppCompat) theme.

---

## [0.5.0] — 2026-05-26

### Added
- **Multi-tenant API key auth** — replaces the single shared `AUTOMATON_TOKEN`
  with a full role-based access model while keeping full backward compatibility:
  - `automaton/migrations/0005-api-keys.sql` — `api_keys` table storing
    `SHA-256(raw_key)` (never the plaintext), with `id`, `name`, `role`,
    `active`, `created_at`, and `last_used_at` columns.
  - `automaton/automaton/auth.py` — key lifecycle module: `generate_key`,
    `hash_key`, `create_api_key`, `revoke_api_key`, `delete_api_key`,
    `list_api_keys`, `authenticate`, and role helpers `role_can_read`,
    `role_can_write`, `role_is_admin`.
  - **Roles**: `admin` (full access, may manage keys), `operator` (all reads +
    write routes), `viewer` (read routes only).
  - **Key format**: `atk_<64 hex chars>` (32 random bytes); plaintext shown
    exactly once at creation, never stored.
  - **`_get_role()` in `ui.py`** — resolves any Bearer token to a role string:
    checks `AUTOMATON_TOKEN` first (always "admin"), then DB lookup with
    `touch_last_used`. POST handler now returns 403 (not 401) when the caller
    is authenticated but lacks write permission.
  - **Key management HTTP API** (admin-only):
    - `GET  /api/keys`                — list all keys (no `key_hash` field)
    - `POST /api/keys`                — create key; returns plaintext once
    - `DELETE /api/keys/<name_or_id>` — revoke key
  - **Key management CLI** (`automaton key`):
    - `automaton key create <name> [--role operator|viewer|admin]`
    - `automaton key list`
    - `automaton key revoke <name_or_id>`
  - `tests/test_multitenancy.py` — 37 integration tests covering all roles,
    revoked/unknown tokens, `last_used_at` tracking, key API round-trips, and
    401 vs 403 distinctions.
- **Option C: automaton as `echoes` durable store** — automaton now acts as
  a remote persistence backend for echoes agents running on any machine:
  - `automaton/migrations/0004-agents.sql` — adds `agent` and `agent_memory`
    tables with a `UNIQUE (agent_name, tick)` constraint and `ON DELETE CASCADE`
    for clean agent removal.
  - `automaton/automaton/agents.py` — six functions (`get_agent`,
    `upsert_agent`, `append_entry`, `get_entries`, `list_agents`,
    `delete_agent`) operating inside `db.transaction()` blocks for
    atomicity.
  - **Agent HTTP API** (`ui.py`) — five new routes authenticated with the
    existing Bearer-token model:
    - `GET  /api/agents` — list all agents (name, goal, tick, updated_at)
    - `GET  /api/agents/<name>/meta` — one agent's metadata row
    - `POST /api/agents/<name>/meta` — upsert agent row (`{goal, tick}`)
    - `GET  /api/agents/<name>/entries` — all memory entries ordered by tick
    - `POST /api/agents/<name>/entries` — append one entry; returns 409 on
      duplicate tick
  - **`AgentStore` trait in echoes** (`store.rs`) — both backends implement
    `save_agent_meta`, `load_agent_meta`, `save_entry`, `load_entries`:
    - `SqliteStore` (formerly `Store`) — unchanged local-first behaviour;
      `pub type Store = SqliteStore` keeps existing call-sites working.
    - `RemoteStore` — HTTP backend that POSTs entries to automaton's
      `/api/agents/*` API via the `ureq` sync HTTP client (no async runtime
      required).  Supports resume: `load_entries` fetches the existing chain
      so `echoes run` continues from where it left off even on a different
      machine.
  - **`--remote-store URL --token TOKEN`** flags on `echoes run` — when
    present, a `Box<dyn AgentStore>` is dispatched to `RemoteStore` instead
    of the local SQLite file.  `--token` also reads `AUTOMATON_TOKEN` from
    the environment.  The `verify` and `report` subcommands remain
    SQLite-only (forensic integrity checks require local data).
  - `tests/test_agents.py` — 26 HTTP-level tests covering CRUD lifecycle,
    idempotent meta upsert, entry ordering, duplicate-tick 409, auth
    enforcement on both GET and POST routes, and the full end-to-end
    lifecycle.
- **Real event sources in `echoes` (Phase 4c)** — `sensor.rs` adds:
  - `EventSource` trait — non-blocking `poll() -> Option<SecurityEvent>`;
    any implementor can be passed to `agent.think_with(Some(&mut source))`.
  - `FileWatcher` — wraps the `notify` crate (inotify / kqueue /
    ReadDirectoryChangesW) to emit `SecurityEvent::FileAccess` on real
    filesystem activity. Enable with `--features watch`; without the feature
    the binary compiles and runs normally with synthetic events.
  - `ProcessScanner` — reads `/proc/<pid>/comm` (Linux) or `ps -eo pid,comm`
    (macOS) each tick; emits `SecurityEvent::ProcessExecution` for any PID
    seen since the last scan. No-op stub on other platforms.
  - `CompositeSource` — chains multiple `EventSource` implementations;
    returns the first event found on each `poll()`.
  - `agent::Agent::think_with<S: EventSource>()` — generically-dispatched
    variant of `think()`; `think()` delegates to it using `None` (zero-cost,
    no behaviour change for existing callers).
  - CLI flags `--watch PATH` and `--procs` on the `run` subcommand wire
    the sources into the tick loop.
  - `echoes.yml` GitHub Actions workflow — `cargo test` on Linux + macOS,
    `cargo test --features watch`, `cargo clippy -D warnings`.
- **Postgres backend (Phase 6)** — set `AUTOMATON_DB_URL=postgresql://...` to
  run against Postgres instead of SQLite.  The new `automaton.pg` module
  provides `PgConn`, a thin adapter that presents the same `conn.execute()`
  interface as `sqlite3.Connection`: paramstyle, date functions, and
  `lastrowid` are translated transparently so `engine.py` is unchanged.
  Queue leasing uses `SELECT … FOR UPDATE SKIP LOCKED` for true multi-worker
  concurrency without optimistic-lock retries.  Schema is applied by
  `pg.migrate(conn)` — a single idempotent `CREATE TABLE IF NOT EXISTS` script
  (no yoyo dependency on the Postgres path).  Install the extra:
  `pip install 'automaton-engine[postgres]'`.
- **`db.open_store(url)`** — routing factory that returns a `SQLiteConn` or
  `PgConn` based on the URL prefix.  Falls back to `AUTOMATON_DB_URL` env var,
  then `AUTOMATON_DB`, then `automaton.db`.
- **`echoes_agent` step type** — built-in step that invokes the `echoes`
  binary (auto-discovered from PATH or the sibling `echoes-v1.0-forensic/`
  directory).  Supports `action: run | verify | report`.  Parses Merkle root,
  tick count, and integrity status from output; stores structured JSON in the
  step's `output_json`.
- **`templates/agent/echoes-daily.yaml`** — workflow template for a daily
  forensic agent run: advance → verify integrity → store JSON report.
- **Tailscale reachability card in the UI** — the run-list homepage now shows
  a compact "Connected to Tailscale" card when the daemon is running and logged
  in. Displays the MagicDNS URL (e.g. `https://host.tailnet.ts.net`), Tailscale
  IP, peer count, and a one-click copy button. Hidden automatically on
  local-only deployments where Tailscale isn't installed.
- **`automaton mesh status` quick-access section** — when Tailscale is healthy,
  `automaton mesh status` now prints an "access URL" block with the browser URL
  and the `tailscale serve` one-liner to enable HTTPS via Let's Encrypt.
- **Startup URL hint in `automaton serve`** — if Tailscale is running at
  startup, the serve command prints the Tailscale URL alongside the local one
  so you can open it on your phone immediately.
- **`mesh.cached_status()`** — 60-second TTL cache on the `tailscale status`
  subprocess call, so the UI card adds zero latency on cache-warm requests.
- **`deploy/mesh/README.md` Quick path** — new section at the top of the mesh
  README with a three-command sequence to get HTTPS remote access in under five
  minutes.

### Security
- **Read-route auth** — all GET routes (UI dashboard, `/api/*`, `/metrics`) now
  require `Authorization: Bearer <AUTOMATON_TOKEN>` when `AUTOMATON_TOKEN` is
  set. Previously only write (POST) routes were protected, making it unsafe to
  expose the UI over a mesh network without a reverse proxy.  
  **Always-open routes** (no token required): `/healthz`, `/health`,
  `/manifest.json`, `/sw.js` — liveness checks and PWA assets continue to work
  unauthenticated.  
  **Browser bookmarks**: `?token=<TOKEN>` query-string fallback still works on
  GET requests for bookmarking convenience; note it leaks the token into server
  logs.  
  **New flag `--insecure-read-no-auth`**: disables auth on read routes only
  while keeping write routes protected — useful for local Prometheus scrapers
  that can't send headers.  
  **Migration**: if you scrape `/metrics` without a token, add
  `--insecure-read-no-auth` to your `automaton serve` invocation, or set
  `AUTOMATON_TOKEN` and configure your scraper to send the header.

---

## [0.4.0] — 2026-05-23

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
  show a Re-run button in the run-detail page. Clicking it POSTs to
  `POST /api/trigger/<workflow>` with the original trigger payload and
  redirects to the new run.
- **`automaton inspect` filter flags** — `--status`, `--workflow`,
  `--after`, `--before`, `--limit` narrow the CLI run listing via
  `search_runs()`.
- **`foreach` step type** — fan-out execution: runs a nested step spec
  once per item in a list. Supports `${{ item }}` and `${{ item_index }}`
  template references inside the nested spec. `fail_fast: true` (default)
  stops on the first failure; `fail_fast: false` collects all results and
  raises at the end. Output includes per-item results, total count, and
  failed count.
- Docker deployment: multi-stage `Dockerfile` (builder + slim runtime),
  `docker-compose.yml` with worker / scheduler / ui services, health-check
  on `/healthz`, named volume for the SQLite file.
- PyPI packaging: `automaton-engine` distribution name, full classifiers,
  project URLs, `py.typed` marker (PEP 561).

---

## [0.3.0] -- 2026-05-22

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

## [0.2.0] -- 2025-06-01

### Added
- **`timed_out` run status** — `timeout_seconds` column on workflow spec,
  `reap_timed_out_runs()` in the scheduler, `AUTOMATON_NOTIFY_ON_TIMEOUT`
  env var.
- **Schema migrations** via yoyo-migrations — `automaton migrate` applies
  all pending SQL files from `automaton/migrations/` in order.

---

<!-- version comparison links (Keep a Changelog convention) -->
[Unreleased]: https://github.com/richmond/echoes/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/richmond/echoes/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/richmond/echoes/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/richmond/echoes/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/richmond/echoes/releases/tag/v0.2.0
