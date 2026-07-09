# Changelog

All notable changes to echoes are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
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
