//! SQLite persistence layer + remote HTTP persistence for agent memory.
//!
//! # Backends
//!
//! `SqliteStore` — local SQLite; the default.  Each agent gets its own row in
//! `agents` (name + goal + last_tick) and its memory entries in
//! `memory_entries`.  The hash field is stored as a 64-character hex string.
//!
//! `RemoteStore` — HTTP backend that persists to an automaton server's
//! `/api/agents/*` endpoints.  Used when `echoes run --remote-store URL` is
//! given.  Requires a valid `AUTOMATON_TOKEN` bearer token.
//!
//! Both backends implement the `AgentStore` trait so `cmd_run` can dispatch
//! through a `Box<dyn AgentStore>` without caring which backend is active.
//!
//! # Backward-compatibility
//!
//! `pub type Store = SqliteStore` keeps existing call-sites that use `Store`
//! working without changes.

use rusqlite::{Connection, params, Result as SqlResult};
use crate::agent::{Action, Hash, MemoryEntry, SecurityEvent};

// ============================================================
// Unified result type
// ============================================================

pub type StoreError = Box<dyn std::error::Error>;
pub type StoreResult<T> = Result<T, StoreError>;

// ============================================================
// AgentStore trait
// ============================================================

/// Abstraction over local SQLite and remote HTTP persistence.
pub trait AgentStore {
    /// Upsert the agent row (name, goal, last_tick).
    fn save_agent_meta(&self, name: &str, goal: &str, last_tick: u32) -> StoreResult<()>;

    /// Load agent metadata.  Returns `(goal, last_tick)` or `None` if unknown.
    fn load_agent_meta(&self, name: &str) -> StoreResult<Option<(String, u32)>>;

    /// Persist a single `MemoryEntry`.  Called after every `agent.think()`.
    fn save_entry(&self, agent_name: &str, entry: &MemoryEntry) -> StoreResult<()>;

    /// Load all entries for `agent_name`, ordered by tick ascending.
    fn load_entries(&self, agent_name: &str) -> StoreResult<Vec<MemoryEntry>>;
}

// ============================================================
// SqliteStore
// ============================================================

pub struct SqliteStore {
    conn: Connection,
}

/// Backward-compatible type alias.
pub type Store = SqliteStore;

impl SqliteStore {
    /// Open (or create) the SQLite database at `path`.
    pub fn open(path: &str) -> SqlResult<Self> {
        let conn = Connection::open(path)?;
        // WAL mode: safe under concurrent readers, no journal file left behind.
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;
        let store = SqliteStore { conn };
        store.init_schema()?;
        Ok(store)
    }

    /// Create tables if they don't already exist.
    fn init_schema(&self) -> SqlResult<()> {
        self.conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS agents (
                name        TEXT NOT NULL PRIMARY KEY,
                goal        TEXT NOT NULL,
                last_tick   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS memory_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name  TEXT    NOT NULL REFERENCES agents(name),
                tick        INTEGER NOT NULL,
                action      TEXT    NOT NULL,
                event_json  TEXT    NOT NULL,
                note        TEXT    NOT NULL,
                hash_hex    TEXT    NOT NULL,
                prev_hex    TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mem_agent
                ON memory_entries(agent_name, tick);
            ",
        )
    }

    // -------------------------------------------------------
    // Internal helpers called by AgentStore impl
    // -------------------------------------------------------

    fn save_agent_meta_sq(&self, name: &str, goal: &str, last_tick: u32) -> SqlResult<()> {
        self.conn.execute(
            "INSERT INTO agents (name, goal, last_tick)
             VALUES (?1, ?2, ?3)
             ON CONFLICT(name) DO UPDATE SET goal=excluded.goal,
                                             last_tick=excluded.last_tick",
            params![name, goal, last_tick],
        )?;
        Ok(())
    }

    fn load_agent_meta_sq(&self, name: &str) -> SqlResult<Option<(String, u32)>> {
        let mut stmt = self.conn.prepare(
            "SELECT goal, last_tick FROM agents WHERE name = ?1"
        )?;
        let mut rows = stmt.query(params![name])?;
        if let Some(row) = rows.next()? {
            let goal: String = row.get(0)?;
            let last_tick: u32 = row.get(1)?;
            Ok(Some((goal, last_tick)))
        } else {
            Ok(None)
        }
    }

    fn save_entry_sq(&self, agent_name: &str, entry: &MemoryEntry) -> SqlResult<()> {
        let action_str = format!("{:?}", entry.action);
        let event_json = serde_json::to_string(&entry.event)
            .unwrap_or_else(|_| "\"serialization_error\"".to_string());
        let hash_hex  = hex::encode(entry.hash);
        let prev_hex  = hex::encode(entry.prev_hash);

        self.conn.execute(
            "INSERT INTO memory_entries
                 (agent_name, tick, action, event_json, note, hash_hex, prev_hex)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                agent_name,
                entry.tick,
                action_str,
                event_json,
                entry.note,
                hash_hex,
                prev_hex,
            ],
        )?;
        Ok(())
    }

    fn load_entries_sq(&self, agent_name: &str) -> SqlResult<Vec<MemoryEntry>> {
        let mut stmt = self.conn.prepare(
            "SELECT tick, action, event_json, note, hash_hex, prev_hex
             FROM memory_entries
             WHERE agent_name = ?1
             ORDER BY tick ASC",
        )?;

        let entries = stmt.query_map(params![agent_name], |row| {
            let tick: u32          = row.get(0)?;
            let action_str: String = row.get(1)?;
            let event_json: String = row.get(2)?;
            let note: String       = row.get(3)?;
            let hash_hex: String   = row.get(4)?;
            let prev_hex: String   = row.get(5)?;

            let action    = parse_action(&action_str);
            let event     = serde_json::from_str::<SecurityEvent>(&event_json)
                .unwrap_or(SecurityEvent::Custom(event_json.clone()));
            let hash      = hex_to_hash(&hash_hex);
            let prev_hash = hex_to_hash(&prev_hex);

            Ok(MemoryEntry { tick, action, event, note, hash, prev_hash })
        })?
        .filter_map(|r| r.ok())
        .collect();

        Ok(entries)
    }
}

impl AgentStore for SqliteStore {
    fn save_agent_meta(&self, name: &str, goal: &str, last_tick: u32) -> StoreResult<()> {
        self.save_agent_meta_sq(name, goal, last_tick).map_err(Into::into)
    }

    fn load_agent_meta(&self, name: &str) -> StoreResult<Option<(String, u32)>> {
        self.load_agent_meta_sq(name).map_err(Into::into)
    }

    fn save_entry(&self, agent_name: &str, entry: &MemoryEntry) -> StoreResult<()> {
        self.save_entry_sq(agent_name, entry).map_err(Into::into)
    }

    fn load_entries(&self, agent_name: &str) -> StoreResult<Vec<MemoryEntry>> {
        self.load_entries_sq(agent_name).map_err(Into::into)
    }
}

// ============================================================
// RemoteStore — HTTP backend via automaton
// ============================================================

/// HTTP-backed agent store that persists to an automaton server.
///
/// Requires `echoes run --remote-store <URL> --token <TOKEN>`.
pub struct RemoteStore {
    base_url: String,
    token: String,
}

impl RemoteStore {
    /// `base_url` — e.g. `http://192.168.1.10:8080` (no trailing slash).
    /// `token` — the `AUTOMATON_TOKEN` value configured on the server.
    pub fn new(base_url: &str, token: &str) -> Self {
        RemoteStore {
            base_url: base_url.trim_end_matches('/').to_string(),
            token: token.to_string(),
        }
    }
}

impl AgentStore for RemoteStore {
    fn save_agent_meta(&self, name: &str, goal: &str, last_tick: u32) -> StoreResult<()> {
        let url = format!("{}/api/agents/{}/meta", self.base_url, name);
        let body = serde_json::json!({ "goal": goal, "tick": last_tick });
        ureq::post(&url)
            .set("Authorization", &format!("Bearer {}", self.token))
            .send_json(body)?;
        Ok(())
    }

    fn load_agent_meta(&self, name: &str) -> StoreResult<Option<(String, u32)>> {
        let url = format!("{}/api/agents/{}/meta", self.base_url, name);
        match ureq::get(&url)
            .set("Authorization", &format!("Bearer {}", self.token))
            .call()
        {
            Ok(resp) => {
                let body: serde_json::Value = resp.into_json()?;
                let goal  = body["goal"].as_str().unwrap_or("").to_string();
                let tick  = body["tick"].as_u64().unwrap_or(0) as u32;
                Ok(Some((goal, tick)))
            }
            Err(ureq::Error::Status(404, _)) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    fn save_entry(&self, agent_name: &str, entry: &MemoryEntry) -> StoreResult<()> {
        let url = format!("{}/api/agents/{}/entries", self.base_url, agent_name);
        let event_val = serde_json::to_value(&entry.event)
            .unwrap_or(serde_json::Value::String("unknown".to_string()));
        let body = serde_json::json!({
            "tick":      entry.tick,
            "action":    format!("{:?}", entry.action),
            "event":     event_val,
            "note":      entry.note,
            "hash":      hex::encode(entry.hash),
            "prev_hash": hex::encode(entry.prev_hash),
        });
        ureq::post(&url)
            .set("Authorization", &format!("Bearer {}", self.token))
            .send_json(body)?;
        Ok(())
    }

    fn load_entries(&self, agent_name: &str) -> StoreResult<Vec<MemoryEntry>> {
        let url = format!("{}/api/agents/{}/entries", self.base_url, agent_name);
        let resp = ureq::get(&url)
            .set("Authorization", &format!("Bearer {}", self.token))
            .call()
            .map_err(|e| -> StoreError { e.into() })?;
        let body: serde_json::Value = resp.into_json()?;
        let empty: Vec<serde_json::Value> = vec![];
        let arr = body["entries"].as_array().unwrap_or(&empty);
        let mut result = Vec::new();
        for e in arr {
            let tick: u32  = e["tick"].as_u64().unwrap_or(0) as u32;
            let action_str = e["action"].as_str().unwrap_or("Observe");
            let note       = e["note"].as_str().unwrap_or("").to_string();
            let hash_hex   = e["hash"].as_str().unwrap_or("");
            let prev_hex   = e["prev_hash"].as_str().unwrap_or("");
            let event      = serde_json::from_value::<SecurityEvent>(e["event"].clone())
                .unwrap_or_else(|_| SecurityEvent::Custom(
                    e["event"].as_str().unwrap_or("unknown").to_string()
                ));
            let action    = parse_action(action_str);
            let hash      = hex_to_hash(hash_hex);
            let prev_hash = hex_to_hash(prev_hex);
            result.push(MemoryEntry { tick, action, event, note, hash, prev_hash });
        }
        Ok(result)
    }
}

// ============================================================
// Helpers (shared by both backends)
// ============================================================

fn hex_to_hash(s: &str) -> Hash {
    let mut out = [0u8; 32];
    if let Ok(bytes) = hex::decode(s) {
        let len = bytes.len().min(32);
        out[..len].copy_from_slice(&bytes[..len]);
    }
    out
}

fn parse_action(s: &str) -> Action {
    match s {
        "Observe"  => Action::Observe,
        "Explore"  => Action::Explore,
        "Rest"     => Action::Rest,
        "Reflect"  => Action::Reflect,
        _          => Action::Observe,
    }
}

// ============================================================
// Unit tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::Agent;

    fn in_memory_store() -> Store {
        Store::open(":memory:").expect("in-memory store")
    }

    #[test]
    fn schema_init_is_idempotent() {
        let store = in_memory_store();
        store.init_schema().expect("second init_schema call");
    }

    #[test]
    fn save_and_load_agent_meta() {
        let store = in_memory_store();
        store.save_agent_meta("Alpha", "test goal", 5).unwrap();
        let meta = store.load_agent_meta("Alpha").unwrap();
        assert_eq!(meta, Some(("test goal".to_string(), 5)));
    }

    #[test]
    fn load_meta_returns_none_for_unknown_agent() {
        let store = in_memory_store();
        let meta = store.load_agent_meta("Ghost").unwrap();
        assert!(meta.is_none());
    }

    #[test]
    fn save_and_load_entries_round_trip() {
        let store = in_memory_store();
        store.save_agent_meta("Bot", "round-trip goal", 0).unwrap();

        let mut agent = Agent::new("Bot", "round-trip goal");
        for _ in 0..5 {
            let entry = agent.think();
            store.save_entry("Bot", &entry).unwrap();
        }
        store.save_agent_meta("Bot", "round-trip goal", agent.tick).unwrap();

        let loaded = store.load_entries("Bot").unwrap();
        assert_eq!(loaded.len(), 5);
        assert_eq!(loaded[0].tick, 1);
        assert_eq!(loaded[4].tick, 5);
    }

    #[test]
    fn loaded_entries_restore_valid_agent() {
        let store = in_memory_store();
        store.save_agent_meta("Restorer", "persistence test", 0).unwrap();

        let mut agent = Agent::new("Restorer", "persistence test");
        for _ in 0..6 {
            let entry = agent.think();
            store.save_entry("Restorer", &entry).unwrap();
        }

        let entries = store.load_entries("Restorer").unwrap();
        let restored = Agent::restore("Restorer", "persistence test", entries)
            .expect("restore should succeed");
        assert_eq!(restored.memory_len(), 6);
        assert_eq!(restored.tick, 6);
        assert!(restored.verify_integrity());
    }

    #[test]
    fn hash_survives_hex_round_trip() {
        let original: Hash = [
            0x1a, 0x2b, 0x3c, 0x4d, 0x5e, 0x6f, 0x70, 0x81,
            0x92, 0xa3, 0xb4, 0xc5, 0xd6, 0xe7, 0xf8, 0x09,
            0x10, 0x21, 0x32, 0x43, 0x54, 0x65, 0x76, 0x87,
            0x98, 0xa9, 0xba, 0xcb, 0xdc, 0xed, 0xfe, 0x0f,
        ];
        let hex_str = hex::encode(original);
        let recovered = hex_to_hash(&hex_str);
        assert_eq!(original, recovered);
    }
}
