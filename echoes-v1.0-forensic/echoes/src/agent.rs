//! Core agent types: memory, hash chain, Merkle tree, forensic events.
//!
//! Everything here is pure computation — no I/O, no persistence. The store
//! module handles SQLite; the CLI wires them together.

use sha2::{Sha256, Digest};
use serde::{Deserialize, Serialize};
use std::fmt;

// ============================================================
// Forensic event types
// ============================================================

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum SecurityEvent {
    NetworkConnection {
        src: String,
        dst: String,
        port: u16,
    },
    FileAccess {
        path: String,
        operation: String,
    },
    Authentication {
        user: String,
        success: bool,
    },
    ProcessExecution {
        name: String,
        pid: u32,
    },
    Custom(String),
}

impl fmt::Display for SecurityEvent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SecurityEvent::NetworkConnection { src, dst, port } => {
                write!(f, "network {} -> {}:{}", src, dst, port)
            }
            SecurityEvent::FileAccess { path, operation } => {
                write!(f, "file {} {}", operation, path)
            }
            SecurityEvent::Authentication { user, success } => {
                write!(f, "auth user={} success={}", user, success)
            }
            SecurityEvent::ProcessExecution { name, pid } => {
                write!(f, "process {} (pid={})", name, pid)
            }
            SecurityEvent::Custom(msg) => write!(f, "{}", msg),
        }
    }
}

// ============================================================
// Hash helpers
// ============================================================

pub type Hash = [u8; 32];

pub fn hash_data(data: &[u8]) -> Hash {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finalize().into()
}

pub fn hash_pair(left: &Hash, right: &Hash) -> Hash {
    let mut hasher = Sha256::new();
    hasher.update(left);
    hasher.update(right);
    hasher.finalize().into()
}

pub fn short_hash(hash: &Hash) -> String {
    format!("{:02x}{:02x}..{:02x}{:02x}", hash[0], hash[1], hash[30], hash[31])
}

/// Hash sealing a pruned prefix of the memory chain (see ADR-002 Phase 7b).
///
/// Binds the previous checkpoint hash (zeros for the first checkpoint), the
/// agent name, the pruned range, the head hash of the pruned prefix, and the
/// Merkle root over the pruned entries. Checkpoints therefore form their own
/// hash chain: forging or reordering any sealed prefix changes every later
/// checkpoint hash.
pub fn checkpoint_hash(
    prev_checkpoint: &Hash,
    agent_name: &str,
    pruned_through_tick: u32,
    entries_sealed: u32,
    head: &Hash,
    merkle_root: &Hash,
) -> Hash {
    let mut hasher = Sha256::new();
    hasher.update(prev_checkpoint);
    hasher.update(agent_name.as_bytes());
    hasher.update(pruned_through_tick.to_be_bytes());
    hasher.update(entries_sealed.to_be_bytes());
    hasher.update(head);
    hasher.update(merkle_root);
    hasher.finalize().into()
}

// ============================================================
// Agent action enum
// ============================================================

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Action {
    Observe,
    Explore,
    Rest,
    Reflect,
}

impl fmt::Display for Action {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Action::Observe  => write!(f, "observing"),
            Action::Explore  => write!(f, "exploring"),
            Action::Rest     => write!(f, "resting"),
            Action::Reflect  => write!(f, "reflecting"),
        }
    }
}

// ============================================================
// Memory entry — one tick of agent state, hash-chained
// ============================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryEntry {
    pub tick: u32,
    pub action: Action,
    pub event: SecurityEvent,
    pub note: String,
    pub hash: Hash,
    pub prev_hash: Hash,
}

#[derive(Debug, Clone)]
pub struct AuditEntry {
    pub tick: u32,
    pub event: String,
}

// ============================================================
// Agent
// ============================================================

#[derive(Debug)]
pub struct Agent {
    pub name: String,
    goal: String,
    pub memory: Vec<MemoryEntry>,
    pub audit_log: Vec<AuditEntry>,
    pub tick: u32,
    pub last_hash: Hash,
    /// Trusted genesis for chain verification: all zeros for a full chain,
    /// or the head hash of the last pruned prefix when the store holds a
    /// checkpoint (see `checkpoint_hash`).
    pub base_hash: Hash,
}

impl Agent {
    /// Create a fresh agent (no prior memory).
    pub fn new(name: &str, goal: &str) -> Self {
        let mut agent = Agent {
            name: name.to_string(),
            goal: goal.to_string(),
            memory: Vec::new(),
            audit_log: Vec::new(),
            tick: 0,
            last_hash: [0u8; 32],
            base_hash: [0u8; 32],
        };
        agent.audit_log.push(AuditEntry {
            tick: 0,
            event: format!("Agent '{}' created with goal: {}", name, goal),
        });
        agent
    }

    /// Reconstruct an agent from persisted memory entries.
    ///
    /// Verifies the full chain before accepting — returns `Err` if integrity
    /// fails, which means the stored memory was tampered with or corrupted.
    pub fn restore(name: &str, goal: &str, entries: Vec<MemoryEntry>) -> Result<Self, String> {
        Self::restore_from(name, goal, entries, [0u8; 32], 0)
    }

    /// Like [`restore`](Self::restore), but verifies the chain against a
    /// non-zero trusted genesis — the head hash of a pruned prefix sealed by
    /// a checkpoint. `base_tick` is the tick of that head entry, used as the
    /// starting tick when no live entries remain.
    pub fn restore_from(
        name: &str,
        goal: &str,
        entries: Vec<MemoryEntry>,
        base_hash: Hash,
        base_tick: u32,
    ) -> Result<Self, String> {
        let mut agent = Agent {
            name: name.to_string(),
            goal: goal.to_string(),
            memory: entries,
            audit_log: Vec::new(),
            tick: base_tick,
            last_hash: base_hash,
            base_hash,
        };

        if !agent.verify_integrity() {
            return Err(
                "stored memory chain is CORRUPT — possible tamper or incomplete write. \
                 Refusing to continue. Restore from backup or start a new DB."
                    .to_string(),
            );
        }

        // Fast-forward internal state to match the last stored entry.
        if let Some(last) = agent.memory.last() {
            agent.tick = last.tick;
            agent.last_hash = last.hash;
        }

        agent.audit_log.push(AuditEntry {
            tick: agent.tick,
            event: format!(
                "Agent '{}' restored from {} persisted entries.",
                name,
                agent.memory.len()
            ),
        });

        Ok(agent)
    }

    pub fn goal(&self) -> &str {
        &self.goal
    }

    fn compute_hash(
        prev_hash: &Hash,
        tick: u32,
        action: &Action,
        event: &SecurityEvent,
        note: &str,
    ) -> Hash {
        let mut hasher = Sha256::new();
        hasher.update(prev_hash);
        hasher.update(tick.to_be_bytes());
        hasher.update(format!("{:?}", action).as_bytes());
        hasher.update(format!("{:?}", event).as_bytes());
        hasher.update(note.as_bytes());
        hasher.finalize().into()
    }

    /// Advance the agent by one tick. Returns the new `MemoryEntry` so the
    /// caller (store) can persist it.
    ///
    /// Uses synthetic `Custom(...)` events. For real events, use
    /// [`think_with`](Self::think_with).
    pub fn think(&mut self) -> MemoryEntry {
        self.think_with(None::<&mut crate::sensor::ProcessScanner>)
    }

    /// Advance the agent by one tick, optionally drawing a real event from
    /// `source`.  If `source` is `None` or yields `None`, a synthetic
    /// `Custom(...)` event is used — identical to the plain `think()` path.
    ///
    /// The `action` is still determined by the agent's internal state; the
    /// event source only replaces the *event* field of the resulting entry.
    pub fn think_with<S: crate::sensor::EventSource>(
        &mut self,
        mut source: Option<&mut S>,
    ) -> MemoryEntry {
        self.tick += 1;

        let action = if self.memory.len() < 2 {
            Action::Observe
        } else if self.memory.len() < 4 {
            Action::Explore
        } else if self.memory.len() == 4 {
            Action::Rest
        } else {
            Action::Reflect
        };

        let note = match &action {
            Action::Observe => "scanning environment".to_string(),
            Action::Explore => format!("working toward goal: {}", self.goal()),
            Action::Rest    => "conserving energy".to_string(),
            Action::Reflect => "reviewing history".to_string(),
        };

        // Try to get a real event from the sensor; fall back to synthetic.
        let event = source
            .as_mut()
            .and_then(|s| s.poll())
            .unwrap_or_else(|| match &action {
                Action::Observe => SecurityEvent::Custom("environment scan performed".to_string()),
                Action::Explore => SecurityEvent::Custom("exploring toward goal".to_string()),
                Action::Rest    => SecurityEvent::Custom("agent resting".to_string()),
                Action::Reflect => SecurityEvent::Custom("reflecting on history".to_string()),
            });

        let new_hash = Self::compute_hash(&self.last_hash, self.tick, &action, &event, &note);

        let entry = MemoryEntry {
            tick: self.tick,
            action: action.clone(),
            event: event.clone(),
            note: note.clone(),
            hash: new_hash,
            prev_hash: self.last_hash,
        };

        self.memory.push(entry.clone());
        self.last_hash = new_hash;

        self.audit_log.push(AuditEntry {
            tick: self.tick,
            event: format!("{} performed: {}", self.name, action),
        });

        println!(
            "[Tick {:02}] {} is {} → {}  [hash: {}]",
            self.tick,
            self.name,
            action,
            note,
            short_hash(&new_hash)
        );

        entry
    }

    /// Verify the SHA-256 chain over all in-memory entries, starting from
    /// `base_hash` (all zeros unless restored above a checkpoint).
    pub fn verify_integrity(&self) -> bool {
        let mut expected_prev = self.base_hash;
        for entry in &self.memory {
            if entry.prev_hash != expected_prev {
                return false;
            }
            let recomputed = Self::compute_hash(
                &entry.prev_hash,
                entry.tick,
                &entry.action,
                &entry.event,
                &entry.note,
            );
            if recomputed != entry.hash {
                return false;
            }
            expected_prev = entry.hash;
        }
        true
    }

    pub fn memory_len(&self) -> usize {
        self.memory.len()
    }

    pub fn merkle_root(&self) -> Hash {
        MerkleTree::from_memory(&self.memory).root()
    }

    pub fn print_memory_chain(&self) {
        println!("\n--- Memory Chain for {} (goal: {}) ---", self.name, self.goal());
        if self.memory.is_empty() {
            println!("  (no memories yet)");
            return;
        }
        for (i, entry) in self.memory.iter().enumerate() {
            println!(
                "  [{:02}] Tick {:02} | {:<10} | {}\n       hash: {}  prev: {}",
                i,
                entry.tick,
                format!("{:?}", entry.action),
                entry.note,
                short_hash(&entry.hash),
                short_hash(&entry.prev_hash)
            );
        }
    }

    pub fn print_audit_log(&self) {
        println!("\n--- Audit Log for {} ---", self.name);
        for entry in &self.audit_log {
            println!("  [Tick {:02}] {}", entry.tick, entry.event);
        }
    }
}

// ============================================================
// Merkle Tree
// ============================================================

#[derive(Debug, Clone)]
pub struct MerkleProof {
    pub index: usize,
    pub leaf_hash: Hash,
    pub siblings: Vec<Hash>,
    pub directions: Vec<bool>,
}

#[derive(Debug, Clone)]
pub struct MerkleTree {
    leaves: Vec<Hash>,
    root: Hash,
}

impl MerkleTree {
    pub fn from_memory(entries: &[MemoryEntry]) -> Self {
        if entries.is_empty() {
            return MerkleTree { leaves: vec![], root: [0u8; 32] };
        }
        let leaves: Vec<Hash> = entries.iter().map(|e| {
            let mut data = e.tick.to_be_bytes().to_vec();
            data.extend(format!("{:?}", e.action).as_bytes());
            data.extend(e.note.as_bytes());
            data.extend(&e.hash);
            hash_data(&data)
        }).collect();

        let root = Self::build_root(&leaves);
        MerkleTree { leaves, root }
    }

    fn build_root(leaves: &[Hash]) -> Hash {
        if leaves.is_empty() { return [0u8; 32]; }
        let mut level: Vec<Hash> = leaves.to_vec();
        while level.len() > 1 {
            let mut next = Vec::new();
            for i in (0..level.len()).step_by(2) {
                let left  = level[i];
                let right = if i + 1 < level.len() { level[i + 1] } else { level[i] };
                next.push(hash_pair(&left, &right));
            }
            level = next;
        }
        level[0]
    }

    pub fn root(&self) -> Hash { self.root }

    pub fn short_root(&self) -> String { short_hash(&self.root) }

    pub fn generate_proof(&self, index: usize) -> Option<MerkleProof> {
        if index >= self.leaves.len() { return None; }
        let mut proof = MerkleProof {
            index,
            leaf_hash: self.leaves[index],
            siblings: vec![],
            directions: vec![],
        };
        let mut level: Vec<Hash> = self.leaves.clone();
        let mut idx = index;
        while level.len() > 1 {
            let sib_idx = if idx.is_multiple_of(2) { idx + 1 } else { idx - 1 };
            let sibling = if sib_idx < level.len() { level[sib_idx] } else { level[idx] };
            proof.siblings.push(sibling);
            proof.directions.push(idx.is_multiple_of(2));
            let mut next = Vec::new();
            for i in (0..level.len()).step_by(2) {
                let l = level[i];
                let r = if i + 1 < level.len() { level[i + 1] } else { level[i] };
                next.push(hash_pair(&l, &r));
            }
            level = next;
            idx /= 2;
        }
        Some(proof)
    }

    pub fn verify_proof(root: &Hash, proof: &MerkleProof) -> bool {
        let mut current = proof.leaf_hash;
        for (sibling, is_left) in proof.siblings.iter().zip(proof.directions.iter()) {
            current = if *is_left {
                hash_pair(&current, sibling)
            } else {
                hash_pair(sibling, &current)
            };
        }
        current == *root
    }
}

// ============================================================
// Unit tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integrity_passes_after_normal_operation() {
        let mut agent = Agent::new("TestAgent", "run integrity test");
        for _ in 0..6 { agent.think(); }
        assert!(agent.verify_integrity());
        assert_eq!(agent.memory_len(), 6);
    }

    #[test]
    fn tamper_is_detected() {
        let mut agent = Agent::new("TamperTest", "detect modification");
        for _ in 0..4 { agent.think(); }
        assert!(agent.verify_integrity());
        agent.memory[1].note = "I was tampered with!".to_string();
        assert!(!agent.verify_integrity());
    }

    #[test]
    fn goal_is_read_only() {
        let agent = Agent::new("GoalTest", "immutable goal test");
        assert_eq!(agent.goal(), "immutable goal test");
    }

    #[test]
    fn hash_chain_links_correctly() {
        let mut agent = Agent::new("ChainTest", "verify linking");
        agent.think();
        agent.think();
        assert_eq!(agent.memory[0].prev_hash, [0u8; 32]);
        assert_eq!(agent.memory[1].prev_hash, agent.memory[0].hash);
    }

    #[test]
    fn merkle_root_changes_on_tamper() {
        let mut agent = Agent::new("MerkleTest", "merkle root test");
        for _ in 0..4 { agent.think(); }
        let root_before = agent.merkle_root();
        agent.memory[0].note = "tampered".to_string();
        let root_after = agent.merkle_root();
        assert_ne!(root_before, root_after);
    }

    #[test]
    fn merkle_proof_verification() {
        let mut agent = Agent::new("ProofTest", "merkle proof test");
        for _ in 0..5 { agent.think(); }
        let tree = MerkleTree::from_memory(&agent.memory);
        let root = tree.root();

        // Every leaf produces a proof that verifies against the honest root.
        for index in 0..agent.memory.len() {
            let proof = tree
                .generate_proof(index)
                .expect("a valid leaf index must yield a proof");
            assert_eq!(proof.index, index);
            assert!(
                MerkleTree::verify_proof(&root, &proof),
                "valid proof for leaf {index} failed to verify"
            );
        }

        // A proof whose leaf hash has been corrupted must not verify.
        let mut corrupted = tree.generate_proof(2).expect("leaf 2 exists");
        corrupted.leaf_hash[0] ^= 0xff;
        assert!(
            !MerkleTree::verify_proof(&root, &corrupted),
            "a proof with a corrupted leaf hash must not verify"
        );

        // A proof taken before a mutation must not verify against the new root.
        let stale = tree.generate_proof(0).expect("leaf 0 exists");
        agent.memory[0].note = "tampered".to_string();
        let mutated_root = MerkleTree::from_memory(&agent.memory).root();
        assert!(
            !MerkleTree::verify_proof(&mutated_root, &stale),
            "a stale proof must not verify against a mutated root"
        );

        // Out-of-range indices yield no proof.
        assert!(tree.generate_proof(agent.memory.len()).is_none());
    }

    #[test]
    fn restore_from_verifies_against_nonzero_base() {
        let mut agent = Agent::new("BaseTest", "checkpoint base test");
        for _ in 0..6 { agent.think(); }

        // Simulate pruning the first 3 entries: the suffix chain must verify
        // against the head hash of the pruned prefix, not zeros.
        let base_hash = agent.memory[2].hash;
        let base_tick = agent.memory[2].tick;
        let suffix: Vec<MemoryEntry> = agent.memory[3..].to_vec();

        let restored = Agent::restore_from(
            "BaseTest", "checkpoint base test", suffix.clone(), base_hash, base_tick,
        ).expect("suffix must verify against pruned-prefix head");
        assert_eq!(restored.memory_len(), 3);
        assert_eq!(restored.tick, 6);
        assert!(restored.verify_integrity());

        // The same suffix must NOT verify against a zero genesis...
        assert!(Agent::restore("BaseTest", "checkpoint base test", suffix.clone()).is_err());

        // ...nor against the wrong base, nor when tampered.
        let mut wrong_base = base_hash;
        wrong_base[0] ^= 0xff;
        assert!(Agent::restore_from(
            "BaseTest", "checkpoint base test", suffix.clone(), wrong_base, base_tick,
        ).is_err());

        let mut tampered = suffix;
        tampered[1].note = "tampered".to_string();
        assert!(Agent::restore_from(
            "BaseTest", "checkpoint base test", tampered, base_hash, base_tick,
        ).is_err());
    }

    #[test]
    fn restore_from_empty_entries_adopts_base_state() {
        let restored = Agent::restore_from("Empty", "g", vec![], [7u8; 32], 42)
            .expect("empty suffix is trivially valid");
        assert_eq!(restored.memory_len(), 0);
        assert_eq!(restored.tick, 42);
        assert_eq!(restored.last_hash, [7u8; 32]);
        assert!(restored.verify_integrity());
    }

    #[test]
    fn checkpoint_hash_is_sensitive_to_every_field() {
        let zeros = [0u8; 32];
        let head = [1u8; 32];
        let merkle = [2u8; 32];
        let base = checkpoint_hash(&zeros, "A", 10, 10, &head, &merkle);

        assert_ne!(base, checkpoint_hash(&[9u8; 32], "A", 10, 10, &head, &merkle));
        assert_ne!(base, checkpoint_hash(&zeros, "B", 10, 10, &head, &merkle));
        assert_ne!(base, checkpoint_hash(&zeros, "A", 11, 10, &head, &merkle));
        assert_ne!(base, checkpoint_hash(&zeros, "A", 10, 11, &head, &merkle));
        assert_ne!(base, checkpoint_hash(&zeros, "A", 10, 10, &[3u8; 32], &merkle));
        assert_ne!(base, checkpoint_hash(&zeros, "A", 10, 10, &head, &[4u8; 32]));

        // Deterministic.
        assert_eq!(base, checkpoint_hash(&zeros, "A", 10, 10, &head, &merkle));
    }
}
