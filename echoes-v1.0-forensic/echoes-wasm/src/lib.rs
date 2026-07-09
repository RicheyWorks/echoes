//! WebAssembly bindings for echoes hash-chain and Merkle-tree integrity
//! primitives.
//!
//! All functions are pure computation — no I/O, no WASI, no OS calls.
//! The hashing logic matches `agent.rs` in the main `echoes` crate exactly,
//! so a chain produced by the Rust binary can be verified here in any JS/TS
//! environment that supports WebAssembly.
//!
//! # Entry shape
//!
//! All `*_json` arguments accept (or return) JSON arrays of entry objects
//! that match the shape returned by automaton's
//! `GET /api/agents/<name>/entries`:
//!
//! ```json
//! {
//!   "tick":      1,
//!   "action":    "Observe",
//!   "event":     { "Custom": "environment scan performed" },
//!   "note":      "scanning environment",
//!   "hash":      "deadbeef...64hexchars",
//!   "prev_hash": "0000000...64hexchars"
//! }
//! ```

use wasm_bindgen::prelude::*;
use sha2::{Sha256, Digest};
use serde::{Deserialize, Serialize};

// ============================================================
// Internal hash helpers  (identical to agent.rs)
// ============================================================

type Hash = [u8; 32];

fn sha256(data: &[u8]) -> Hash {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().into()
}

fn sha256_pair(a: &Hash, b: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update(a);
    h.update(b);
    h.finalize().into()
}

fn from_hex(s: &str) -> Option<Hash> {
    if s.len() != 64 { return None; }
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

fn to_hex(h: &Hash) -> String {
    h.iter().map(|b| format!("{:02x}", b)).collect()
}

// ============================================================
// Entry type — matches the JSON shape from the Agent API
// ============================================================

#[derive(Deserialize)]
struct Entry {
    tick:      u32,
    action:    String,            // "Observe" | "Explore" | "Rest" | "Reflect"
    event:     serde_json::Value, // SecurityEvent as serde JSON
    note:      String,
    hash:      String,            // 64-char lowercase hex
    prev_hash: String,            // 64-char lowercase hex
}

// ============================================================
// Reconstruct Rust Debug representations
//
// The Rust agent hashes format!("{:?}", action) and format!("{:?}", event).
// For the four unit variants of Action, the Debug string equals the variant
// name — same as the JSON string serde produces.  For SecurityEvent we need
// to convert from serde's JSON shape back to the Rust Debug string.
// ============================================================

/// Convert a SecurityEvent JSON value to its Rust `{:?}` Debug string.
/// Must match the output of `format!("{:?}", event)` in agent.rs exactly.
fn event_debug(v: &serde_json::Value) -> String {
    let obj = match v.as_object() {
        Some(o) => o,
        None    => return format!("{:?}", v),
    };

    // Custom("msg")
    if let Some(msg) = obj.get("Custom").and_then(|m| m.as_str()) {
        return format!("Custom({:?})", msg);
    }

    // NetworkConnection { src: "...", dst: "...", port: N }
    if let Some(nc) = obj.get("NetworkConnection") {
        let src  = nc["src"].as_str().unwrap_or("");
        let dst  = nc["dst"].as_str().unwrap_or("");
        let port = nc["port"].as_u64().unwrap_or(0) as u16;
        return format!(
            "NetworkConnection {{ src: {:?}, dst: {:?}, port: {} }}",
            src, dst, port
        );
    }

    // FileAccess { path: "...", operation: "..." }
    if let Some(fa) = obj.get("FileAccess") {
        let path = fa["path"].as_str().unwrap_or("");
        let op   = fa["operation"].as_str().unwrap_or("");
        return format!("FileAccess {{ path: {:?}, operation: {:?} }}", path, op);
    }

    // Authentication { user: "...", success: bool }
    if let Some(auth) = obj.get("Authentication") {
        let user    = auth["user"].as_str().unwrap_or("");
        let success = auth["success"].as_bool().unwrap_or(false);
        return format!("Authentication {{ user: {:?}, success: {} }}", user, success);
    }

    // ProcessExecution { name: "...", pid: N }
    if let Some(pe) = obj.get("ProcessExecution") {
        let name = pe["name"].as_str().unwrap_or("");
        let pid  = pe["pid"].as_u64().unwrap_or(0) as u32;
        return format!("ProcessExecution {{ name: {:?}, pid: {} }}", name, pid);
    }

    format!("{:?}", v)
}

/// Recompute the SHA-256 hash for one entry.  Matches `Agent::compute_hash`
/// in agent.rs exactly: SHA-256(prev_hash || tick_be || action_debug || event_debug || note).
fn recompute_hash(prev: &Hash, tick: u32, action: &str, event: &serde_json::Value, note: &str) -> Hash {
    let mut h = Sha256::new();
    h.update(prev);
    h.update(tick.to_be_bytes());
    // format!("{:?}", Action::Observe) == "Observe" == the JSON string for unit variants
    h.update(action.as_bytes());
    h.update(event_debug(event).as_bytes());
    h.update(note.as_bytes());
    h.finalize().into()
}

// ============================================================
// Merkle tree helpers  (identical to MerkleTree in agent.rs)
// ============================================================

fn leaf_hash(e: &Entry) -> Hash {
    // Matches MerkleTree::from_memory:
    //   tick.to_be_bytes() || format!("{:?}", action) || note || entry.hash_bytes
    let hash_bytes = from_hex(&e.hash).unwrap_or([0u8; 32]);
    let mut data = e.tick.to_be_bytes().to_vec();
    data.extend(e.action.as_bytes()); // unit variant Debug == the variant name string
    data.extend(e.note.as_bytes());
    data.extend(&hash_bytes);
    sha256(&data)
}

fn build_merkle_root(leaves: &[Hash]) -> Hash {
    if leaves.is_empty() { return [0u8; 32]; }
    let mut level: Vec<Hash> = leaves.to_vec();
    while level.len() > 1 {
        let mut next = Vec::new();
        let mut i = 0;
        while i < level.len() {
            let left  = level[i];
            let right = if i + 1 < level.len() { level[i + 1] } else { level[i] };
            next.push(sha256_pair(&left, &right));
            i += 2;
        }
        level = next;
    }
    level[0]
}

// ============================================================
// Public WASM API
// ============================================================

/// Verify the SHA-256 hash chain over a JSON array of memory entries.
///
/// Returns `true` if every entry's `hash` matches the recomputed value and
/// every `prev_hash` matches the preceding entry's hash.  Returns `false` on
/// any mismatch (tampered or corrupted data).  Throws on malformed JSON.
#[wasm_bindgen]
pub fn verify_chain(entries_json: &str) -> Result<bool, JsError> {
    let entries: Vec<Entry> = serde_json::from_str(entries_json)
        .map_err(|e| JsError::new(&e.to_string()))?;

    let mut expected_prev = [0u8; 32];
    for entry in &entries {
        let prev = from_hex(&entry.prev_hash)
            .ok_or_else(|| JsError::new(&format!("invalid prev_hash at tick {}", entry.tick)))?;
        let stored = from_hex(&entry.hash)
            .ok_or_else(|| JsError::new(&format!("invalid hash at tick {}", entry.tick)))?;

        if prev != expected_prev {
            return Ok(false); // prev_hash linkage broken
        }
        let recomputed = recompute_hash(&prev, entry.tick, &entry.action, &entry.event, &entry.note);
        if recomputed != stored {
            return Ok(false); // hash recomputation mismatch — tamper detected
        }
        expected_prev = stored;
    }
    Ok(true)
}

/// Compute the Merkle root of a JSON array of memory entries.
///
/// Returns the root as a 64-character lowercase hex string, or 64 zeros for
/// an empty entry list.
#[wasm_bindgen]
pub fn compute_merkle_root(entries_json: &str) -> Result<String, JsError> {
    let entries: Vec<Entry> = serde_json::from_str(entries_json)
        .map_err(|e| JsError::new(&e.to_string()))?;

    if entries.is_empty() {
        return Ok("0".repeat(64));
    }
    let leaves: Vec<Hash> = entries.iter().map(leaf_hash).collect();
    Ok(to_hex(&build_merkle_root(&leaves)))
}

/// Generate a Merkle inclusion proof for the entry at `index`.
///
/// Returns a JSON string:
/// ```json
/// {
///   "index":      2,
///   "leaf_hash":  "abcd...64hex",
///   "siblings":   ["ef01...64hex", ...],
///   "directions": [true, false, ...]
/// }
/// ```
/// `directions[i] = true` means the current node is the *left* child at level i
/// (sibling is on the right).
///
/// Returns `null` if `index` is out of range.
#[wasm_bindgen]
pub fn generate_proof(entries_json: &str, index: usize) -> Result<JsValue, JsError> {
    let entries: Vec<Entry> = serde_json::from_str(entries_json)
        .map_err(|e| JsError::new(&e.to_string()))?;

    if index >= entries.len() {
        return Ok(JsValue::NULL);
    }

    let leaves: Vec<Hash> = entries.iter().map(leaf_hash).collect();
    let mut siblings   = Vec::new();
    let mut directions = Vec::new();
    let mut level      = leaves.clone();
    let mut idx        = index;

    while level.len() > 1 {
        let sib_idx = if idx.is_multiple_of(2) { idx + 1 } else { idx - 1 };
        let sibling = if sib_idx < level.len() { level[sib_idx] } else { level[idx] };
        siblings.push(to_hex(&sibling));
        directions.push(idx.is_multiple_of(2)); // true = we are on the left

        let mut next = Vec::new();
        let mut i = 0;
        while i < level.len() {
            let l = level[i];
            let r = if i + 1 < level.len() { level[i + 1] } else { level[i] };
            next.push(sha256_pair(&l, &r));
            i += 2;
        }
        level = next;
        idx /= 2;
    }

    #[derive(Serialize)]
    struct Proof {
        index:      usize,
        leaf_hash:  String,
        siblings:   Vec<String>,
        directions: Vec<bool>,
    }

    let proof = Proof {
        index,
        leaf_hash: to_hex(&leaves[index]),
        siblings,
        directions,
    };

    let json = serde_json::to_string(&proof).map_err(|e| JsError::new(&e.to_string()))?;
    Ok(JsValue::from_str(&json))
}

/// Verify a Merkle inclusion proof.
///
/// - `root_hex`        — expected Merkle root (64-char hex)
/// - `leaf_hex`        — leaf hash to prove inclusion of (64-char hex, from `generate_proof`)
/// - `siblings_json`   — JSON array of sibling hashes (64-char hex strings)
/// - `directions_json` — JSON array of booleans matching the `generate_proof` output
///
/// Returns `true` if the proof is valid for the given root.
#[wasm_bindgen]
pub fn verify_merkle_proof(
    root_hex:        &str,
    leaf_hex:        &str,
    siblings_json:   &str,
    directions_json: &str,
) -> Result<bool, JsError> {
    let root     = from_hex(root_hex).ok_or_else(|| JsError::new("invalid root hex"))?;
    let mut curr = from_hex(leaf_hex).ok_or_else(|| JsError::new("invalid leaf hex"))?;

    let siblings:   Vec<String> = serde_json::from_str(siblings_json)
        .map_err(|e| JsError::new(&e.to_string()))?;
    let directions: Vec<bool>   = serde_json::from_str(directions_json)
        .map_err(|e| JsError::new(&e.to_string()))?;

    if siblings.len() != directions.len() {
        return Err(JsError::new("siblings and directions must have the same length"));
    }

    for (sib_hex, is_left) in siblings.iter().zip(directions.iter()) {
        let sib = from_hex(sib_hex)
            .ok_or_else(|| JsError::new(&format!("invalid sibling hex: {}", sib_hex)))?;
        curr = if *is_left {
            sha256_pair(&curr, &sib)
        } else {
            sha256_pair(&sib, &curr)
        };
    }
    Ok(curr == root)
}

// ============================================================
// Tests (run with: wasm-pack test --node)
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_bindgen_test::wasm_bindgen_test;

    fn make_entry_json(tick: u32, prev_hex: &str) -> (String, String) {
        let prev = from_hex(prev_hex).unwrap();
        let event_val: serde_json::Value = serde_json::json!({"Custom": "test event"});
        let action = "Observe";
        let note   = "test note";
        let hash   = recompute_hash(&prev, tick, action, &event_val, note);
        let hash_hex = to_hex(&hash);
        let json = format!(
            r#"{{"tick":{},"action":"{}","event":{{"Custom":"test event"}},"note":"{}","hash":"{}","prev_hash":"{}"}}"#,
            tick, action, note, hash_hex, prev_hex
        );
        (json, hash_hex)
    }

    #[wasm_bindgen_test]
    fn test_verify_chain_empty() {
        assert!(verify_chain("[]").unwrap());
    }

    #[wasm_bindgen_test]
    fn test_verify_chain_single_entry() {
        let zero_hex = "0".repeat(64);
        let (json, _) = make_entry_json(1, &zero_hex);
        let arr = format!("[{}]", json);
        assert!(verify_chain(&arr).unwrap());
    }

    #[wasm_bindgen_test]
    fn test_verify_chain_three_entries() {
        let zero_hex = "0".repeat(64);
        let (e1, h1) = make_entry_json(1, &zero_hex);
        let (e2, h2) = make_entry_json(2, &h1);
        let (e3, _)  = make_entry_json(3, &h2);
        let arr = format!("[{},{},{}]", e1, e2, e3);
        assert!(verify_chain(&arr).unwrap());
    }

    #[wasm_bindgen_test]
    fn test_verify_chain_detects_tamper() {
        let zero_hex = "0".repeat(64);
        let (e1, h1) = make_entry_json(1, &zero_hex);
        let (e2, _)  = make_entry_json(2, &h1);
        // Corrupt e1 by changing the note without recomputing hash
        let e1_tampered = e1.replace("test note", "tampered note");
        let arr = format!("[{},{}]", e1_tampered, e2);
        assert!(!verify_chain(&arr).unwrap());
    }

    #[wasm_bindgen_test]
    fn test_merkle_root_empty() {
        let root = compute_merkle_root("[]").unwrap();
        assert_eq!(root, "0".repeat(64));
    }

    #[wasm_bindgen_test]
    fn test_merkle_root_deterministic() {
        let zero_hex = "0".repeat(64);
        let (e1, h1) = make_entry_json(1, &zero_hex);
        let (e2, _)  = make_entry_json(2, &h1);
        let arr = format!("[{},{}]", e1, e2);
        let r1 = compute_merkle_root(&arr).unwrap();
        let r2 = compute_merkle_root(&arr).unwrap();
        assert_eq!(r1, r2);
    }

    #[wasm_bindgen_test]
    fn test_proof_roundtrip() {
        let zero_hex = "0".repeat(64);
        let (e1, h1) = make_entry_json(1, &zero_hex);
        let (e2, h2) = make_entry_json(2, &h1);
        let (e3, _)  = make_entry_json(3, &h2);
        let arr = format!("[{},{},{}]", e1, e2, e3);

        let root = compute_merkle_root(&arr).unwrap();
        let proof_json = generate_proof(&arr, 1).unwrap();
        let proof: serde_json::Value = serde_json::from_str(proof_json.as_string().unwrap().as_str()).unwrap();

        let leaf       = proof["leaf_hash"].as_str().unwrap();
        let siblings   = serde_json::to_string(&proof["siblings"]).unwrap();
        let directions = serde_json::to_string(&proof["directions"]).unwrap();

        assert!(verify_merkle_proof(&root, leaf, &siblings, &directions).unwrap());
    }

    #[wasm_bindgen_test]
    fn test_proof_out_of_range_returns_null() {
        let result = generate_proof("[]", 0).unwrap();
        assert!(result.is_null());
    }
}


// ============================================================
// Cross-crate parity guard (native `cargo test`)
//
// Ensures this crate's JSON-based reimplementation stays byte-for-byte
// compatible with the canonical `echoes` agent core, using the real `echoes`
// crate as the source of truth. Runs under plain `cargo test`; the
// #[wasm_bindgen_test] tests above run separately under wasm-pack.
// ============================================================
#[cfg(all(test, not(target_arch = "wasm32")))]
mod parity_tests {
    use super::*;
    use echoes::agent::{Agent, SecurityEvent};

    // event_debug(JSON) must equal agent.rs's format!("{:?}", event) for every
    // SecurityEvent variant — that string is hashed, so drift breaks verify.
    #[test]
    fn event_debug_matches_agent_debug() {
        let cases = [
            SecurityEvent::Custom("environment scan performed".to_string()),
            SecurityEvent::NetworkConnection { src: "10.0.0.2".to_string(), dst: "1.1.1.1".to_string(), port: 443 },
            SecurityEvent::FileAccess { path: "/etc/shadow".to_string(), operation: "read".to_string() },
            SecurityEvent::Authentication { user: "root".to_string(), success: false },
            SecurityEvent::ProcessExecution { name: "sshd".to_string(), pid: 4242 },
        ];
        for ev in &cases {
            let json = serde_json::to_value(ev).unwrap();
            assert_eq!(event_debug(&json), format!("{:?}", ev), "event_debug drift for {:?}", ev);
        }
    }

    // A chain built by the real agent must recompute + Merkle-root identically
    // through this crate's JSON path.
    #[test]
    fn wasm_recompute_matches_agent_chain() {
        let mut agent = Agent::new("Parity", "cross-crate parity check");
        for _ in 0..6 { agent.think(); }

        // Per-entry hash + prev_hash linkage parity.
        let mut prev: Hash = [0u8; 32];
        for e in &agent.memory {
            let action = format!("{:?}", e.action);
            let event = serde_json::to_value(&e.event).unwrap();
            assert_eq!(prev, e.prev_hash, "prev_hash mismatch at tick {}", e.tick);
            assert_eq!(recompute_hash(&prev, e.tick, &action, &event, &e.note), e.hash,
                       "hash recompute drift at tick {}", e.tick);
            prev = e.hash;
        }

        // Merkle-root parity via leaf_hash + build_merkle_root.
        let leaves: Vec<Hash> = agent.memory.iter().map(|e| {
            leaf_hash(&Entry {
                tick: e.tick,
                action: format!("{:?}", e.action),
                event: serde_json::to_value(&e.event).unwrap(),
                note: e.note.clone(),
                hash: to_hex(&e.hash),
                prev_hash: to_hex(&e.prev_hash),
            })
        }).collect();
        assert_eq!(build_merkle_root(&leaves), agent.merkle_root(), "merkle root drift");
    }
}
