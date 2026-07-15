//! echoes — forensic agent CLI
//!
//! Subcommands:
//!   run     Run (or resume) an agent for N ticks, persisting every step.
//!   verify  Reload the agent from the DB and verify hash-chain integrity.
//!   report  Print the full memory chain + Merkle root; optionally as JSON.
//!   prune   Seal and delete all but the last N entries (chain-aware).


use clap::{Parser, Subcommand};
use serde_json::json;
use echoes::agent::{Agent, MerkleTree};
use echoes::store::{AgentStore, PruneOutcome, RemoteStore, Store};

// ============================================================
// CLI definition
// ============================================================

#[derive(Parser)]
#[command(
    name    = "echoes",
    version,   // always matches Cargo.toml (CARGO_PKG_VERSION)
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
        /// TOML config file; CLI flags override it (ADR-002 Phase 8d).
        #[arg(long, value_name = "PATH")]
        config: Option<String>,

        /// Path to the SQLite database file [default: echoes.db].
        #[arg(long)]
        db: Option<String>,

        /// Number of ticks to execute [default: 8].
        #[arg(long, short = 'n')]
        ticks: Option<u32>,

        /// Agent name (used as the primary key in the store) [default: Echo].
        #[arg(long)]
        name: Option<String>,

        /// Agent goal (only used when creating a fresh agent; ignored on resume).
        #[arg(long)]
        goal: Option<String>,

        /// Watch this filesystem path for real FileAccess events.
        /// Requires the `watch` Cargo feature (`--features watch`).
        #[arg(long, value_name = "PATH")]
        watch: Option<String>,

        /// Scan for new processes each tick and emit ProcessExecution events.
        #[arg(long, default_value_t = false)]
        procs: bool,

        /// Diff the OS connection table each tick and emit NetworkConnection
        /// events for new established connections (unprivileged, metadata only).
        #[arg(long, default_value_t = false)]
        net: bool,

        /// Tail the system auth log and emit Authentication events for sshd
        /// accepts/failures and PAM failures. Linux-first; needs `adm` group
        /// membership (not root). Optionally pass an explicit log path.
        #[arg(long, value_name = "PATH", num_args = 0..=1,
              default_missing_value = "")]
        auth: Option<String>,

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

        /// Include the continuity view: episode boundaries, gaps between
        /// micro-runs, offline-diff event counts, live-vs-span coverage.
        #[arg(long)]
        continuity: bool,
    },

    /// Seal and delete all but the last N entries (chain-aware pruning).
    ///
    /// The deleted prefix is replaced by a checkpoint row recording its
    /// Merkle root and head hash, so `verify` still attests the full history
    /// — but the raw entries are UNRECOVERABLE by design. Refuses to run if
    /// the chain does not verify first.
    Prune {
        /// Path to the SQLite database file.
        #[arg(long, default_value = "echoes.db")]
        db: String,

        /// Agent name to prune.
        #[arg(long, default_value = "Echo")]
        name: String,

        /// Number of most-recent entries to keep (minimum 1).
        #[arg(long, value_parser = clap::value_parser!(u32).range(1..))]
        keep_last: u32,
    },
}

// ============================================================
// Entry point
// ============================================================

fn main() {
    let cli = Cli::parse();

    match cli.command {
        Commands::Run { config, db, ticks, name, goal, watch, procs, net, auth, remote_store, token } => {
            // Resolution order: CLI flag > config file > built-in default.
            // Boolean sensors are enabled by either side.
            let cfg = match config {
                Some(path) => echoes::config::Config::load(&path).unwrap_or_else(|e| {
                    eprintln!("error: {}", e);
                    std::process::exit(1);
                }),
                None => echoes::config::Config::default(),
            };
            let db    = db.or(cfg.db.clone()).unwrap_or_else(|| "echoes.db".into());
            let ticks = ticks.or(cfg.ticks).unwrap_or(8);
            let name  = name.or(cfg.name.clone()).unwrap_or_else(|| "Echo".into());
            let goal  = goal.or(cfg.goal.clone())
                .unwrap_or_else(|| "map environment with cryptographic memory".into());
            let watch = watch.or(cfg.watch.clone());
            let procs = procs || cfg.procs.unwrap_or(false);
            let net   = net || cfg.net.unwrap_or(false);
            let auth  = auth.or(cfg.auth_flag());
            let remote_store = remote_store.or(cfg.remote_store.clone());
            let token = token.or(cfg.token.clone());

            cmd_run(
                &db, ticks, &name, &goal,
                watch.as_deref(), procs, net, auth.as_deref(),
                remote_store.as_deref(), token.as_deref(),
            )
        }
        Commands::Verify { db, name }       => cmd_verify(&db, &name),
        Commands::Report { db, name, json, continuity } =>
            cmd_report(&db, &name, json, continuity),
        Commands::Prune { db, name, keep_last } => cmd_prune(&db, &name, keep_last),
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
           watch_path: Option<&str>, scan_procs: bool, scan_net: bool,
           auth_log: Option<&str>,
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

    // Chain base: zeros for a full chain, or the head of the last pruned
    // prefix when the local DB holds a checkpoint. Remote stores have no
    // pruning support yet, so their base is always zeros.
    let (base_hash, base_tick) = match remote_url {
        Some(_) => ([0u8; 32], 0),
        None => {
            let local = Store::open(db_path).unwrap_or_else(|e| {
                eprintln!("error: could not open DB at '{}': {}", db_path, e);
                std::process::exit(1);
            });
            local.chain_base(name).unwrap_or_else(|e| {
                eprintln!("error: {}", e);
                std::process::exit(2);
            })
        }
    };

    // Try to restore a pre-existing agent, otherwise create fresh. ----------
    let mut agent = match store.load_entries(name) {
        Ok(entries) if !entries.is_empty() || base_tick > 0 => {
            let stored_goal = store
                .load_agent_meta(name)
                .ok()
                .flatten()
                .map(|(g, _)| g)
                .unwrap_or_else(|| goal.to_string());

            println!("Resuming agent '{}' from {} persisted entries.", name, entries.len());
            Agent::restore_from(name, &stored_goal, entries, base_hash, base_tick)
                .unwrap_or_else(|e| {
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

    // Offline state-diff (ADR-002 Phase 9a): if this watch root was scanned
    // at the end of a previous run, diff it now and inject synthetic
    // FileAccess events *ahead of* the live sensors. Local store only —
    // manifests live next to the chain they protect.
    let manifest_store: Option<Store> = match remote_url {
        Some(_) => None,
        None => Store::open(db_path).ok(),
    };
    let mut offline_events: Vec<echoes::agent::SecurityEvent> = Vec::new();
    if let (Some(ms), Some(root)) = (&manifest_store, watch_path) {
        match ms.load_manifest(name, root) {
            Ok(old) if !old.is_empty() => {
                let now = echoes::manifest::scan_tree(root);
                offline_events = echoes::manifest::diff(&old, &now)
                    .into_iter()
                    .map(|(path, operation)| {
                        echoes::agent::SecurityEvent::FileAccess { path, operation }
                    })
                    .collect();
                if !offline_events.is_empty() {
                    println!(
                        "Offline diff: {} change(s) under '{}' since the last run.",
                        offline_events.len(),
                        root
                    );
                }
            }
            Ok(_) => {} // first run for this root — nothing to diff against
            Err(e) => eprintln!("warning: could not load manifest: {}", e),
        }
    }

    // Build the composite sensor from whichever sources were requested. -----
    use echoes::sensor::{CompositeSource, EventSource, FileWatcher, ProcessScanner};
    let mut sources: Vec<Box<dyn EventSource>> = Vec::new();

    if !offline_events.is_empty() {
        sources.push(Box::new(echoes::sensor::QueuedSource::new(offline_events)));
    }

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

    if scan_net {
        println!("Network connection scanner active.");
        sources.push(Box::new(echoes::sensor::NetScanner::new()));
    }

    if let Some(path) = auth_log {
        use echoes::sensor::AuthWatcher;
        let watcher = if path.is_empty() {
            AuthWatcher::new()
        } else {
            AuthWatcher::with_path(path)
        };
        match watcher {
            Some(w) => {
                println!("Auth-log watcher active.");
                sources.push(Box::new(w));
            }
            None => {
                eprintln!(
                    "warning: could not open an auth log (need membership in the \
                     'adm' group on Linux; unsupported on this platform?)"
                );
            }
        }
    }

    let mut composite = CompositeSource::new(sources);

    // Episode bookkeeping (Phase 9c): record this run's wall-clock and tick
    // boundaries so `report --continuity` can show coverage vs. gaps.
    let episode_id = manifest_store
        .as_ref()
        .and_then(|ms| ms.begin_episode(name, agent.tick).ok());

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

    // End-of-run manifest snapshot (Phase 9a): the next run diffs against
    // this. A crash before this point leaves the previous manifest in
    // place, so the next diff simply reports the cumulative changes.
    if let (Some(ms), Some(root)) = (&manifest_store, watch_path) {
        let snapshot = echoes::manifest::scan_tree(root);
        if let Err(e) = ms.save_manifest(name, root, &snapshot) {
            eprintln!("warning: could not save manifest: {}", e);
        } else {
            println!("Manifest saved: {} file(s) under '{}'.", snapshot.len(), root);
        }
    }

    if let (Some(ms), Some(eid)) = (&manifest_store, episode_id) {
        if let Err(e) = ms.end_episode(eid, agent.tick) {
            eprintln!("warning: could not close episode: {}", e);
        }
    }

    let tree = MerkleTree::from_memory(&agent.memory);
    println!("\nDone — {} total memories | Merkle root: {}", agent.memory_len(), tree.short_root());
}

/// Verify hash-chain integrity for a persisted agent (SQLite only).
///
/// Validates the checkpoint chain first (if the agent has ever been pruned),
/// then verifies the live chain against the checkpoint head.
fn cmd_verify(db_path: &str, name: &str) {
    let store = open_or_exit(db_path);
    let (goal, _) = load_meta_or_exit(&store, name, db_path);
    let entries   = load_entries_or_exit(&store, name, db_path);

    let checkpoint = store.verify_checkpoints(name).unwrap_or_else(|e| {
        eprintln!("Hash-chain integrity: FAILED ✗\n  {}", e);
        std::process::exit(2);
    });
    let (base_hash, base_tick) = match &checkpoint {
        Some(cp) => (cp.head, cp.pruned_through_tick),
        None => ([0u8; 32], 0),
    };

    match Agent::restore_from(name, &goal, entries, base_hash, base_tick) {
        Ok(agent) => {
            println!("Agent '{}' — {} entries loaded.", name, agent.memory_len());
            if let Some(cp) = &checkpoint {
                println!(
                    "Checkpoint:           {} sealed entries through tick {} (chain OK)",
                    cp.entries_sealed, cp.pruned_through_tick
                );
            }
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

/// Seal and delete all but the last `keep_last` entries (SQLite only).
fn cmd_prune(db_path: &str, name: &str, keep_last: u32) {
    let store = open_or_exit(db_path);
    match store.prune(name, keep_last as usize) {
        Ok(PruneOutcome::Nothing { entries }) => {
            println!(
                "Nothing to prune — {} live entries, keep-last is {}.",
                entries, keep_last
            );
        }
        Ok(PruneOutcome::Pruned { sealed, remaining, checkpoint }) => {
            println!("Sealed {} entries through tick {} — {} remain live.",
                     sealed, checkpoint.pruned_through_tick, remaining);
            println!("Checkpoint hash: {}", hex::encode(checkpoint.hash));
            println!("Sealed Merkle root: {}", hex::encode(checkpoint.merkle_root));
            println!("note: sealed entries are unrecoverable; their integrity \
                      attestation survives in the checkpoint.");
        }
        Err(e) => {
            eprintln!("error: {}", e);
            std::process::exit(2);
        }
    }
}

/// Print the full memory chain, optionally as JSON (SQLite only).
fn cmd_report(db_path: &str, name: &str, as_json: bool, continuity: bool) {
    let store   = open_or_exit(db_path);
    let (goal, _) = load_meta_or_exit(&store, name, db_path);
    let entries = load_entries_or_exit(&store, name, db_path);
    let count   = entries.len();

    let checkpoint = store.verify_checkpoints(name).unwrap_or_else(|e| {
        eprintln!("error: integrity failure during report: {}", e);
        std::process::exit(2);
    });
    let (base_hash, base_tick) = match &checkpoint {
        Some(cp) => (cp.head, cp.pruned_through_tick),
        None => ([0u8; 32], 0),
    };

    let agent = Agent::restore_from(name, &goal, entries, base_hash, base_tick)
        .unwrap_or_else(|e| {
            eprintln!("error: integrity failure during report: {}", e);
            std::process::exit(2);
        });

    let tree = MerkleTree::from_memory(&agent.memory);

    // Continuity view (Phase 9c): episodes + gaps + offline coverage.
    let continuity_data = if continuity {
        let episodes = store.load_episodes(name).unwrap_or_default();
        let summary = echoes::store::summarize_episodes(&episodes);
        let offline = agent
            .memory
            .iter()
            .filter(|e| matches!(
                &e.event,
                echoes::agent::SecurityEvent::FileAccess { operation, .. }
                    if operation.ends_with("-while-offline")
            ))
            .count();
        Some((episodes, summary, offline))
    } else {
        None
    };

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

        let mut out = json!({
            "agent":        name,
            "goal":         goal,
            "entries":      count,
            "merkle_root":  hex::encode(tree.root()),
            "integrity":    "ok",
            "memory":       mem_json,
        });
        if let Some(cp) = &checkpoint {
            out["sealed_entries"] = json!(cp.entries_sealed);
            out["sealed_through_tick"] = json!(cp.pruned_through_tick);
            out["sealed_merkle_root"] = json!(hex::encode(cp.merkle_root));
            out["checkpoint_hash"] = json!(hex::encode(cp.hash));
        }
        if let Some((episodes, summary, offline)) = &continuity_data {
            out["continuity"] = json!({
                "episodes": episodes.iter().map(|e| json!({
                    "started_at": e.started_at,
                    "ended_at":   e.ended_at,
                    "start_tick": e.start_tick,
                    "end_tick":   e.end_tick,
                })).collect::<Vec<_>>(),
                "interrupted":    summary.interrupted,
                "gaps_seconds":   summary.gaps,
                "live_seconds":   summary.live_seconds,
                "span_seconds":   summary.span_seconds,
                "offline_events": offline,
            });
        }
        println!("{}", serde_json::to_string_pretty(&out).unwrap());
    } else {
        agent.print_memory_chain();
        agent.print_audit_log();
        if let Some(cp) = &checkpoint {
            println!(
                "\nSealed prefix: {} entries through tick {} | checkpoint: {}",
                cp.entries_sealed,
                cp.pruned_through_tick,
                echoes::agent::short_hash(&cp.hash)
            );
        }
        if let Some((episodes, summary, offline)) = &continuity_data {
            println!("\n--- Continuity for {} ---", name);
            if episodes.is_empty() {
                println!("  (no episode data — DB predates Phase 9c)");
            } else {
                println!(
                    "  Episodes: {} ({} clean, {} interrupted)",
                    summary.episodes,
                    summary.episodes - summary.interrupted,
                    summary.interrupted
                );
                for (i, ep) in episodes.iter().enumerate() {
                    match (ep.ended_at, ep.end_tick) {
                        (Some(end), Some(et)) => println!(
                            "  [{}] ticks {:>4}-{:<4} {}s",
                            i + 1, ep.start_tick, et, end - ep.started_at
                        ),
                        _ => println!(
                            "  [{}] ticks {:>4}-...  INTERRUPTED",
                            i + 1, ep.start_tick
                        ),
                    }
                    if i < summary.gaps.len() {
                        println!("       gap {}s", summary.gaps[i]);
                    }
                }
                println!(
                    "  Live {}s of {}s span | offline-diff events: {}",
                    summary.live_seconds, summary.span_seconds, offline
                );
            }
        }
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

fn load_entries_or_exit(store: &Store, name: &str, db_path: &str) -> Vec<echoes::agent::MemoryEntry> {
    store.load_entries(name).unwrap_or_else(|e| {
        eprintln!("error: could not load entries for '{}' from '{}': {}", name, db_path, e);
        std::process::exit(1);
    })
}
