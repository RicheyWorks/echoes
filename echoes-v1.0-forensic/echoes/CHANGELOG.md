# Changelog

All notable changes to echoes are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Fixed
- `echoes --version` printed a hardcoded `1.0.0`; it now derives from
  `Cargo.toml` (`CARGO_PKG_VERSION`), so it can never drift again.

### Added
- **Continuity report (ADR-002 Phase 9c)** — `echoes report --continuity`
  shows episode boundaries (every `run` now records wall-clock + tick range
  into a new `episodes` table), gaps between micro-runs, interrupted runs,
  live-vs-span coverage, and the offline-diff event count. JSON reports gain
  a `continuity` object. Makes micro-run "logical continuity" inspectable
  instead of implied.
- **Offline state-diff manifests (ADR-002 Phase 9a)** — every `run --watch
  PATH` snapshots the watched tree (size, mtime, SHA-256) into a new
  `file_manifest` table at exit; the next run diffs before live watching
  begins and records synthetic `FileAccess` events
  (`created/changed/deleted-while-offline`) into the hash chain. SHA-256
  content hashing defeats timestomping; symlinks are not followed; works in
  builds without the `watch` feature. New `manifest.rs` module +
  `QueuedSource` sensor.
- **`echoes.toml` config file (ADR-002 Phase 8d)** — `echoes run --config
  PATH` loads db/name/goal/ticks/sensors/remote-store from TOML (new
  `config.rs` module, `toml` dep). CLI flags override the file; boolean
  sensors are enabled by either side; unknown keys are rejected loudly.
- **Windows watch CI (ADR-002 Phase 8c)** — the `test-watch` job now runs a
  ubuntu + windows matrix, exercising the ReadDirectoryChangesW backend; a
  feature-gated `FileWatcher` test pins the operation vocabulary across
  backends without depending on CI event-delivery timing.
- **`AuthWatcher` — real `Authentication` events (ADR-002 Phase 8b)** —
  `echoes run --auth [PATH]` tails the system auth log (auto-detects
  `/var/log/auth.log` / `/var/log/secure`) and records sshd
  accepts/failures (incl. invalid-user attempts) and PAM authentication
  failures. Rotation-aware, partial-line-safe, only-new-lines semantics.
  Requires `adm` group membership, not root; declines gracefully where no
  readable log exists. With this, **all four `SecurityEvent` variants have
  live sensors.**
- **`NetScanner` — real `NetworkConnection` events (ADR-002 Phase 8a)** —
  `echoes run --net` diffs the OS connection table each tick and records new
  established connections (src `ip:port`, dst ip, dst port). Parses
  `/proc/net/tcp{,6}` directly on Linux (no subprocess); `netstat` on
  macOS/Windows. Unprivileged by design: metadata only, no packet capture, no
  root. Pure parsers are platform-independent and unit-tested everywhere,
  plus a live loopback-capture test on Linux.
- **Prebuilt release binaries (ADR-002 Phase 7d)** — `echoes-v*` releases now
  attach binaries for linux x86_64/aarch64, macOS arm64, and Windows x86_64,
  built `--release --features watch` so filesystem event capture works out of
  the box. No Rust toolchain needed to deploy.
- **Publishing pipeline (ADR-002 Phase 7c)** — pushing an `echoes-v*` tag now
  publishes the `echoes` crate to crates.io and the wasm bindings to npm as
  `@richeyworks/echoes-integrity`, gated on clippy + tests for both crates
  (`release.yml`). Cargo.toml gained the crates.io metadata (description,
  license, repository, keywords, categories) and a `LICENSE` file; a
  tag-vs-Cargo.toml version guard prevents mismatched releases. Requires the
  `CARGO_REGISTRY_TOKEN` and `NPM_TOKEN` repo secrets.
- **Chain-aware pruning (`echoes prune --keep-last N`)** — seals all but the
  last N entries behind a checkpoint row (ADR-002 Phase 7b). The checkpoint
  records the pruned prefix's head hash (which becomes the trusted genesis
  for the live chain), a Merkle root attesting the deleted entries, and a
  checkpoint hash; checkpoint rows are themselves hash-chained, so forging
  any sealed prefix breaks every later checkpoint. `verify`, `report`, and
  `run` are all checkpoint-aware; `prune` refuses to run if the chain does
  not verify first. Sealed entries are unrecoverable by design — their
  integrity attestation survives. `report --json` gains `sealed_entries`,
  `sealed_through_tick`, `sealed_merkle_root`, and `checkpoint_hash` fields
  when a checkpoint exists (existing fields unchanged).
- `Agent::restore_from(...)` / `Agent.base_hash` — chain verification
  against a non-zero trusted genesis; `agent::checkpoint_hash(...)` is the
  sealing primitive. `SqliteStore` gains `verify_checkpoints`, `chain_base`,
  and `prune` (transactional). New `checkpoints` table (created lazily;
  existing DBs are unaffected until first prune).
- **`echoes-wasm` crate** — WebAssembly bindings for the hash-chain and
  Merkle-tree integrity primitives, publishable as
  `@richeyworks/echoes-integrity` on npm:
  - `verify_chain(entries_json)` — recomputes every SHA-256 hash and
    checks `prev_hash` linkage; returns `true` if the chain is intact.
  - `compute_merkle_root(entries_json)` — builds the Merkle tree and
    returns the root as a 64-char hex string.
  - `generate_proof(entries_json, index)` — inclusion proof for one entry.
  - `verify_merkle_proof(root, leaf, siblings, directions)` — verifies an
    inclusion proof against a known root.
  - 7 `wasm-bindgen-test` tests covering empty/single/multi-entry chains,
    tamper detection, Merkle root determinism, and proof round-trip.
  - Hashing logic matches `agent.rs` exactly — a chain produced by the
    Rust binary is verifiable in any JS/TS WASM environment.

---

## [1.1.0] — 2026-05-26

### Added
- **`AgentStore` trait** (`store.rs`) — both backends implement
  `save_agent_meta`, `load_agent_meta`, `save_entry`, `load_entries`:
  - `SqliteStore` (formerly `Store`) — unchanged local-first behaviour;
    `pub type Store = SqliteStore` keeps existing call-sites working.
  - `RemoteStore` — HTTP backend that POSTs entries to automaton's
    `/api/agents/*` API via the `ureq` sync HTTP client.  Supports
    resume: `load_entries` fetches the existing chain so `echoes run`
    continues from where it left off even on a different machine.
- **`--remote-store URL --token TOKEN`** flags on `echoes run` — when
  present, a `Box<dyn AgentStore>` is dispatched to `RemoteStore` instead
  of the local SQLite file.  `--token` also reads `AUTOMATON_TOKEN` from
  the environment.
- **Real event sources** (`sensor.rs`):
  - `EventSource` trait — non-blocking `poll() -> Option<SecurityEvent>`.
  - `FileWatcher` — wraps the `notify` crate (inotify / kqueue /
    ReadDirectoryChangesW); enabled with `--features watch`.
  - `ProcessScanner` — reads `/proc/<pid>/comm` (Linux) or
    `ps -eo pid,comm` (macOS) each tick.
  - `CompositeSource` — chains multiple `EventSource` implementations.
  - `agent::Agent::think_with<S: EventSource>()` — generically-dispatched
    variant; `think()` delegates to it with `None` (zero-cost).
  - CLI flags `--watch PATH` and `--procs` on the `run` subcommand.
- **`echoes.yml` GitHub Actions workflow** — `cargo test` on Linux +
  macOS, `cargo test --features watch`, `cargo clippy -D warnings`.

### Changed
- Version bumped to 1.1.0.
- `ureq = { version = "2", features = ["json"] }` added to `Cargo.toml`.

---

## [1.0.0] — 2026-05-22

### Added
- **Persistence layer** (`store.rs`) — `Store` backed by SQLite via
  `rusqlite`; integrity verified on load.
- **CLI** (`main.rs`) — `echoes run`, `echoes verify`, `echoes report`
  subcommands via `clap`.  `--json` flag on `report`.
- **Structured `SecurityEvent`** types: `NetworkConnection`, `FileAccess`,
  `Authentication`, `ProcessExecution`, `Custom`.
- Cryptographic memory integrity via SHA-256 hash chaining + Merkle tree.
- Append-only audit log; immutable goal after agent creation.
- `verify_integrity()`, `merkle_root()`, Merkle proof generation and
  verification.
- 5 unit tests covering integrity, tamper detection, Merkle proofs.

---

<!-- version comparison links -->
[Unreleased]: https://github.com/RicheyWorks/echoes/compare/echoes-v1.1.0...HEAD
[1.1.0]: https://github.com/RicheyWorks/echoes/compare/echoes-v1.0.0...echoes-v1.1.0
[1.0.0]: https://github.com/RicheyWorks/echoes/releases/tag/echoes-v1.0.0
