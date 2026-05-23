# echoes

A security-minded multi-agent simulation starter in Rust.

The goal of this project is to explore **agent systems with strong integrity and auditability** from the ground up.

## Current Features (v1.0) - Forensic Security Agent

- **Structured `SecurityEvent`** types (NetworkConnection, FileAccess, Authentication, ProcessExecution, Custom)
- Cryptographic memory integrity using **SHA-256 hash chaining + Merkle Tree**
- Append-only audit log
- Immutable goal after agent creation
- `verify_integrity()` + Merkle root verification
- `print_memory_chain()`, `print_audit_log()`, `print_merkle_info()`
- `merkle_root()` and Merkle proof generation/verification
- Comprehensive unit tests
- Built-in tamper detection demo with structured events

## How to Run

```bash
cargo run
```

You should see the agent taking actions, pretty-printed chain + audit log, **Merkle root**, integrity check, and an enhanced tamper demo showing both mechanisms detecting changes.

### Running Tests

```bash
cargo test
```

All tests focus on the security guarantees:
- Normal operation keeps the chain valid
- Any modification to past memory is detected
- Goal remains immutable
- Hash links are correctly formed

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

Run the program normally:

```bash
cargo run
```

At the end you will see:

```
=== Integrity Verification ===
Memory chain integrity: PASSED ✓
```

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

- Multi-agent support with identity
- Signed actions
- Reputation / trust system between agents
- Secure inter-agent messaging

## Philosophy

This project prioritizes **correctness and auditability** over rapid feature development. Every major component should be verifiable.