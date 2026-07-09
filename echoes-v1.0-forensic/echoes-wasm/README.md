# @richeyworks/echoes-integrity

WebAssembly bindings for the [echoes](../echoes) hash-chain and Merkle-tree
integrity primitives.

Lets any JavaScript or TypeScript environment verify that an echoes agent's
memory chain is intact — without trusting the server that stored it.

## What's in the box

| Function | What it does |
|---|---|
| `verify_chain(entries_json)` | Recomputes every SHA-256 hash and checks the prev_hash linkage. Returns `true` if intact, `false` on any tamper. |
| `compute_merkle_root(entries_json)` | Builds the Merkle tree from entries; returns the root as a 64-char hex string. Compare against what automaton reported. |
| `generate_proof(entries_json, index)` | Returns an inclusion proof for entry `index` (`{leaf_hash, siblings, directions}`). |
| `verify_merkle_proof(root, leaf, siblings, directions)` | Verifies an inclusion proof against a known root. |

All functions are pure computation — no I/O, no network calls, no OS
dependencies. The hashing logic matches `agent.rs` exactly, so a chain
produced by the Rust binary is verifiable here bit-for-bit.

## Entry shape

Functions that take `entries_json` expect a JSON array matching the shape
returned by automaton's `GET /api/agents/<name>/entries`:

```json
[
  {
    "tick":      1,
    "action":    "Observe",
    "event":     { "Custom": "environment scan performed" },
    "note":      "scanning environment",
    "hash":      "3a7f...64hexchars",
    "prev_hash": "0000...64hexchars"
  }
]
```

`event` can be any `SecurityEvent` variant:

```json
{ "Custom": "message" }
{ "NetworkConnection": { "src": "1.2.3.4", "dst": "5.6.7.8", "port": 443 } }
{ "FileAccess": { "path": "/etc/passwd", "operation": "read" } }
{ "Authentication": { "user": "root", "success": false } }
{ "ProcessExecution": { "name": "bash", "pid": 1234 } }
```

## Install

```bash
npm install @richeyworks/echoes-integrity
```

Or import directly in a browser via a CDN that serves the package.

## Usage

### Verify a chain fetched from automaton

```typescript
import init, { verify_chain, compute_merkle_root } from '@richeyworks/echoes-integrity';

await init(); // load the .wasm binary once

const res = await fetch('https://automaton.host/api/agents/my-agent/entries', {
  headers: { Authorization: `Bearer ${token}` },
});
const { entries } = await res.json();
const json = JSON.stringify(entries);

const ok   = verify_chain(json);
const root = compute_merkle_root(json);

console.log(`Chain intact: ${ok}`);
console.log(`Merkle root:  ${root}`);
// Compare root against what automaton reported in GET /api/agents/my-agent/meta
```

### Verify an inclusion proof

```typescript
import init, {
  compute_merkle_root,
  generate_proof,
  verify_merkle_proof,
} from '@richeyworks/echoes-integrity';

await init();

const json  = JSON.stringify(entries);
const root  = compute_merkle_root(json);
const proof = JSON.parse(generate_proof(json, 3)); // entry at index 3

const valid = verify_merkle_proof(
  root,
  proof.leaf_hash,
  JSON.stringify(proof.siblings),
  JSON.stringify(proof.directions),
);
console.log(`Entry 3 is ${valid ? 'proven' : 'NOT proven'} to be in the tree`);
```

### Node.js / CommonJS

```javascript
const { verify_chain, compute_merkle_root } = require('@richeyworks/echoes-integrity');
// No init() needed when using the Node.js build target
```

## Building from source

Requires [wasm-pack](https://rustwasm.github.io/wasm-pack/):

```bash
cargo install wasm-pack
```

**For bundlers (webpack, Vite, Rollup):**

```bash
wasm-pack build --target bundler --out-name echoes_wasm
# Output: pkg/
```

**For browsers with native ES modules (no bundler):**

```bash
wasm-pack build --target web --out-name echoes_wasm
```

**For Node.js:**

```bash
wasm-pack build --target nodejs --out-name echoes_wasm
```

## Publishing to npm

```bash
wasm-pack build --target bundler --out-name echoes_wasm
cd pkg
# Edit package.json to set "name": "@richeyworks/echoes-integrity" if needed
npm publish --access public
```

## Testing

```bash
wasm-pack test --node
```

The test suite covers:
- Empty chain verification
- Single-entry chain
- Three-entry chain (correct hashing + linkage)
- Tamper detection (note changed without recomputing hash)
- Merkle root determinism
- Full proof round-trip (generate → verify)
- Out-of-range proof index → `null`

## How the hashing works

The hash for each entry is computed identically to `Agent::compute_hash` in `agent.rs`:

```
SHA-256(
  prev_hash_bytes       ||   // [u8; 32], all zeros for the genesis entry
  tick.to_be_bytes()    ||   // u32 big-endian
  action_debug_bytes    ||   // bytes of format!("{:?}", action) — e.g. "Observe"
  event_debug_bytes     ||   // bytes of format!("{:?}", event) — Rust Debug format
  note_bytes                 // UTF-8 bytes of the note string
)
```

The Merkle leaf hash for each entry:

```
SHA-256(
  tick.to_be_bytes()    ||
  action_debug_bytes    ||
  note_bytes            ||
  entry_hash_bytes           // the [u8; 32] hash above, raw bytes
)
```

The Merkle tree is a balanced binary tree, pairing adjacent leaves and
duplicating the last leaf at odd levels, identical to `MerkleTree` in `agent.rs`.

## Security note

This library verifies cryptographic integrity — it cannot detect content that
was never recorded in the first place. A compromised automaton server could
serve a plausible-looking chain with legitimate hashes. For high-assurance
use, export the chain with `GET /api/agents/<name>/entries` and also obtain
the Merkle root from an independent source (e.g. a separate automaton
instance, or a value you noted when the agent ran locally with `echoes report`).
