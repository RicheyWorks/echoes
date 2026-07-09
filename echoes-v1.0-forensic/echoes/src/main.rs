//! echoes — forensic agent CLI
//!
//! Subcommands:
//!   run     Run (or resume) an agent for N ticks, persisting every step.
//!   verify  Reload the agent from the DB and verify hash-chain integrity.
//!   report  Print the full memory chain + Merkle root; optionally as JSON.

mod agent;
mod sensor;
mod store;

use clap::{Parser, Subcommand};
use serde_json::json;
use agent::{Agent, MerkleTree};
use store::{AgentStore, RemoteStore, Store};

// ============================================================
// CLI definition
// ============================================================

#[derive(Parser)]
#[command(
    name    = "echoes",
    version = "1.0.0",
    about   = "Forensic agent with cryptographic memory and SQLite persistence",
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Run an agent for N ticks (resumes from DB or remote store if already exists).
    Run {
        /// Path to the SQLite database file (unused when --remote-store is set).
        #[arg(long, default_value = "echoes.db")]
        db: String,

        /// Number of ticks to execute.
        #[arg(long, short = 'n', default_value_t = 8)]
        ticks: u32,

        /// Agent name (used as the primary key in the store).
        #[arg(long, default_value = "Echo")]
        name: String,

        /// Agent goal (only used when creating a fresh agent; ignored on resume).
        #[arg(long, default_value = "map environment with cryptographic memory")]
        goal: String,

        /// Watch this filesystem path for real FileAccess events.
        /// Requires the `watch` Cargo feature (`--features watch`).
        #[arg(long, value_name = "PATH")]
        watch: Option<String>,

        /// Scan for new processes each tick and emit ProcessExecution events.
        #[arg(long, default_value_t = false)]
        procs: bool,

        /// Persist to a remote automaton server instead of a local SQLite file.
        /// Example: http://192.168.1.10:8080
        #[arg(long, value_name = "URL")]
        remote_store: Option<String>,

        /// Bearer token for the remote automaton server (AUTOMATON_TOKEN).
        #[arg(long, value_name = "TOKEN", env = "AUTOMATON_TOKEN")]
        token: Option<String>,
    },

    /// Verify hash-chain integrity for a persisted agent.
    Verify {
        /// Path to the SQLite database file.
        #[arg(long, default_value = "echoes.db")]
        db: String,

        /// Agent name to verify.
        #[arg(long, default_value = "Echo")]
        name: String,
    },

    /// Print the full memory chain and Merkle root.
    Report {
        /// Path to the SQLite database file.
        #[arg(long, default_value = "echoes.db")]
        db: String,

        /// Agent name to report on.
        #[arg(long, default_value = "Echo")]
        name: String,

        /// Output as JSON instead of human-readable text.
        #[arg(long)]
        json: bool,
    },
}

// ============================================================
// Entry point
// ============================================================

fn main() {
    let cli = Cli::parse();

    match cli.command {
        Commands::Run { db, ticks, name, goal, watch, procs, remote_store, token } =>
            cmd_run(
                &db, ticks, &name, &goal,
                watch.as_deref(), procs,
                remote_store.as_deref(), token.as_deref(),
            ),
        Commands::Verify { db, name }       => cmd_verify(&db, &name),
        Commands::Report { db, name, json } => cmd_report(&db, &name, json),
    }
}

// ============================================================
// Command implementations
// ============================================================

/// Run (or resume) an agent for `ticks` steps, persisting each entry.
///
/// When `remote_url` is `Some`, a `RemoteStore` is used instead of the local
/// SQLite file. The remote store talks to an automaton `/api/agents/*` API.
#[allow(clippy::too_many_arguments)]
fn cmd_run(db_path: &str, ticks: u32, name: &str, goal: &str,
           watch_path: Option<&str>, scan_procs: bool,
           remote_url: Option<&str>, remote_token: Option<&str>) {

    // Build the store backend -----------------------------------------------
    let store: Box<dyn AgentStore> = match remote_url {
        Some(url) => {
            let tok = remote_token.unwrap_or("");
            println!("Using remote store: {}", url);
            Box::new(RemoteStore::new(url, tok))
        }
        None => {
            Box::new(Store::open(db_path).unwrap_or_else(|e| {
                eprintln!("error: could not open DB at '{}': {}", db_path, e);
                std::process::exit(1);
            }))
        }
    };

    // Try to restore a pre-existing agent, otherwise create fresh. ----------
    let mut agent = match store.load_entries(name) {
        Ok(entries) if !entries.is_empty() => {
            let stored_goal = store
                .load_agent_meta(name)
                .ok()
                .flatten()
                .map(|(g, _)| g)
                .unwrap_or_else(|| goal.to_string());

            println!("Resuming agent '{}' from {} persisted entries.", name, entries.len());
            Agent::restore(name, &stored_goal, entries).unwrap_or_else(|e| {
                eprintln!("error: {}", e);
                std::process::exit(1);
            })
        }
        _ => {
            println!("Creating new agent '{}'.", name);
            store.save_agent_meta(name, goal, 0).unwrap_or_else(|e| {
                eprintln!("error: could not initialise agent row: {}", e);
                std::process::exit(1);
            });
            Agent::new(name, goal)
        }
    };

    // Build the composite sensor from whichever sources were requested. -----
    use sensor::{CompositeSource, EventSource, FileWatcher, ProcessScanner};
    let mut sources: Vec<Box<dyn EventSource>> = Vec::new();

    if let Some(p) = watch_path {
        match FileWatcher::new(p) {
            Some(fw) => {
                println!("File watcher active on '{}'.", p);
                sources.push(Box::new(fw));
            }
            None => {
                eprintln!(
                    "warning: could not start file watcher on '{}'                      (build without --features watch?)",
                    p
                );
            }
        }
    }

    if scan_procs {
        println!("Process scanner active.");
        sources.push(Box::new(ProcessScanner::new()));
    }

    let mut composite = CompositeSource::new(sources);

    // Main tick loop ---------------------------------------------------------
    println!("Running {} tick(s)...\n", ticks);
    for _ in 0..ticks {
        let entry = agent.think_with(Some(&mut composite));
        store.save_entry(name, &entry).unwrap_or_else(|e| {
            eprintln!("error: could not persist entry (tick {}): {}", entry.tick, e);
            std::process::exit(1);
        });
    }

    store.save_agent_meta(name, agent.goal(), agent.tick).unwrap_or_else(|e| {
        eprintln!("warning: could not update agent meta: {}", e);
    });

    let tree = MerkleTree::from_memory(&agent.memory);
    println!("\nDone — {} total memories | Merkle root: {}", agent.memory_len(), tree.short_root());
}

/// Verify hash-chain integrity for a persisted agent (SQLite only).
fn cmd_verify(db_path: &str, name: &str) {
    let store = open_or_exit(db_path);
    let (goal, _) = load_meta_or_exit(&store, name, db_path);
    let entries   = load_entries_or_exit(&store, name, db_path);

    match Agent::restore(name, &goal, entries) {
        Ok(agent) => {
            println!("Agent '{}' — {} entries loaded.", name, agent.memory_len());
            if agent.verify_integrity() {
                println!("Hash-chain integrity: PASSED ✓");
            } else {
                eprintln!("Hash-chain integrity: FAILED ✗  (unexpected — restore bug?)");
                std::process::exit(2);
            }
            let tree = MerkleTree::from_memory(&agent.memory);
            println!("Merkle root:          {}", tree.short_root());
        }
        Err(e) => {
            eprintln!("Hash-chain integrity: FAILED ✗\n  {}", e);
            std::process::exit(2);
        }
    }
}

/// Print the full memory chain, optionally as JSON (SQLite only).
fn cmd_report(db_path: &str, name: &str, as_json: bool) {
    let store   = open_or_exit(db_path);
    let (goal, _) = load_meta_or_exit(&store, name, db_path);
    let entries = load_entries_or_exit(&store, name, db_path);
    let count   = entries.len();

    let agent = Agent::restore(name, &goal, entries).unwrap_or_else(|e| {
        eprintln!("error: integrity failure during report: {}", e);
        std::process::exit(2);
    });

    let tree = MerkleTree::from_memory(&agent.memory);

    if as_json {
        let mem_json: Vec<_> = agent.memory.iter().map(|e| {
            json!({
                "tick":      e.tick,
                "action":    format!("{:?}", e.action),
                "event":     format!("{}", e.event),
                "note":      e.note,
                "hash":      hex::encode(e.hash),
                "prev_hash": hex::encode(e.prev_hash),
            })
        }).collect();

        let out = json!({
            "agent":        name,
            "goal":         goal,
            "entries":      count,
            "merkle_root":  hex::encode(tree.root()),
            "integrity":    "ok",
            "memory":       mem_json,
        });
        println!("{}", serde_json::to_string_pretty(&out).unwrap());
    } else {
        agent.print_memory_chain();
        agent.print_audit_log();
        println!("\nMerkle root: {}", tree.short_root());
    }
}

// ============================================================
// SQLite-specific helpers (verify + report only)
// ============================================================

fn open_or_exit(db_path: &str) -> Store {
    Store::open(db_path).unwrap_or_else(|e| {
        eprintln!("error: could not open DB at '{}': {}", db_path, e);
        std::process::exit(1);
    })
}

fn load_meta_or_exit(store: &Store, name: &str, db_path: &str) -> (String, u32) {
    match store.load_agent_meta(name) {
        Ok(Some(meta)) => meta,
        Ok(None) => {
            eprintln!(
                "error: agent '{}' not found in '{}'. Run `echoes run --name {}` first.",
                name, db_path, name
            );
            std::process::exit(1);
        }
        Err(e) => {
            eprintln!("error: DB read failed: {}", e);
            std::process::exit(1);
        }
    }
}

fn load_entries_or_exit(store: &Store, name: &str, db_path: &str) -> Vec<agent::MemoryEntry> {
    store.load_entries(name).unwrap_or_else(|e| {
        eprintln!("error: could not load entries for '{}' from '{}': {}", name, db_path, e);
        std::process::exit(1);
    })
}
