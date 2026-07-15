# ADR-002: Operational Hardening & Continuous Forensics Roadmap

**Status:** Accepted — implemented through Phase 10 (2026-07-15); Phase 9 trial gate and optional 10d outstanding
**Date:** 2026-07-14
**Decider:** Richmond (sole sign-off)
**Supersedes:** nothing — continues where [ADR-001](ADR-001-architecture-roadmap.md) left off
**Scope:** Both sub-projects — `automaton` (v0.5.1, Python) and `echoes` (v1.1, Rust core + wasm)

---

## Context

ADR-001 closed with all six phases and all post-roadmap items complete, five green
CI workflows, and its known-issues log fully resolved. The system now works:
`automaton` is a production-shaped engine with Postgres, multi-tenant auth, and
native mobile clients; `echoes` is a real forensic binary with persistence, file
and process sensors, a wasm build, and two-way integration with automaton
(`echoes_agent` step type; `--remote-store` persistence).

What the system is *not* yet is **operated**. Nothing runs unattended and tells
you when something is wrong. Specifically:

1. **Integrity verification is pull-only.** `echoes verify` exists, but nothing
   schedules it, and an integrity failure only surfaces if a human runs the
   command or a workflow happens to invoke it. A tamper-evidence system that
   nobody checks is a tamper-oblivious system.
2. **No alerting path for forensic events.** `automaton` has a rich Apprise
   notification layer (`notify.py`, 110+ backends, quiet hours), but no wiring
   from "echoes chain failed verification" to "your phone buzzes."
3. **echoes DBs grow forever.** `automaton` has `prune.py`; `echoes` has no
   retention story. A long-running agent DB grows unbounded, and naive row
   deletion would break the hash chain — retention needs a chain-aware design.
4. **Distribution is PyPI-only.** `automaton-engine` is published;
   the `echoes` crate and `echoes-wasm` npm package are not. Anyone (including
   future-you on a new machine) builds Rust from a git checkout.
5. **Two of four `SecurityEvent` variants are stubs.** `FileAccess` and
   `ProcessExecution` have real sensors (`sensor.rs`, 351 lines);
   `NetworkConnection` and `Authentication` are still manually-logged only —
   deferred in ADR-001 Phase 4c because they need elevated privileges.
6. **Agents are episodic, not continuous.** Every echoes invocation is
   run-N-ticks-and-exit. ADR-001's "revisit later" note on Option C asked:
   are per-workflow snapshots enough, or do we want long-running memory?
   Answer after two months of use: snapshots are fine for workflow attestation,
   but real monitoring (watch this directory *always*) wants a daemon.
7. **Mobile clients predate the agent features.** The iOS/Android clients ship
   the original three screens (runs list, run detail, trigger). Agents, keys,
   and echoes step output (Merkle roots) exist in the API but not on the phone.

Items 1–4 are operational debt on things that already work. Items 5–6 extend
forensic capability. Item 7 is UX catch-up. That ordering is deliberate.

---

## Decision

Proceed in four phases, numbered continuing from ADR-001. Phase 7 (ops
hardening) comes first because it multiplies the value of everything already
built and carries the least risk. Phase 9 (continuous agents) contains the one
real architectural decision in this ADR — see Options below.

---

## The Architectural Decision: How Do Continuous Agents Run?

The other phases are engineering, not architecture. The one genuine decision is
what "long-running agent" means.

### Option A: `echoes daemon` — a new first-class daemon mode

A `daemon` subcommand: the agent runs indefinitely, sensors push events as they
occur (no tick loop), entries flush to the local DB and optionally stream to
automaton via `--remote-store`. Ships with systemd/launchd units like
automaton's own deploy assets.

| Dimension | Assessment |
|---|---|
| Complexity | Medium-high — signal handling, log rotation, reconnect/backoff for remote store, graceful shutdown mid-chain |
| Operational surface | New always-on process to supervise, on every monitored machine |
| Capability | True continuous monitoring; events captured the moment they happen |
| Failure modes | Daemon death = monitoring gap; needs its own liveness alerting |

**Pros:** Real-time capture; the hash chain covers the entire monitored period
with no gaps; conceptually clean ("echoes watches, automaton orchestrates").
**Cons:** Most new code; a second service to keep alive; the "who watches the
watcher" problem lands on day one.

### Option B: automaton-scheduled micro-runs (chained episodes)

No daemon. An automaton cron workflow invokes `echoes run` every N minutes
against the same DB. Each episode appends to the existing chain (the store
already reloads and verifies before appending). Continuity is logical, not
temporal: the chain is continuous even though the process is not.

| Dimension | Assessment |
|---|---|
| Complexity | Low — everything needed already exists; this is a workflow template plus a sensor flag to "drain events since last run" |
| Operational surface | Zero new processes; automaton's scheduler is the supervisor |
| Capability | Events between runs are batched (file events via journal catch-up; process scans are periodic anyway) — bounded blind spots, not none |
| Failure modes | automaton's existing retry/alerting covers missed runs for free |

**Pros:** Almost free; supervision, retries, alerting, and run history are
inherited from automaton; blind-spot windows are explicit and tunable.
**Cons:** Not real-time — an attacker acting entirely inside one interval
window evades file-event capture (process scan still catches persistence);
inotify/kqueue events that occur while no process is listening are simply lost
unless the sensor does a state-diff on startup.

### Option C: daemon in automaton (echoes as a supervised child)

automaton grows a `services:` concept and supervises `echoes daemon` as a
long-lived child process, restarting it and alerting on death.

| Dimension | Assessment |
|---|---|
| Complexity | High — process supervision is a new responsibility for automaton's engine, orthogonal to its DAG model |
| Operational surface | Blurs automaton's identity: workflow engine → init system |
| Capability | Same as A, plus unified supervision |

**Pros:** One place to look. **Cons:** Rebuilds systemd inside automaton;
scope creep with the worst effort-to-value ratio of the three.

### Recommendation

**Option B now, Option A only if a real gap bites.** The micro-run pattern
costs days, inherits every operational property automaton already has, and its
weakness (inter-run blind spots) is mitigated by adding a **startup state-diff**
to `FileWatcher` (snapshot mtimes/hashes of the watched tree at shutdown,
diff on next start, emit synthetic `FileAccess` events for changes). That
closes most of the gap without a resident process. Option C is rejected.
This mirrors ADR-001's B-then-C integration call, which worked out well.

---

## Phased Roadmap

### Phase 7 — Ops hardening: verify on a schedule, alert on failure, retain sanely, publish

**7a — Scheduled verification (2–3 days).** Ship a first-party workflow template
`templates/agent/echoes-verify.yaml`: cron trigger → `echoes_agent` step with
`action: verify` for each registered DB → on failure, `notify` step (urgent,
bypasses quiet hours). `steps.py` already raises `StepError` on integrity
failure, so the wiring is template-only. Add an `integrity_failures_total`
counter to `metrics.py`.

**7b — Retention for echoes DBs (1 week).** Chain-aware pruning:
`echoes prune --keep-last N --db PATH` seals entries `0..k` by recording a
**checkpoint row** (Merkle root + entry count + head hash of the pruned prefix,
itself hashed into the chain) and then deletes the raw rows. `verify` treats a
checkpoint as a trusted genesis. Old data becomes unrecoverable but its
*integrity attestation* survives — the audit property is preserved, the bytes
are not. Document this trade-off in the CLI help.

**7c — Publish the Rust artifacts (2–3 days).**
- `echoes` crate → crates.io (needs a crate-name check; `echoes` may be taken —
  fall back to `echoes-forensic`).
- `echoes-wasm` `pkg/` → npm as `@richeyworks/echoes-wasm`.
- Extend `release.yml`: tag `echoes-v*` publishes both, gated on the existing
  fmt/clippy/test/parity jobs.

**7d — Prebuilt binaries (1–2 days).** `cargo dist` or a matrix build job
attaching `echoes` binaries (linux-x86_64/aarch64, macos-arm64, windows) to
GitHub Releases. Removes the Rust-toolchain requirement from every deploy doc.

**Success criteria:** A tampered byte in a monitored DB produces a phone
notification within one cron interval, with zero manual steps. `cargo install
echoes-forensic` and `npm i @richeyworks/echoes-wasm` both work from a clean
machine.

---

### Phase 8 — Deeper forensics: the two stub event variants + Windows parity

**8a — `NetworkConnection` sensor (1–2 weeks).** Periodic connection-table
scan — `/proc/net/tcp{,6}` on Linux, `lsof -i -n` on macOS, `GetExtendedTcpTable`
(or `netstat -ano` fallback) on Windows. Diff against the previous scan; new
connections become events with `src`, `dst`, `port`, plus owning PID where
readable. **No packet capture, no promiscuous mode** — connection metadata only.
Unprivileged operation is the design constraint: degrade gracefully (fewer
fields) rather than require root/admin.

**8b — `Authentication` sensor (1–2 weeks, Linux-first).** Tail auth logs:
`/var/log/auth.log` / `journalctl -f _COMM=sshd` on Linux (readable by the
`adm`/`systemd-journal` groups — document the group add, don't require root),
`log stream --predicate` for macOS `loginwindow`/`sshd` as best-effort.
Parse successes and failures into `Authentication` events. Windows: stub
(Security event log needs privileges that violate the unprivileged constraint).

**8c — Windows `FileWatcher` parity (2–3 days).** The `notify` crate already
backs onto ReadDirectoryChangesW; the work is enabling the `watch` feature
path on Windows, adding a Windows job to `echoes.yml`'s test-watch matrix, and
verifying event semantics (rename/overwrite differ from inotify).

**8d — Sensor config file (2–3 days).** CLI flags are outgrowing themselves
(`--watch`, `--procs`, now `--net`, `--auth`). Add `echoes.toml`:
sensor enable/disable, scan intervals, watch paths, remote-store URL.
Flags override file. One `--config` flag replaces flag sprawl.

**Success criteria:** On an unprivileged Linux box, an agent captures a real
SSH login and a real outbound connection into the hash chain, and `verify`
passes after restart. All four `SecurityEvent` variants have at least one
platform with a live sensor.

---

### Phase 9 — Continuous monitoring via chained micro-runs (Option B)

**9a — Startup state-diff for `FileWatcher` (1 week).** On shutdown, persist a
manifest (path → mtime, size, xxhash) of watched trees into the DB. On startup,
diff and emit synthetic `FileAccess` events (`operation: "changed-while-offline"`)
before live watching begins. This is the blind-spot mitigation that makes
micro-runs honest.

**9b — Monitor workflow template (2–3 days).**
`templates/agent/echoes-monitor.yaml`: every-5-minutes cron → `echoes run`
against a persistent DB with the config file from 8d → verify step → optional
report step publishing the Merkle root to the event log. Retry + notify on
failure inherited from automaton.

**9c — Continuity report (2–3 days).** `echoes report --continuity`: episode
boundaries (run start/stop entries), offline-diff summaries, and total
chain-covered wall-clock time vs. gaps. This makes the "logical continuity"
claim inspectable instead of implied.

**Success criteria:** Two weeks of unattended 5-minute micro-runs on one
machine: chain verifies end-to-end, offline changes appear as synthetic events,
and automaton's run history shows the Merkle root advancing. Revisit Option A
(daemon) only if this two-week trial surfaces gaps that state-diff can't cover.

---

### Phase 10 — Mobile/UI catch-up

Do this last: phases 7–9 change what the API exposes (agents, integrity
status, continuity), and building screens twice is waste.

**10a — Agents in the clients (1 week each platform).** New tab: registered
agents, last Merkle root, integrity badge (green/red), last-seen time.
Read-only. Three API calls, all existing (`/api/agents/*`).

**10b — Integrity surfaces in run detail (2–3 days each).** Where a run
contains an `echoes_agent` step, render `merkle_root` (truncated, tap to copy)
and `integrity` as a badge instead of raw JSON.

**10c — UI dashboard: forensics card (2–3 days).** Web-UI counterpart of 10a
plus a sparkline of `integrity_failures_total`.

**10d — Push on integrity failure (optional, 1 week).** The 7a template
already notifies via Apprise, which covers phones without touching APNs/FCM.
Native push is a want, not a need — build only if Apprise latency annoys.

**Success criteria:** Opening the app on a Tailnet phone answers, in one
glance, "are all my agents green?"

---

## Trade-off Summary

| Phase | Effort | Risk | Unlocks |
|---|---|---|---|
| 7 — Ops hardening | Low (2–3 weeks total) | Low — mostly templates + release plumbing; 7b's checkpoint design is the one careful bit | Unattended operation; installable artifacts |
| 8 — Deeper forensics | Medium (4–6 weeks) | Medium — OS-specific parsing, privilege edge cases | All four event variants real; Windows parity |
| 9 — Micro-run monitoring | Low-medium (2 weeks) | Low — inherits automaton's supervision | Continuous coverage without a daemon |
| 10 — Mobile/UI | Medium (3–4 weeks) | Low — read-only screens on stable APIs | Glanceable forensic status |

Sequencing rationale: 7 before 8 because alerting on integrity failure matters
more than new event types (better to *know* about tampering with two sensors
than to miss it with four). 9 depends on 8d (config file) and benefits from
8c. 10 last, after the API surface stops moving.

---

## Consequences

**Easier afterwards:** the system runs itself and reports by exception;
new machines onboard with `cargo install` + one workflow template; the phone
becomes a status surface instead of a browser bookmark.

**Harder afterwards:** published artifacts mean semver discipline and a real
deprecation cost for the JSON contract and `echoes.toml` schema; auth-log and
connection-table parsing is inherently distro/OS-version sensitive and will
need a compatibility test matrix; checkpoint-pruning (7b) permanently trades
raw history for attestations — irreversible by design.

**Revisit later:**
- Option A daemon mode — after the Phase 9 two-week trial, with data.
- eBPF-based sensors on Linux — real-time capture without polling, but a
  privilege and complexity jump; only worth it if 8a's scan interval proves too
  coarse.
- Signed releases (sigstore/minisign) for the prebuilt binaries — a forensic
  tool should arguably have attested provenance itself.
- Cross-machine chain aggregation — one automaton, many agents, one "fleet
  integrity" Merkle root over per-agent roots.

---

## Action Items

- [x] **Phase 7a:** `templates/agent/echoes-verify.yaml` (urgent, hourly cron, verify + attest steps) + `automaton_integrity_failures_total` metric in `metrics.py`. Tests in `test_metrics.py` + catalog tests updated. Full suite: 820 passed, 15 skipped.
- [x] **Phase 7b:** `checkpoints` table + `Checkpoint`/`PruneOutcome` in `store.rs` (`verify_checkpoints`, `chain_base`, transactional `prune`); `agent::checkpoint_hash` + `Agent::restore_from` with `base_hash`; `echoes prune --keep-last N` CLI; `verify`/`report`/`run` checkpoint-aware; 10 new unit tests (29 total) incl. tamper-after-prune and forged-checkpoint cases; prune leg added to CI smoke. Verified locally: cargo test + clippy clean, full CLI lifecycle incl. kill/resume and both tamper paths (exit 2).
- [x] **Phase 7c:** crates.io name `echoes` confirmed available; publish metadata + LICENSE added to the crate (`cargo publish --dry-run` passes, package list clean); `release.yml` extended with an `echoes-v*` tag family — test-echoes gate → publish-crate (crates.io) + publish-npm (`@richeyworks/echoes-integrity`) → GitHub Release from the echoes CHANGELOG; python jobs skip echoes tags and vice versa; 7 structural tests added to `test_deploy_ci.py`. Needs `CARGO_REGISTRY_TOKEN` + `NPM_TOKEN` repo secrets before first tagged release.
- [x] **Phase 7d:** `build-binaries` matrix in `release.yml` (linux x86_64 + aarch64 via `ubuntu-24.04-arm`, macOS arm64, Windows), `--release --features watch`, tar.gz/zip with LICENSE+README+CHANGELOG, attached to the echoes GitHub Release. 2 more structural tests (58 total in `test_deploy_ci.py`). Bonus fix: `--version` was hardcoded at 1.0.0; now derives from Cargo.toml.
- [x] **Phase 8a:** `NetScanner` in `sensor.rs` behind `--net` — snapshot-diff of the connection table, ESTABLISHED only; `/proc/net/tcp{,6}` parsed directly on Linux (kernel hex format, incl. v6 little-endian words), `netstat` parsing for macOS (BSD `ip.port`) and Windows (`ip:port` + CRLF). Pure parsers unit-tested on all platforms + a live loopback-capture test on Linux. 35 crate tests, clippy clean. (Config-file wiring lands with 8d.)
- [x] **Phase 8b:** `AuthWatcher` in `sensor.rs` behind `--auth [PATH]` — offset-tracking tail of `/var/log/auth.log`/`/var/log/secure`, rotation-aware, partial-line-safe; parses sshd Accepted/Failed (incl. invalid-user) + PAM failures. `adm` group documented in README. Pure parser + tail-behavior tests (37 crate tests), clippy clean; live CLI smoke captured a mid-run appended failure event into the chain. macOS `log stream` left as future work (no unprivileged equivalent). All four `SecurityEvent` variants now have live sensors.
- [x] **Phase 8c:** `test-watch` in `echoes.yml` is now a ubuntu+windows matrix (`shell: bash` for the smoke); feature-gated `file_watcher_reports_known_operations` test pins the operation vocabulary, tolerant of CI event-delivery timing.
- [x] **Phase 8d:** `config.rs` + `toml` dep; `echoes run --config PATH`; CLI > file > default resolution, boolean sensors OR'd, unknown keys rejected (`deny_unknown_fields`); 5 config tests + end-to-end smoke (config-driven run, flag override, loud typo failure). 43 crate tests, clippy clean on both feature sets. **Phase 8 complete.**
- [x] **Phase 9a:** `manifest.rs` (`scan_tree` with SHA-256 + `diff`), `file_manifest` table with save/load in `store.rs`, `QueuedSource` injecting `*-while-offline` events ahead of live sensors in `cmd_run`, end-of-run snapshot. 49 crate tests, clippy clean both feature sets. End-to-end verified: edit+delete+create between two runs → all three synthetic events in the verified chain. Works without the `watch` feature (micro-runs need only manifests). Timestomp-resistant via content hashing.
- [x] **Phase 9b:** `templates/agent/echoes-monitor.yaml` (5-min cron, urgent, run→verify→attest with watch/net/procs sensors) + sensor/config passthrough in the `echoes_agent` step (`config`/`watch`/`procs`/`net`/`auth` spec fields, env-string tolerant). 2 new step tests, catalog at 13 templates, 68 tests green.
- [x] **Phase 9c:** `episodes` table + `begin/end_episode` bookkeeping in `run`; pure `summarize_episodes` (gaps, live vs span, interrupted count); `report --continuity` in text + JSON incl. offline-diff event count. 51 crate tests, clippy clean. Smoke: 3 micro-runs with an offline edit → correct episodes, gaps, and offline count. **Code for Phase 9 complete — the two-week unattended trial is the remaining gate.**
- [ ] **Phase 9 gate:** two-week unattended trial; write up findings; decide on Option A.
- [x] **Phase 10b (web) + 10c:** forensics card on the run-list page (per-agent linkage badge via new `agents.chain_linkage_ok` — linkage-only, honestly documented; last hash; integrity-failure count + 14-day sparkline) and echoes badges in run detail (integrity green/red, truncated Merkle root with copy, entries/sealed counts). 6 new UI tests; 60 green across UI suites.
- [x] **Phase 10a + 10b (mobile):** `AgentsView.swift` (NavigationStack list → detail with linkage badge, latest hash, recent memory) and `AgentsScreen.kt` (list → inspect dialog), fourth tab wired in both apps; `agents()`/`agentEntries()` + `ChainLinkage` check in both client kits. Structural tests extended (67 green); compile gate is `mobile.yml` CI. **All ADR-002 code complete** — remaining: Phase 9 two-week trial, optional 10d (native push, deferred pending Apprise experience).
- [ ] **Phase 10d (optional):** native push, only if Apprise proves insufficient.
