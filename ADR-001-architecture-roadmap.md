# ADR-001: Architecture Audit & Phased Roadmap

**Status:** Implemented (Phases 1–6 complete) — continued in [ADR-002](ADR-002-operational-roadmap.md)  
**Date:** 2026-05-23  
**Updated:** 2026-07-14  
**Decider:** Richmond (sole sign-off)  
**Scope:** Both sub-projects — `automaton` (Python automation engine) and `echoes` (Rust agent integrity framework)

---

## Context

The `echoes` repository currently hosts two distinct but related projects:

- **`automaton`** — a strongly consistent personal automation engine (Python, PyPI: `automaton-engine`). Currently at v0.3.0 with v0.4.0 unreleased. Implements triggers → DAG workflow → exactly-once step execution backed by SQLite WAL.
- **`echoes`** — a Rust multi-agent simulation framework exploring cryptographic memory integrity. Currently at v1.0, having evolved through hash chains (v0.6), Merkle trees (v0.9), and structured forensic events (v1.0).

Both projects have reached a meaningful inflection point. `automaton` has completed its core consistency story and has scaffolding across Linux, macOS, Windows, iOS, and Android — but not all of it is production-grade yet. `echoes` has proven out its cryptographic primitives in simulation but has no persistence, no network layer, and no integration with `automaton`.

This ADR documents what's actually done, what's partial, what the gaps are, and the recommended sequence of phases to advance both projects.

---

## Current State Audit

### automaton — What's genuinely done

| Area | Status | Evidence |
|---|---|---|
| Core consistency (exactly-once, single-leader) | ✅ Solid | `test_consistency.py`, `test_scheduler.py` |
| SQLite WAL state store | ✅ Solid | `db.py`, `schema.sql`, WAL confirmed in tests |
| Retry with backoff (fixed + exponential) | ✅ Done | `test_retries.py` — 5 tests, rotating idempotency keys |
| Webhooks (signed) | ✅ Done | `webhooks.py`, `test_plugins.py` |
| Workflow signals (park + resume) | ✅ Done | `engine.py`, `test_cancel_and_validate.py` |
| Metrics (Prometheus `/metrics`) | ✅ Done | `metrics.py`, 11 tests |
| Notifications (Apprise — 110+ backends) | ✅ Done | `notify.py`, `test_notify.py`, quiet hours, urgent flag |
| Secrets management (OS keychain / encrypted file) | ✅ Done | `secrets.py`, keyring + keyrings.alt |
| TLS cert generation (self-signed, 825d) | ✅ Done | `tls.py`, `cryptography` extra |
| Tailscale mesh status helper | ✅ Done | `mesh.py`, `test_mesh.py` |
| Plugin system (entry-point step types) | ✅ Done | `steps.py`, `test_plugins.py` |
| Backup + restore (online snapshot + Litestream docs) | ✅ Done | `backup.py`, BACKUP.md |
| Prune (old runs / event log) | ✅ Done | `prune.py`, `test_prune.py` |
| Foreach / fan-out | ✅ Done | `test_foreach.py` exists |
| Linux systemd deployment | ✅ Done | `deploy/systemd/` |
| macOS launchd deployment | ✅ Artifacts done | `deploy/macos/` — plists, `install.sh`, Homebrew formula |
| Windows Service deployment | ✅ Artifacts done | `deploy/windows/install.ps1` |
| Mesh config (Tailscale ACLs + Headscale) | ✅ Done | `deploy/mesh/` — ACL JSON, Headscale YAML |
| Python step type | ✅ Done | Released in v0.4.0 |
| Workflow YAML editor in UI | ✅ Done | Released in v0.4.0 |
| 10 workflow templates | ✅ Done | Released in v0.4.0 |
| Auth on read routes | ✅ Done | Phase 2 complete — GET routes require Bearer token; `--insecure-read-no-auth` flag available |
| iOS client | ✅ Done | `deploy/ios/` — SwiftUI, URLSession, Keychain, cert pinning, README |
| Android client | ✅ Done | `deploy/android/` — Compose, OkHttp, EncryptedSharedPrefs, full build infra |
| Postgres backend | ✅ Done | `automaton/pg.py` — PgConn adapter, `SKIP LOCKED` lease, `open_store()` |
| Multi-machine deployment | ✅ Done (Postgres path) | `AUTOMATON_DB_URL=postgresql://...` enables multi-worker |

### echoes — What's genuinely done

| Area | Status | Notes |
|---|---|---|
| Hash-chain memory integrity | ✅ Solid | SHA-256 chain with `prev_hash` linking, genesis zero-hash |
| Merkle tree + proofs | ✅ Solid | Full binary tree build, proof generation + verification |
| Structured forensic events | ✅ Done | `SecurityEvent` enum: NetworkConnection, FileAccess, Authentication, ProcessExecution |
| Tamper detection | ✅ Done | `verify_integrity()` + Merkle root shift both catch modification |
| Immutable goal after creation | ✅ Done | `goal` field private, no public setter |
| Append-only audit log | ✅ Done | Separate `AuditEntry` vec, sequential tick ordering |
| Unit tests | ✅ Done | 5 tests cover integrity, tamper, linking, Merkle proofs |
| Persistence | ✅ Done (Phase 4) | SQLite-backed `store.rs`; integrity verified on load |
| Network layer | ✅ Done (Phase 5) | Invoked as subprocess via `echoes_agent` step type |
| Multi-agent simulation | 🔶 Partial | `agent_sim/` directory exists but not deeply explored |
| Integration with automaton | ✅ Done (Phase 5) | `echoes_agent` step type; `templates/agent/echoes-daily.yaml` |

---

## Gap Analysis — What's Actually Blocking Progress

Three gaps are genuinely load-bearing right now; the rest are additive features.

**Gap 1 — Read-route auth in automaton.** The write API (POST /api/*) requires a Bearer token. The inspection UI and `/metrics` endpoint do not. This is safe on localhost but makes it impossible to safely expose the UI over Tailscale without a reverse proxy in front. Every other phase that involves remote access depends on closing this gap.

**Gap 2 — v0.4.0 not released.** The Python step type, UI editor, and 10 templates are implemented and sitting in `[Unreleased]`. Until this ships, PyPI users on v0.3.0 can't use features that are already written. Every demo or live test should be on the published version.

**Gap 3 — echoes has no persistence.** All cryptographic integrity work is lost when the process exits. The research is correct and the primitives are clean, but the project can't be used for any real monitoring or security task until memory survives restart. This is the single decision point that determines whether echoes becomes a real tool or stays a proof of concept.

---

## Options Considered for the Integration Question

The central architectural decision is whether `automaton` and `echoes` converge or stay separate forever.

### Option A: Keep them separate, develop independently

Both projects continue as distinct tools. `automaton` gets production-hardened. `echoes` gets persistence and a network layer, but as a standalone Rust binary/library.

| Dimension | Assessment |
|---|---|
| Complexity | Low — no cross-language concerns |
| Maintenance | Two separate release cycles, two test suites |
| Capability | Each project can go deep in its own domain |
| Team fit | One person; two independent things to context-switch between |

**Pros:** Clean separation of concerns. Rust binary can be hyper-efficient for forensic event capture. No FFI complexity.  
**Cons:** Misses the obvious synergy: `echoes` generates cryptographically-attested audit trails; `automaton` runs workflows that need exactly that. You'd eventually want them to talk and would have to build the bridge anyway.

### Option B: echoes becomes an automaton step type

`echoes` is compiled to a binary that `automaton` invokes as a `shell` step or a dedicated `echoes_agent` step type. The agent runs, emits its Merkle-rooted audit log, and automaton records the result. The bridge is stdout/JSON.

| Dimension | Assessment |
|---|---|
| Complexity | Low — subprocess boundary, JSON contract |
| Integration effort | Low — 1-2 days to wrap the binary as a step type |
| Coupling | Loose — echoes stays independent, automaton consumes its output |
| Persistence | automaton's SQLite carries the output; echoes itself stays stateless per-run |

**Pros:** Immediate value. Automaton gets cryptographic auditability for any workflow that uses an echoes agent step. No FFI. echoes can still run standalone.  
**Cons:** echoes can't maintain memory across automaton workflow runs without external help (would need to read/write a sidecar file). Short-circuit for short-lived forensic tasks, not long-running agents.

### Option C: automaton becomes echoes' persistence layer

`echoes` is extended with an HTTP client that stores MemoryEntries by posting to `automaton`'s write API after each tick. Automaton holds the durable memory; echoes holds the live in-flight state.

| Dimension | Assessment |
|---|---|
| Complexity | Medium — echoes needs an async HTTP client, a serialization contract |
| Integration effort | Medium — 1 week |
| Coupling | Medium — echoes depends on automaton being up |
| Capability | High — you get persistent, tamper-evident agent memory with zero additional infrastructure |

**Pros:** Uses automaton's consistency story (exactly-once, linearizable) for echoes memory. The SQLite event log becomes an audit ledger for agent actions. Backup and restore work for free.  
**Cons:** echoes gains a network dependency. Running echoes standalone for testing or simulation requires mocking the persistence layer.

**Recommended:** Start with Option B (step type bridge). It delivers value immediately, costs almost nothing, and doesn't foreclose Option C. If long-running persistent agents become important, layer Option C on top later — the JSON contract established in B becomes the serialization format for C.

---

## Decision

Proceed in six sequential phases. Phases 1-3 are `automaton`-only and unblock the live deployment. Phase 4 advances `echoes` to a real tool. Phase 5 is the first integration point. Phase 6 is the speculative Postgres/multi-machine path.

---

## Phased Roadmap

### Phase 1 — Ship v0.4.0 and do the live test ✅ COMPLETE

The work is done. The only thing remaining is release packaging and first deployment.

**Deliverables:**
- Move `[Unreleased]` CHANGELOG items into `[0.4.0]` section with date.
- Bump `pyproject.toml` to `version = "0.4.0"`.
- Run `python -m pytest tests/ -q` — all pass.
- Tag `v0.4.0`, push, let the GitHub Actions release workflow publish to PyPI.
- Execute the LIVE-TEST-READINESS quick test plan on real hardware (Days 1–7).
- After Day 7: snapshot findings, write a one-page postmortem-style retro.

**Success criteria:** `pip install automaton-engine` gives v0.4.0. The heartbeat workflow runs for 7 days without data loss across at least one intentional worker kill.

**Nothing new to build** — this is entirely about shipping what's already written.

---

### Phase 2 — Auth on read routes ✅ COMPLETE

This is the single open security gap. It must close before any remote access or mobile work.

**What to build:**
- Apply Bearer token check to `GET /api/*`, `GET /metrics`, and the UI dashboard pages. The token is already in `AUTOMATON_TOKEN`; the write-path middleware already validates it — extend it to reads.
- Add `--insecure-read-no-auth` flag (parallel to the existing `--insecure-no-auth`) for developers running locally who want to browse without the token. Default: auth required.
- Update `deploy/mesh/README.md` to remove the caveat about needing a reverse proxy for read protection.
- Add one test in `test_cli_run.py` or `test_metrics.py` that confirms `/metrics` returns 401 without a token.

**ADR note:** This is a breaking change for any tool that scrapes `/metrics` without a token today. Document it in CHANGELOG as "Security: read routes now require auth."

**Success criteria:** `curl http://localhost:8080/metrics` returns `401 Unauthorized`. `curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/metrics` returns 200.

---

### Phase 3 — Remote access end-to-end ✅ COMPLETE

With auth closed, Tailscale access from mobile/laptop becomes safe to enable.

**What to build:**
- Wire `automaton mesh status` command to surface the Tailscale IP and magic DNS name (the `mesh.py` helper already fetches this — just surface it in the CLI and the UI dashboard header).
- Add a "Reachability" card to the UI homepage: shows Tailscale IP, magic DNS, peer count, and a copy button for the URL.
- Document the Tailscale Serve one-liner (`tailscale serve https / http://localhost:8080`) in `deploy/mesh/README.md` under a "Quick path" heading. This gets a real Let's Encrypt cert for the Tailscale hostname with zero extra work.
- Test: add an integration test that starts `automaton serve`, hits it via the mesh helper's advertised IP loopback, and confirms the auth gate is in place.

**What NOT to build yet:** native iOS/Android apps. The mobile story at this point is "open Safari/Chrome on your phone and hit the Tailscale URL." That's good enough and avoids App Store/Play Store friction while the UI is still evolving.

**Success criteria:** On a phone connected to the same Tailnet, the automaton dashboard loads at `https://automaton-host.your-tailnet.ts.net` with a valid cert and requires the Bearer token to log in.

---

### Phase 4 — echoes: persistence + real forensic use ✅ COMPLETE

`echoes` transitions from simulation to a usable forensic agent. This is a Rust-only phase.

**What to build:**

**4a — Persistence layer (1 week):** Add a `store.rs` module backed by a SQLite file (via `rusqlite`). On each `agent.think()` call, serialize the `MemoryEntry` (tick, action, event, note, hash, prev_hash) to a row. On startup, reconstruct the agent's memory from the DB and verify `verify_integrity()` before accepting any new entries. If integrity fails on load, refuse to run and emit a clear error: "stored memory chain is corrupt — possible tamper."

**4b — CLI interface (3–5 days):** Add a minimal CLI (`echoes run`, `echoes verify`, `echoes report`). `echoes run` runs the agent for N ticks and saves to the DB. `echoes verify` loads the DB and runs `verify_integrity()` + Merkle root check, printing pass/fail. `echoes report` dumps the memory chain and audit log as JSON to stdout.

**4c — Real event sources (1 week):** Replace the `SecurityEvent::Custom("...")` stubs with actual data. Wire `FileAccess` to `inotify` (Linux) / `kqueue` (macOS) / ReadDirectoryChangesW (Windows) via the `notify` crate. Wire `ProcessExecution` to a periodic `/proc` scan (Linux) or `sysctl` (macOS). `NetworkConnection` and `Authentication` can stay as manually-logged events for now — those require elevated privileges that complicate deployment.

**4d — JSON output contract:** `echoes report --json` emits a document with: `agent`, `goal`, `entries` (count), `merkle_root` (hex), `integrity` ("ok"/"failed"), `memory` (array of entries). This becomes the contract for Phase 5 integration with automaton. *(Wording corrected 2026-07-14 to match the shipped contract — see known issue 6.)*

**Success criteria:** `echoes run --ticks 100 --db ./echo.db && echoes verify --db ./echo.db` exits 0 and prints "Integrity: PASSED" after the process has been killed and restarted mid-run. `echoes report --json` emits valid JSON that another tool can parse.

---

### Phase 5 — Integration: echoes as an automaton step type ✅ COMPLETE

After Phase 4, `echoes` is a real binary. Wrapping it as an automaton step takes one short pass.

**What to build:**

Add a built-in `echoes_agent` step type to `automaton/steps.py` (or as a first-party plugin in a separate `automaton-echoes` package):

```yaml
- name: monitor_filesystem
  type: echoes_agent
  ticks: 50
  db: /var/lib/echoes/monitor.db
  report: true          # if true, step output includes the JSON report
```

The step handler:
1. Invokes `echoes run --ticks N --db PATH` as a subprocess.
2. If `report: true`, invokes `echoes report --json --db PATH` and stores the parsed JSON as the step's `output_json`.
3. The Merkle root and `integrity_ok` fields land in automaton's event log — giving you cryptographically-attested, tamper-evident records inside automaton's linearizable history.

**Why this is valuable:** Any automaton workflow that touches sensitive files or external systems can sandwich its steps between `echoes_agent` monitor calls. The before/after Merkle roots in the run history let you prove (or disprove) that the filesystem was in a clean state when the workflow executed.

**Success criteria:** A workflow YAML with an `echoes_agent` step registers, triggers, runs, and produces a completed step with `output_json` containing `merkle_root` and `integrity_ok: true`. The automaton UI displays the structured output with the Merkle root hash visible.

---

### Phase 6 — Postgres backend + multi-machine + native mobile clients ✅ COMPLETE

Do this only if you need more than one worker machine or find SQLite's single-writer bottleneck in load testing.

**Trigger conditions (do Phase 6 when any of these are true):**
- `test_load_regression.py` shows sustained latency > 500ms at your expected workflow rate.
- You want to run workers on two different machines pointing at the same store.
- The iOS/Android native clients need a backend that can handle concurrent reads from multiple devices without WAL contention.

**What to build:**
- Abstract the DB layer behind a `Store` protocol in `db.py`. SQLite and Postgres implementations both satisfy it.
- Implement `PostgresStore` using `psycopg3`. Translate the queue lease (`UPDATE ... WHERE leased_by IS NULL OR leased_until < now()`) to `SELECT ... FOR UPDATE SKIP LOCKED`. That idiom is designed exactly for this pattern.
- Add a `AUTOMATON_DB_URL` env var. If it starts with `postgresql://`, use the Postgres store; otherwise, use SQLite.
- Run the full test suite against both backends in CI (one SQLite job, one Postgres job via the `services:` key in the GitHub Actions workflow).

**What does NOT change:** The consistency model, idempotency story, leader election, and worker protocol are identical. The SQL is almost identical. This is a backend swap, not a redesign.

**Mobile native clients (iOS + Android):** The scaffolding in `deploy/ios/` (Package.swift) and `deploy/android/` (Gradle) can be fleshed out once the UI is stable. The minimum viable native client is: list recent runs, show run detail, trigger a workflow by name. That's three screens and six API calls — a weekend of SwiftUI/Compose work once the API contract is locked.

---

## Trade-off Summary

| Phase | Effort | Risk | Unlocks |
|---|---|---|---|
| 1 — Ship v0.4.0 + live test | Low — already built | Low | Real deployment data, PyPI users on latest |
| 2 — Read-route auth | Low (3-5 days) | Low — one middleware change | Safe remote access |
| 3 — Remote access UX | Low (1 week) | Low | Mobile browser access, mesh reachability card |
| 4 — echoes persistence + CLI | Medium (2-4 weeks) | Medium — first real Rust I/O work | echoes becomes a real tool |
| 5 — Integration (step type) | Low (1 week) | Low — subprocess boundary | Cryptographic auditability in automaton workflows |
| 6 — Postgres + mobile apps | High (4-8 weeks) | Medium | Multi-machine, native mobile clients |

---

## Consequences

**What becomes easier after these phases:**
- Phases 1–3 make the system safe and reachable from anywhere on your mesh — the goal of the original platform expansion plan.
- Phase 4 turns `echoes` from an academic proof-of-concept into a tool you actually run. The cryptographic work done in v0.6–v1.0 is solid; it just needs to be wired to disk and a process boundary.
- Phase 5 gives `automaton` a unique capability no off-the-shelf workflow engine has: cryptographically-attested, tamper-evident step audit trails built into the run history.

**What becomes harder:**
- Each phase that adds surface area (remote access, native apps, Postgres) adds operational complexity. The single-SQLite-file simplicity that makes backup trivial gets harder to maintain once Postgres enters the picture.
- Phase 4's real event sources (inotify, kqueue) make `echoes` platform-specific. Plan for `#[cfg(target_os = "linux")]` gating and a stub fallback for Windows CI.

**What to revisit later:**
- Option C (automaton as echoes' persistence layer) — revisit after Phase 5 ships and you have a sense of whether short-lived per-workflow forensic snapshots are enough or whether you want continuous long-running agent memory.
- Multi-tenant access control — if anyone other than you ever runs `automaton`, the single-token auth model needs a proper user/role layer.
- WASM build of `echoes` — the Merkle tree and hash chain code is pure computation with no OS dependencies. A WASM build could run in the browser or in a JS/TS environment, making the integrity primitives portable to the mobile clients.

---

## Action Items

- [x] **Phase 1:** CHANGELOG updated, version bumped to 0.4.0, PyPI package published. 729 tests passing.
- [x] **Phase 2:** Bearer token auth added to all GET routes in `ui.py`. `--insecure-read-no-auth` flag available for Prometheus scrapers. Tests in `test_metrics.py` confirm 401 without token, 200 with.
- [x] **Phase 3:** `automaton mesh status` surfaces Tailscale URL + IP + peer count. UI dashboard shows Tailscale reachability card. `deploy/mesh/README.md` updated with Tailscale Serve quick path. Startup URL hint added to `automaton serve`.
- [x] **Phase 4a:** `rusqlite` added to `echoes/Cargo.toml`. `src/store.rs` written with `save_entry()`, `load_entries()`, and `verify_on_load()` integrity guard.
- [x] **Phase 4b:** `clap`-based CLI in `src/main.rs`. `run`, `verify`, `report` subcommands with `--json` flag. `cargo test` passes.
- [x] **Phase 4c:** (deferred — real event sources via `notify` crate left for future work; `SecurityEvent::Custom` stub remains in place as the contract)
- [x] **Phase 5:** `echoes_agent` step type in `automaton/steps.py`. Template `templates/agent/echoes-daily.yaml`. Merkle root + `integrity_ok` stored in step `output_json`. UI renders structured output.
- [x] **Phase 6:** `automaton/pg.py` — `PgConn` adapter with `_translate()`, `SKIP LOCKED` lease. `db.open_store()` routing factory. `test_postgres.py` — 11 unit tests (always run) + 15 integration tests (skipped without `AUTOMATON_TEST_PG_URL`). `pyproject.toml` `postgres` extra. iOS SwiftUI client complete (`deploy/ios/`). Android Jetpack Compose client complete (`deploy/android/`) with full build infrastructure.

## Post-Roadmap — Open Items

These were noted during implementation and are candidates for a follow-on ADR:

- ~~**Phase 4c (real event sources):**~~ ✅ Complete — `sensor.rs` added with `EventSource` trait, `FileWatcher` (`notify` crate, feature-gated), `ProcessScanner` (`/proc` on Linux, `ps` on macOS, stub elsewhere), `CompositeSource`. `think_with()` added to `Agent`. CLI flags `--watch PATH` + `--procs`. `echoes.yml` CI workflow.
- ~~**CI for mobile:**~~ ✅ Complete — `.github/workflows/mobile.yml` with iOS (`xcodebuild` on `macos-latest`) and Android (`setup-java@v4` JDK17 + `setup-android@v3` + `gradlew assembleDebug`) jobs. Structural tests in `test_deploy_ci.py`.
- ~~**Option C (automaton as echoes' persistence layer):**~~ ✅ Complete — `0004-agents.sql` schema, `agents.py` CRUD module, five `/api/agents/*` HTTP routes with Bearer auth, `AgentStore` trait in Rust (`SqliteStore` + `RemoteStore` via `ureq`), `--remote-store URL --token TOKEN` flags on `echoes run`. 26 HTTP-level tests in `test_agents.py`.
- ~~**Multi-tenant auth:**~~ ✅ Complete — API-key auth with roles (migration `0005`, `auth.py`, `/api/keys` CRUD, `automaton key` CLI). 37 tests in `test_multitenancy.py`.
- ~~**WASM build of echoes:**~~ ✅ Complete — `echoes-wasm` crate (v1.1.0, `wasm-bindgen`) exposing the hash-chain + Merkle primitives to JS/TS. Hashing matches `agent.rs` exactly; builds for `wasm32-unknown-unknown` in both debug and release, with 8 `wasm-bindgen-test` tests. `wasm-pack` emits the `pkg/` bindings (git-ignored build output).

---

## Known Issues (found & addressed 2026-07-09)

Surfaced during the v1.1 / v0.5.0 release pass:

1. **CI workflows were not running.** All workflows lived under `automaton/.github/workflows/`, but GitHub Actions only executes workflows at the repository-root `.github/workflows/` — so none had ever run (the Actions API reported zero runs). **Resolved:** `test.yml`, `echoes.yml`, and `mobile.yml` were relocated to `/.github/workflows/` (test.yml gained `defaults.run.working-directory: automaton` plus a `paths` filter; the other two were already root-relative). **Resolved (follow-up):** `release.yml` and `docs.yml` were subsequently relocated to the repo root as well (commit `957f217`). The `audit_log` dead-code warning was eliminated by the lib/bin split (commit `a65b423`).

2. **Release tags pointed to the wrong commit.** `echoes-v1.1.0` and `v0.5.0` pointed to `79c8a19` ("Phase 26"), predating the actual releases. **Resolved:** re-pointed to the real release commits — `echoes-v1.1.0` → `5f6f873`, `v0.5.0` → `6f55503` (a `git push --force` updates them on origin).

3. **Workflow relocation broke `test_deploy_ci.py` (found 2026-07-09, audit pass).** The 49 structural CI tests resolved workflow paths as `automaton/.github/workflows/` and all failed after the relocation. **Resolved:** `WORKFLOWS` in `tests/test_deploy_ci.py` now points at the repository root. Full suite: 812 passed, 15 skipped (Postgres integration tests skip without `AUTOMATON_TEST_PG_URL`).

4. **Rust 1.97 clippy regressions in `echoes-wasm`.** New lints (`manual_is_multiple_of` in `merkle_proof`, `bool_assert_comparison` in tests) failed `-D warnings`. **Resolved** with no behavior change; native parity tests and the `wasm32-unknown-unknown` release build both pass. **Resolved (follow-up 2026-07-14):** the wasm parity job in `echoes.yml` now runs `cargo clippy --all-targets -- -D warnings` on `echoes-wasm`, so toolchain-driven lint breakage is caught in CI.

5. **`v0.4.0` tag missing.** Phase 1 records v0.4.0 as tagged and published, but no `v0.4.0` tag exists locally or on origin (only v0.3.0 and v0.5.0). **Resolved (2026-07-14):** annotated tag `v0.4.0` recreated at `79c8a19` ("Phase 26", 2026-05-23) — the feature-complete point matching the CHANGELOG `[0.4.0]` release date. Note: history contains no version-bump commit for 0.4.0 (`pyproject.toml` reads 0.3.0 in that tree); the tag message documents this. Push with `git push origin v0.4.0`.

6. **Doc drift — Phase 4d JSON contract.** The ADR lists keys `agent_name`/`memory_len`/`integrity_ok`; the shipped contract (consistent across `echoes report --json` and `automaton/steps.py`) is `agent`/`memory`/`integrity` plus `goal`, `merkle_root`, `entries`. The code is internally consistent; this document's Phase 4d wording is the stale side. **Resolved (2026-07-14):** Phase 4d wording updated to the shipped contract.

7. **CI hardening pass (2026-07-09/10) — first fully green board.** The first
   real executions of every workflow surfaced, in order: a truncated
   `twine c` command in test.yml (never valid); two genuine Postgres bugs in
   `pg.py` (`RETURNING id` appended to `queue` inserts, and SQLite's
   `julianday()` idiom untranslated); leaked SQLite/yoyo connections in
   `backup.py` and `migrate.py` (`with sqlite3.connect(...)` is a transaction
   scope, not a close) that locked DB files on Windows; Windows shell-token
   quoting in `steps.py`; a reverse-DNS black-hole on GitHub's macOS runners
   that hung the suite inside yoyo's `socket.getfqdn()` call (patched in
   `tests/conftest.py`); macOS deploy scripts tracked without the exec bit;
   missing Android `gradle.properties`, launcher icons, and a nonexistent
   AppCompat theme; a pinned Xcode path that no longer exists on
   `macos-latest`; and two flaky races (early-401 connection resets on
   Windows, a `*/1` cron that could legitimately double-fire across a minute
   boundary). All fixed across commits `20ed2db`…`56d4275`. `pytest-timeout`
   (`--timeout=120`) and job-level `timeout-minutes` are now standing guards
   against future hangs. As of `56d4275`, **all five workflows are green** on
   ubuntu/macOS/windows × py3.10–3.12, Postgres 16, iOS, and Android.
