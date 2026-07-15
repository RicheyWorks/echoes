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
use crate::agent::{
    checkpoint_hash, Action, Hash, MemoryEntry, MerkleTree, SecurityEvent,
};

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

            CREATE TABLE IF NOT EXISTS checkpoints (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name          TEXT    NOT NULL REFERENCES agents(name),
                pruned_through_tick INTEGER NOT NULL,
                entries_sealed      INTEGER NOT NULL,
                head_hex            TEXT    NOT NULL,
                merkle_hex          TEXT    NOT NULL,
                prev_cp_hex         TEXT    NOT NULL,
                cp_hex              TEXT    NOT NULL,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_cp_agent
                ON checkpoints(agent_name, id);

            CREATE TABLE IF NOT EXISTS file_manifest (
                agent_name  TEXT    NOT NULL,
                root        TEXT    NOT NULL,
                file_path   TEXT    NOT NULL,
                size        INTEGER NOT NULL,
                mtime       INTEGER NOT NULL,
                sha_hex     TEXT    NOT NULL,
                PRIMARY KEY (agent_name, root, file_path)
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name  TEXT    NOT NULL,
                started_at  INTEGER NOT NULL,
                ended_at    INTEGER,
                start_tick  INTEGER NOT NULL,
                end_tick    INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_agent
                ON episodes(agent_name, id);
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
// Checkpoints — chain-aware pruning (ADR-002 Phase 7b)
// ============================================================

/// A sealed, pruned prefix of an agent's memory chain.
///
/// The raw entries are gone; what survives is their count, the tick range,
/// the head hash (which becomes the trusted genesis for the live chain),
/// and a Merkle root attesting the deleted entries. Checkpoint rows are
/// themselves hash-chained via `prev_hash`/`hash`.
#[derive(Debug, Clone, PartialEq)]
pub struct Checkpoint {
    pub pruned_through_tick: u32,
    /// Cumulative count of sealed entries across all checkpoints.
    pub entries_sealed: u32,
    pub head: Hash,
    pub merkle_root: Hash,
    pub prev_hash: Hash,
    pub hash: Hash,
}

/// Result of a [`SqliteStore::prune`] call.
#[derive(Debug)]
pub enum PruneOutcome {
    /// Fewer than or exactly `keep_last` live entries — nothing was pruned.
    Nothing { entries: usize },
    /// A prefix was sealed and deleted.
    Pruned {
        sealed: usize,
        remaining: usize,
        checkpoint: Checkpoint,
    },
}

impl SqliteStore {
    /// Load and validate the checkpoint chain for `agent`.
    ///
    /// Recomputes every checkpoint hash from its stored fields and checks the
    /// linkage from the zero genesis. Returns the latest checkpoint (or
    /// `None` when the agent has never been pruned). Any mismatch is an
    /// error: the checkpoint chain itself has been tampered with.
    pub fn verify_checkpoints(&self, agent: &str) -> StoreResult<Option<Checkpoint>> {
        let mut stmt = self.conn.prepare(
            "SELECT pruned_through_tick, entries_sealed, head_hex, merkle_hex,
                    prev_cp_hex, cp_hex
             FROM checkpoints WHERE agent_name = ?1 ORDER BY id ASC",
        )?;
        let rows: Vec<Checkpoint> = stmt
            .query_map(params![agent], |row| {
                let tick: u32        = row.get(0)?;
                let sealed: u32      = row.get(1)?;
                let head_hex: String = row.get(2)?;
                let merk_hex: String = row.get(3)?;
                let prev_hex: String = row.get(4)?;
                let cp_hex: String   = row.get(5)?;
                Ok(Checkpoint {
                    pruned_through_tick: tick,
                    entries_sealed: sealed,
                    head: hex_to_hash(&head_hex),
                    merkle_root: hex_to_hash(&merk_hex),
                    prev_hash: hex_to_hash(&prev_hex),
                    hash: hex_to_hash(&cp_hex),
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut expected_prev = [0u8; 32];
        for (i, cp) in rows.iter().enumerate() {
            if cp.prev_hash != expected_prev {
                return Err(format!(
                    "checkpoint {} for agent '{}' does not link to its predecessor — \
                     checkpoint chain is CORRUPT (possible tamper)",
                    i, agent
                )
                .into());
            }
            let recomputed = checkpoint_hash(
                &cp.prev_hash,
                agent,
                cp.pruned_through_tick,
                cp.entries_sealed,
                &cp.head,
                &cp.merkle_root,
            );
            if recomputed != cp.hash {
                return Err(format!(
                    "checkpoint {} for agent '{}' fails hash verification — \
                     checkpoint chain is CORRUPT (possible tamper)",
                    i, agent
                )
                .into());
            }
            expected_prev = cp.hash;
        }
        Ok(rows.into_iter().last())
    }

    /// The trusted genesis for the live chain: `(base_hash, base_tick)`.
    ///
    /// Zeros/0 when no checkpoint exists; otherwise the latest checkpoint's
    /// head hash and sealed-through tick. Validates the checkpoint chain.
    pub fn chain_base(&self, agent: &str) -> StoreResult<(Hash, u32)> {
        Ok(match self.verify_checkpoints(agent)? {
            Some(cp) => (cp.head, cp.pruned_through_tick),
            None => ([0u8; 32], 0),
        })
    }

    /// Seal and delete all but the last `keep_last` live entries.
    ///
    /// Refuses to prune unless the full live chain (and any existing
    /// checkpoint chain) verifies first — pruning a corrupt chain would
    /// destroy the evidence of tampering. `keep_last` must be >= 1 so the
    /// live chain always retains its linkage row. The deleted prefix's
    /// integrity attestation survives as a checkpoint row; the raw entries
    /// are unrecoverable by design.
    pub fn prune(&self, agent: &str, keep_last: usize) -> StoreResult<PruneOutcome> {
        assert!(keep_last >= 1, "keep_last must be >= 1");

        let prev_cp = self.verify_checkpoints(agent)?;
        let (base_hash, base_tick) = match &prev_cp {
            Some(cp) => (cp.head, cp.pruned_through_tick),
            None => ([0u8; 32], 0),
        };

        let (goal, _) = self
            .load_agent_meta_sq(agent)?
            .ok_or_else(|| format!("agent '{}' not found", agent))?;
        let entries = self.load_entries_sq(agent)?;

        // Full integrity check before deleting anything.
        crate::agent::Agent::restore_from(agent, &goal, entries.clone(), base_hash, base_tick)
            .map_err(|e| format!("refusing to prune: {}", e))?;

        if entries.len() <= keep_last {
            return Ok(PruneOutcome::Nothing { entries: entries.len() });
        }

        let cut = entries.len() - keep_last;
        let segment = &entries[..cut];
        let head = segment.last().expect("cut >= 1").hash;
        let through = segment.last().expect("cut >= 1").tick;
        let merkle_root = MerkleTree::from_memory(segment).root();
        let sealed_total =
            prev_cp.as_ref().map(|c| c.entries_sealed).unwrap_or(0) + cut as u32;
        let prev_hash = prev_cp.as_ref().map(|c| c.hash).unwrap_or([0u8; 32]);
        let hash = checkpoint_hash(
            &prev_hash, agent, through, sealed_total, &head, &merkle_root,
        );

        let tx = self.conn.unchecked_transaction()?;
        tx.execute(
            "INSERT INTO checkpoints
                 (agent_name, pruned_through_tick, entries_sealed,
                  head_hex, merkle_hex, prev_cp_hex, cp_hex)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                agent,
                through,
                sealed_total,
                hex::encode(head),
                hex::encode(merkle_root),
                hex::encode(prev_hash),
                hex::encode(hash),
            ],
        )?;
        tx.execute(
            "DELETE FROM memory_entries WHERE agent_name = ?1 AND tick <= ?2",
            params![agent, through],
        )?;
        tx.commit()?;

        Ok(PruneOutcome::Pruned {
            sealed: cut,
            remaining: keep_last,
            checkpoint: Checkpoint {
                pruned_through_tick: through,
                entries_sealed: sealed_total,
                head,
                merkle_root,
                prev_hash,
                hash,
            },
        })
    }
}

// ============================================================
// Offline state-diff manifests (ADR-002 Phase 9a)
// ============================================================

impl SqliteStore {
    /// Replace the stored manifest for `(agent, root)` with `files`.
    /// Called at the end of a run, after the final tree scan.
    pub fn save_manifest(
        &self,
        agent: &str,
        root: &str,
        files: &[crate::manifest::FileState],
    ) -> StoreResult<()> {
        let tx = self.conn.unchecked_transaction()?;
        tx.execute(
            "DELETE FROM file_manifest WHERE agent_name = ?1 AND root = ?2",
            params![agent, root],
        )?;
        for f in files {
            tx.execute(
                "INSERT INTO file_manifest
                     (agent_name, root, file_path, size, mtime, sha_hex)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![agent, root, f.path, f.size as i64, f.mtime, f.sha_hex],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Load the stored manifest for `(agent, root)`. Empty when this root
    /// has never been scanned (first run).
    pub fn load_manifest(
        &self,
        agent: &str,
        root: &str,
    ) -> StoreResult<Vec<crate::manifest::FileState>> {
        let mut stmt = self.conn.prepare(
            "SELECT file_path, size, mtime, sha_hex FROM file_manifest
             WHERE agent_name = ?1 AND root = ?2 ORDER BY file_path ASC",
        )?;
        let rows = stmt
            .query_map(params![agent, root], |row| {
                Ok(crate::manifest::FileState {
                    path: row.get(0)?,
                    size: row.get::<_, i64>(1)? as u64,
                    mtime: row.get(2)?,
                    sha_hex: row.get(3)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();
        Ok(rows)
    }
}

// ============================================================
// Episodes — run boundaries for the continuity report (Phase 9c)
// ============================================================

/// One micro-run of an agent: wall-clock boundaries plus the tick range it
/// covered. `ended_at`/`end_tick` are `None` for a run that crashed (or is
/// still executing). Episodes are operational telemetry — they live beside
/// the chain, not in it.
#[derive(Debug, Clone, PartialEq)]
pub struct Episode {
    pub id: i64,
    /// Unix seconds.
    pub started_at: i64,
    pub ended_at: Option<i64>,
    /// Agent tick before the first `think()` of this run.
    pub start_tick: u32,
    pub end_tick: Option<u32>,
}

/// Aggregate view over the episode list — the numbers behind
/// `echoes report --continuity`.
#[derive(Debug, Clone, PartialEq)]
pub struct ContinuitySummary {
    pub episodes: usize,
    pub interrupted: usize,
    /// Sum of clean-episode durations.
    pub live_seconds: i64,
    /// First start → last recorded activity.
    pub span_seconds: i64,
    /// Idle seconds between consecutive episodes.
    pub gaps: Vec<i64>,
}

/// Pure summary computation (unit-testable without a DB).
pub fn summarize_episodes(episodes: &[Episode]) -> ContinuitySummary {
    let mut live = 0i64;
    let mut interrupted = 0usize;
    let mut gaps = Vec::new();
    let mut last_end: Option<i64> = None;
    let mut span_end: Option<i64> = None;

    for ep in episodes {
        match ep.ended_at {
            Some(end) => {
                live += (end - ep.started_at).max(0);
                span_end = Some(span_end.map_or(end, |s: i64| s.max(end)));
            }
            None => interrupted += 1,
        }
        if let Some(prev_end) = last_end {
            gaps.push((ep.started_at - prev_end).max(0));
        }
        last_end = ep.ended_at.or(last_end);
        span_end = Some(span_end.map_or(ep.started_at, |s| s.max(ep.started_at)));
    }

    let span = match (episodes.first(), span_end) {
        (Some(first), Some(end)) => (end - first.started_at).max(0),
        _ => 0,
    };

    ContinuitySummary {
        episodes: episodes.len(),
        interrupted,
        live_seconds: live,
        span_seconds: span,
        gaps,
    }
}

impl SqliteStore {
    /// Record the start of a run. Returns the episode id.
    pub fn begin_episode(&self, agent: &str, start_tick: u32) -> StoreResult<i64> {
        self.conn.execute(
            "INSERT INTO episodes (agent_name, started_at, start_tick)
             VALUES (?1, strftime('%s','now'), ?2)",
            params![agent, start_tick],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    /// Close an episode at the current time.
    pub fn end_episode(&self, id: i64, end_tick: u32) -> StoreResult<()> {
        self.conn.execute(
            "UPDATE episodes SET ended_at = strftime('%s','now'), end_tick = ?2
             WHERE id = ?1",
            params![id, end_tick],
        )?;
        Ok(())
    }

    /// All episodes for `agent`, oldest first.
    pub fn load_episodes(&self, agent: &str) -> StoreResult<Vec<Episode>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, started_at, ended_at, start_tick, end_tick
             FROM episodes WHERE agent_name = ?1 ORDER BY id ASC",
        )?;
        let rows = stmt
            .query_map(params![agent], |row| {
                Ok(Episode {
                    id: row.get(0)?,
                    started_at: row.get(1)?,
                    ended_at: row.get(2)?,
                    start_tick: row.get(3)?,
                    end_tick: row.get(4)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();
        Ok(rows)
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

    // -------------------------------------------------------
    // Chain-aware pruning (ADR-002 Phase 7b)
    // -------------------------------------------------------

    fn seeded_store(name: &str, ticks: usize) -> (Store, Agent) {
        let store = in_memory_store();
        store.save_agent_meta(name, "prune test", 0).unwrap();
        let mut agent = Agent::new(name, "prune test");
        for _ in 0..ticks {
            let entry = agent.think();
            store.save_entry(name, &entry).unwrap();
        }
        store.save_agent_meta(name, "prune test", agent.tick).unwrap();
        (store, agent)
    }

    #[test]
    fn prune_seals_prefix_and_deletes_rows() {
        let (store, agent) = seeded_store("P", 10);
        let outcome = store.prune("P", 4).unwrap();
        match outcome {
            PruneOutcome::Pruned { sealed, remaining, checkpoint } => {
                assert_eq!(sealed, 6);
                assert_eq!(remaining, 4);
                assert_eq!(checkpoint.pruned_through_tick, 6);
                assert_eq!(checkpoint.entries_sealed, 6);
                assert_eq!(checkpoint.head, agent.memory[5].hash);
                assert_eq!(checkpoint.prev_hash, [0u8; 32]);
            }
            other => panic!("expected Pruned, got {:?}", other),
        }
        assert_eq!(store.load_entries("P").unwrap().len(), 4);
    }

    #[test]
    fn live_chain_verifies_against_checkpoint_base() {
        let (store, _) = seeded_store("V", 10);
        store.prune("V", 4).unwrap();

        let (base_hash, base_tick) = store.chain_base("V").unwrap();
        assert_eq!(base_tick, 6);
        let entries = store.load_entries("V").unwrap();
        let restored = Agent::restore_from("V", "prune test", entries, base_hash, base_tick)
            .expect("live chain must verify against checkpoint head");
        assert_eq!(restored.memory_len(), 4);
        assert_eq!(restored.tick, 10);
    }

    #[test]
    fn append_after_prune_continues_chain() {
        let (store, _) = seeded_store("A", 8);
        store.prune("A", 3).unwrap();

        let (base_hash, base_tick) = store.chain_base("A").unwrap();
        let entries = store.load_entries("A").unwrap();
        let mut agent =
            Agent::restore_from("A", "prune test", entries, base_hash, base_tick).unwrap();
        for _ in 0..5 {
            let entry = agent.think();
            store.save_entry("A", &entry).unwrap();
        }

        let entries = store.load_entries("A").unwrap();
        assert_eq!(entries.len(), 8); // 3 kept + 5 new
        assert!(Agent::restore_from("A", "prune test", entries, base_hash, base_tick).is_ok());
    }

    #[test]
    fn second_prune_chains_checkpoints() {
        let (store, _) = seeded_store("C", 10);
        let first = match store.prune("C", 6).unwrap() {
            PruneOutcome::Pruned { checkpoint, .. } => checkpoint,
            other => panic!("expected Pruned, got {:?}", other),
        };
        let second = match store.prune("C", 2).unwrap() {
            PruneOutcome::Pruned { checkpoint, .. } => checkpoint,
            other => panic!("expected Pruned, got {:?}", other),
        };
        assert_eq!(second.prev_hash, first.hash);
        assert_eq!(second.entries_sealed, 8); // cumulative: 4 + 4
        assert_eq!(second.pruned_through_tick, 8);

        // verify_checkpoints returns the latest and validates linkage.
        let latest = store.verify_checkpoints("C").unwrap().unwrap();
        assert_eq!(latest, second);
    }

    #[test]
    fn prune_is_noop_when_keep_covers_all() {
        let (store, _) = seeded_store("N", 5);
        match store.prune("N", 5).unwrap() {
            PruneOutcome::Nothing { entries } => assert_eq!(entries, 5),
            other => panic!("expected Nothing, got {:?}", other),
        }
        assert!(store.verify_checkpoints("N").unwrap().is_none());
    }

    #[test]
    fn prune_refuses_corrupt_live_chain() {
        let (store, _) = seeded_store("R", 6);
        store
            .conn
            .execute(
                "UPDATE memory_entries SET note = 'tampered' WHERE tick = 2 AND agent_name = 'R'",
                [],
            )
            .unwrap();
        let err = store.prune("R", 2).unwrap_err().to_string();
        assert!(err.contains("refusing to prune"), "got: {}", err);
        // Nothing was deleted.
        assert_eq!(store.load_entries("R").unwrap().len(), 6);
    }

    #[test]
    fn tampered_checkpoint_is_detected() {
        let (store, _) = seeded_store("T", 10);
        store.prune("T", 4).unwrap();

        // Forge the sealed Merkle root: the recomputed checkpoint hash no
        // longer matches the stored one.
        store
            .conn
            .execute(
                "UPDATE checkpoints SET merkle_hex = ?1 WHERE agent_name = 'T'",
                params![hex::encode([9u8; 32])],
            )
            .unwrap();
        let err = store.verify_checkpoints("T").unwrap_err().to_string();
        assert!(err.contains("CORRUPT"), "got: {}", err);
    }

    #[test]
    fn tampered_checkpoint_head_breaks_live_chain_verification() {
        let (store, _) = seeded_store("H", 10);
        store.prune("H", 4).unwrap();

        // An attacker who forges head_hex must also forge cp_hex (checkpoint
        // hash covers the head)…
        let fake_head = hex::encode([8u8; 32]);
        store
            .conn
            .execute(
                "UPDATE checkpoints SET head_hex = ?1 WHERE agent_name = 'H'",
                params![fake_head],
            )
            .unwrap();
        assert!(store.verify_checkpoints("H").is_err());

        // …and even a fully recomputed forged checkpoint still fails, because
        // the live chain's first prev_hash doesn't match the forged head.
        let forged_cp = checkpoint_hash(&[0u8; 32], "H", 6, 6, &[8u8; 32], &[9u8; 32]);
        store
            .conn
            .execute(
                "UPDATE checkpoints SET merkle_hex = ?1, cp_hex = ?2 WHERE agent_name = 'H'",
                params![hex::encode([9u8; 32]), hex::encode(forged_cp)],
            )
            .unwrap();
        let (base_hash, base_tick) = store.chain_base("H").unwrap();
        let entries = store.load_entries("H").unwrap();
        assert!(
            Agent::restore_from("H", "prune test", entries, base_hash, base_tick).is_err(),
            "live chain must not verify against a forged checkpoint head"
        );
    }

    #[test]
    fn episodes_begin_end_round_trip() {
        let store = in_memory_store();
        let e1 = store.begin_episode("E", 0).unwrap();
        store.end_episode(e1, 8).unwrap();
        let e2 = store.begin_episode("E", 8).unwrap();
        // e2 left open — simulates a crash or an in-flight run.

        let eps = store.load_episodes("E").unwrap();
        assert_eq!(eps.len(), 2);
        assert_eq!(eps[0].id, e1);
        assert_eq!(eps[0].start_tick, 0);
        assert_eq!(eps[0].end_tick, Some(8));
        assert!(eps[0].ended_at.is_some());
        assert_eq!(eps[1].id, e2);
        assert_eq!(eps[1].ended_at, None);
        assert!(store.load_episodes("other").unwrap().is_empty());
    }

    #[test]
    fn summarize_episodes_computes_gaps_live_and_span() {
        let eps = vec![
            Episode { id: 1, started_at: 1000, ended_at: Some(1010), start_tick: 0, end_tick: Some(20) },
            Episode { id: 2, started_at: 1300, ended_at: Some(1315), start_tick: 20, end_tick: Some(40) },
            Episode { id: 3, started_at: 1600, ended_at: None, start_tick: 40, end_tick: None },
        ];
        let s = summarize_episodes(&eps);
        assert_eq!(s.episodes, 3);
        assert_eq!(s.interrupted, 1);
        assert_eq!(s.live_seconds, 25);        // 10 + 15
        assert_eq!(s.gaps, vec![290, 285]);    // 1300-1010, 1600-1315
        assert_eq!(s.span_seconds, 600);       // 1600 - 1000
    }

    #[test]
    fn summarize_episodes_empty_is_zeroes() {
        let s = summarize_episodes(&[]);
        assert_eq!(s, ContinuitySummary {
            episodes: 0, interrupted: 0, live_seconds: 0,
            span_seconds: 0, gaps: vec![],
        });
    }

    #[test]
    fn manifest_round_trip_and_replace() {
        use crate::manifest::FileState;
        let store = in_memory_store();
        let files = vec![
            FileState { path: "/w/a".into(), size: 3, mtime: 100, sha_hex: "aa".into() },
            FileState { path: "/w/b".into(), size: 5, mtime: 200, sha_hex: "bb".into() },
        ];
        store.save_manifest("M", "/w", &files).unwrap();
        assert_eq!(store.load_manifest("M", "/w").unwrap(), files);

        // Saving again replaces, not appends.
        let files2 = vec![
            FileState { path: "/w/c".into(), size: 7, mtime: 300, sha_hex: "cc".into() },
        ];
        store.save_manifest("M", "/w", &files2).unwrap();
        assert_eq!(store.load_manifest("M", "/w").unwrap(), files2);

        // Other agents/roots are untouched namespaces.
        assert!(store.load_manifest("M", "/other").unwrap().is_empty());
        assert!(store.load_manifest("N", "/w").unwrap().is_empty());
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
