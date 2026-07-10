# echoes

[![echoes CI](https://github.com/RicheyWorks/echoes/actions/workflows/echoes.yml/badge.svg)](https://github.com/RicheyWorks/echoes/actions/workflows/echoes.yml)

A forensic security agent with cryptographic memory integrity, written in Rust.

Explores **agent systems with strong auditability** — every decision is hash-chained, Merkle-rooted, and tamper-evident. Memory survives restarts (local SQLite or remote automaton store) and integrity is verified on every load.

## Current Features (v1.1) - Forensic Security Agent

- **Structured `SecurityEvent`** types (NetworkConnection, FileAccess, Authentication, ProcessExecution, Custom)
- Cryptographic memory integrity using **SHA-256 hash chaining + Merkle Tree**
- Append-only audit log
- Immutable goal after agent creation
- `verify_integrity()` + Merkle root verification
- `print_memory_chain()`, `print_audit_log()`, `print_merkle_info()`
- `merkle_root()` and Merkle proof generation/verification
- Comprehensive unit tests
- Built-in tamper detection demo with structured events
- **Real event sources** via `sensor.rs` — `FileWatcher` (inotify/kqueue/ReadDirectoryChangesW), `ProcessScanner`, `CompositeSource`
- **Remote persistence** — `--remote-store URL` persists memory to an [automaton](https://github.com/RicheyWorks/echoes/tree/main/automaton) server; supports cross-machine resume

## CLI (v1.1)

```
echoes run     [--db PATH] [--ticks N] [--name NAME] [--goal TEXT]
               [--watch PATH] [--procs]
               [--remote-store URL] [--token TOKEN]
echoes verify  [--db PATH] [--name NAME]
echoes report  [--db PATH] [--name NAME] [--json]
```

### Basic usage

```bash
# Run for 8 ticks and persist to echoes.db
cargo run -- run --ticks 8

# Resume from the same DB (adds 8 more ticks)
cargo run -- run --ticks 8

# Verify hash-chain integrity
cargo run -- verify

# Print the full memory chain as JSON (for automaton integration)
cargo run -- report --json
```

### Real event sources (Phase 4c)

The agent can draw real forensic events instead of synthetic stubs.

**Filesystem watcher** (requires `--features watch`):
```bash
cargo run --features watch -- run --ticks 20 --watch /etc
```
Records `SecurityEvent::FileAccess` entries for any create/modify/delete
under the watched path. Uses inotify (Linux), kqueue (macOS), or
ReadDirectoryChangesW (Windows) via the [`notify`](https://docs.rs/notify) crate.

**Process scanner**:
```bash
cargo run -- run --ticks 20 --procs
```
Scans for newly-spawned processes each tick and records
`SecurityEvent::ProcessExecution` entries. Reads `/proc/<pid>/comm` on Linux;
runs `ps -eo pid,comm` on macOS. No-op on other platforms.

**Combined**:
```bash
cargo run --features watch -- run --ticks 50 \
    --watch /var/log \
    --procs \
    --db /var/lib/echoes/monitor.db
```

### Remote persistence (Option C)

Persist agent memory to an [automaton](https://github.com/RicheyWorks/echoes/tree/main/automaton) server instead of a local SQLite file. Requires automaton ≥ v0.5.0 running with `AUTOMATON_TOKEN` set.

```bash
# Run against a remote automaton instance
cargo run -- run --ticks 8 \
    --remote-store http://192.168.1.10:8080 \
    --token my-secret-token

# The token can also come from the environment
export AUTOMATON_TOKEN=my-secret-token
cargo run -- run --ticks 8 --remote-store http://192.168.1.10:8080
```

Resume works automatically — on the next run, `load_entries` fetches the existing hash chain from the server so integrity is maintained across machines.

> **Note:** `echoes verify` and `echoes report` remain SQLite-only (forensic integrity checks require local data). Use `echoes run --remote-store` to populate the server, then export the chain with `GET /api/agents/<name>/entries` if offline verification is needed.

### Running tests

```bash
cargo test                    # default — no real event sources
cargo test --features watch   # includes FileWatcher compilation
```

## Architecture

```
src/
  agent.rs    — pure computation: hash chain, Merkle tree, SecurityEvent
  sensor.rs   — real event sources: FileWatcher, ProcessScanner, CompositeSource
  store.rs    — AgentStore trait; SqliteStore (local) + RemoteStore (HTTP via ureq)
  main.rs     — clap CLI: run / verify / report
```

`agent.rs` has no I/O dependency — all unit tests run without any filesystem
or process-related setup. `sensor.rs` provides the `EventSource` trait and
two implementations. `think()` always uses synthetic events; `think_with(src)`
draws from a real source and falls back to synthetic when the source is idle.

## Security Features

### 1. Cryptographic Hash Chain (Memory Integrity)

Every `MemoryEntry` contains:
- `hash`: SHA-256 hash of this entry
- `prev_hash`: Hash of the previous entry

This creates a **hash chain**. If any past memory entry is modified, the chain breaks and `verify_integrity()` will return `false`.

```rust
if agent.verify_integrity() {
    println!("Memory is intact");
} else {
    println!("Tampering detected!");
}
```

### 2. Immutable Goal

Once an agent is created, its goal cannot be changed directly:

```rust
let agent = Agent::new("Echo", "map the environment");

// This won't compile:
// agent.goal = "new goal".to_string();

// Correct way to read it:
println!("{}", agent.goal());
```

This prevents silent goal tampering after initialization.

### 3. Audit Log

All important events are recorded in an append-only audit log:

```rust
for entry in agent.audit_log() {
    println!("[Tick {}] {}", entry.tick, entry.event);
}
```

## Verification Instructions

### Checking Memory Integrity

Run the agent and verify in two steps:

```bash
cargo run -- run --ticks 10
cargo run -- verify
```

`verify` loads the stored chain and prints:

```
Memory chain integrity: PASSED ✓
Merkle root: a3f9...
```

Exit code is 0 on pass, 1 on failure — safe to use in scripts and CI.

### Demonstrating Tamper Detection (Educational)

You can manually test the integrity system:

1. Add a `MemoryEntry` directly (bypassing normal flow)
2. Modify an existing entry's `note` or `hash`
3. Call `verify_integrity()` — it should now return `false`

Example of what breaks integrity:

```rust
// This would break the chain if done after creation
if let Some(entry) = agent.memory.get_mut(0) {
    entry.note = "tampered".to_string();
}

assert!(!agent.verify_integrity());
```

The hash chain ensures that historical decisions cannot be rewritten without detection.

## Future Directions

- Multi-agent support with identity — multiple named agents in a single run, cross-agent attestation
- Signed actions — cryptographically bind each agent action to the agent's keypair
- Reputation / trust system between agents
- Secure inter-agent messaging
- WASM build — the Merkle tree and hash chain code is pure computation with no OS dependencies; a `wasm-pack` build would make the integrity primitives usable in browser or JS/TS environments

## Philosophy

This project prioritizes **correctness and auditability** over rapid feature development. Every major component should be verifiable.
