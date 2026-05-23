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
        } else if self.memory.len() < 5 {
            Action::Explore
        } else {
            Action::Reflect
        };

        let note = match &action {
            Action::Observe => "scanning environment".to_string(),
            Action::Explore => format!("working toward goal: {}", self.goal()),
            Action::Reflect => "reviewing history".to_string(),
            _ => String::new(),
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
            "[Tick {:02}] {} is {}  {}  [hash: {}]",
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
}

fn main() {
    println!("=== echoes v0.6 (immutable goal + SHA-256) ===\n");

    let mut agent = Agent::new("Echo", "map environment with cryptographic memory");

    for _ in 0..8 {
        agent.think();
    }

    println!("\n=== Integrity Verification ===");
    if agent.verify_integrity() {
        println!("Memory chain integrity: PASSED ");
    } else {
        println!("Memory chain integrity: FAILED ");
    }

    println!("Total memories recorded: {}", agent.memory_len());
}
