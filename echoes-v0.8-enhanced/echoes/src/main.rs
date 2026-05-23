use sha2::{Sha256, Digest};
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Observe,
    Explore,
    Rest,
    Reflect,
}

impl fmt::Display for Action {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Action::Observe => write!(f, "observing"),
            Action::Explore => write!(f, "exploring"),
            Action::Rest => write!(f, "resting"),
            Action::Reflect => write!(f, "reflecting"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct MemoryEntry {
    pub tick: u32,
    pub action: Action,
    pub note: String,
    pub hash: [u8; 32],      // SHA-256 hash of this entry
    pub prev_hash: [u8; 32], // Previous entry's hash
}

#[derive(Debug, Clone)]
pub struct AuditEntry {
    pub tick: u32,
    pub event: String,
}

#[derive(Debug)]
pub struct Agent {
    pub name: String,
    goal: String,           // now private → immutable after creation
    memory: Vec<MemoryEntry>,
    audit_log: Vec<AuditEntry>,
    tick: u32,
    last_hash: [u8; 32],
}

impl Agent {
    pub fn new(name: &str, goal: &str) -> Self {
        let mut agent = Agent {
            name: name.to_string(),
            goal: goal.to_string(),
            memory: Vec::new(),
            audit_log: Vec::new(),
            tick: 0,
            last_hash: [0u8; 32], // genesis hash
        };

        agent.audit_log.push(AuditEntry {
            tick: 0,
            event: format!("Agent '{}' created with goal: {}", name, goal),
        });

        agent
    }

    /// Goal is immutable after creation (security decision)
    pub fn goal(&self) -> &str {
        &self.goal
    }

    /// Compute SHA-256 hash chain entry
    fn compute_hash(prev_hash: &[u8; 32], tick: u32, action: &Action, note: &str) -> [u8; 32] {
        let mut hasher = Sha256::new();
        hasher.update(prev_hash);
        hasher.update(tick.to_be_bytes());
        hasher.update(format!("{:?}", action).as_bytes());
        hasher.update(note.as_bytes());
        let result = hasher.finalize();
        result.into()
    }

    /// Helper to display short hash for readability
    fn short_hash(hash: &[u8; 32]) -> String {
        format!("{:02x}{:02x}..{:02x}{:02x}", hash[0], hash[1], hash[30], hash[31])
    }

    pub fn think(&mut self) {
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
            Action::Rest => "conserving energy".to_string(),
            Action::Reflect => "reviewing history".to_string(),
        };

        // Cryptographic hash chain
        let new_hash = Self::compute_hash(&self.last_hash, self.tick, &action, &note);

        self.memory.push(MemoryEntry {
            tick: self.tick,
            action: action.clone(),
            note: note.clone(),
            hash: new_hash,
            prev_hash: self.last_hash,
        });

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
            Self::short_hash(&new_hash)
        );
    }

    /// Verify the cryptographic integrity of the entire memory chain
    pub fn verify_integrity(&self) -> bool {
        let mut expected_prev = [0u8; 32];

        for entry in &self.memory {
            if entry.prev_hash != expected_prev {
                return false;
            }
            let recomputed =
                Self::compute_hash(&entry.prev_hash, entry.tick, &entry.action, &entry.note);
            if recomputed != entry.hash {
                return false;
            }
            expected_prev = entry.hash;
        }
        true
    }

    pub fn audit_log(&self) -> &[AuditEntry] {
        &self.audit_log
    }

    pub fn memory_len(&self) -> usize {
        self.memory.len()
    }

    /// Pretty-print the full cryptographic memory chain
    pub fn print_memory_chain(&self) {
        println!("\n--- Memory Chain for {} (goal: {}) ---", self.name, self.goal());
        if self.memory.is_empty() {
            println!("  (no memories yet)");
            return;
        }
        for (i, entry) in self.memory.iter().enumerate() {
            println!(
                "  [{:02}] Tick {:02} | {:<10} | {} \n       hash: {}  prev: {}",
                i,
                entry.tick,
                format!("{:?}", entry.action),
                entry.note,
                Self::short_hash(&entry.hash),
                Self::short_hash(&entry.prev_hash)
            );
        }
    }

    /// Pretty-print the append-only audit log
    pub fn print_audit_log(&self) {
        println!("\n--- Audit Log for {} ---", self.name);
        for entry in &self.audit_log {
            println!("  [Tick {:02}] {}", entry.tick, entry.event);
        }
    }
}

fn main() {
    println!("=== echoes v0.8 (hash chain + tests + pretty printing) ===\n");

    let mut agent = Agent::new("Echo", "map environment with cryptographic memory");

    for _ in 0..8 {
        agent.think();
    }

    println!("\n=== Integrity Verification ===");
    if agent.verify_integrity() {
        println!("Memory chain integrity: PASSED ✓");
    } else {
        println!("Memory chain integrity: FAILED ✗");
    }

    println!("Total memories recorded: {}", agent.memory_len());

    // Showcase new pretty printers
    agent.print_audit_log();
    agent.print_memory_chain();

    // === Tamper Detection Demo (educational) ===
    println!("\n=== Tamper Detection Demo ===");
    println!("Creating a short demo agent to show tamper detection...");

    let mut demo_agent = Agent::new("DemoAgent", "demonstrate tamper detection");
    for _ in 0..3 {
        demo_agent.think();
    }

    println!(
        "Initial integrity check: {}",
        if demo_agent.verify_integrity() {
            "PASSED ✓"
        } else {
            "FAILED ✗"
        }
    );

    // Tamper with the first memory entry's note (simulating attack or bug)
    // This is possible here because we're in the same crate; in a library this would be private.
    if let Some(entry) = demo_agent.memory.get_mut(0) {
        println!(
            "Tampering with memory entry #0 note (was: '{}')...",
            entry.note
        );
        entry.note = "TAMPERED: malicious change!".to_string();
        // Intentionally NOT updating the hash or chain — this is what an attacker would do
    }

    println!(
        "Integrity check AFTER tamper: {}",
        if demo_agent.verify_integrity() {
            "PASSED ✓ (unexpected!)"
        } else {
            "FAILED ✗ — Tampering correctly detected!"
        }
    );

    println!("\nThe hash chain protects historical memory from silent modification.");
}

// ============================================================
// Unit Tests (run with: cargo test)
// ============================================================
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integrity_passes_after_normal_operation() {
        let mut agent = Agent::new("TestAgent", "run integrity test");
        for _ in 0..6 {
            agent.think();
        }
        assert!(
            agent.verify_integrity(),
            "Memory chain should be valid after normal operation"
        );
        assert_eq!(agent.memory_len(), 6);
    }

    #[test]
    fn tamper_is_detected() {
        let mut agent = Agent::new("TamperTest", "detect modification");
        for _ in 0..4 {
            agent.think();
        }
        assert!(agent.verify_integrity());

        // Tamper with an early entry
        if let Some(entry) = agent.memory.get_mut(1) {
            entry.note = "I was tampered with!".to_string();
        }

        assert!(
            !agent.verify_integrity(),
            "verify_integrity must return false after tampering"
        );
    }

    #[test]
    fn goal_is_read_only() {
        let agent = Agent::new("GoalTest", "immutable goal test");
        assert_eq!(agent.goal(), "immutable goal test");
        // goal field is private — cannot be mutated from outside the module
    }

    #[test]
    fn hash_chain_links_correctly() {
        let mut agent = Agent::new("ChainTest", "verify linking");
        agent.think();
        agent.think();

        let mem = &agent.memory; // accessible in tests (same crate)
        assert_eq!(mem.len(), 2);
        assert_eq!(mem[0].prev_hash, [0u8; 32]); // genesis
        assert_eq!(mem[1].prev_hash, mem[0].hash); // proper chaining
    }
}
