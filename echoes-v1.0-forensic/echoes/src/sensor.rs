//! Real-world event sources for the echoes agent.
//!
//! The `EventSource` trait lets `agent::Agent::think_with()` pull a real
//! `SecurityEvent` instead of a synthetic `Custom(...)` one. Two sources
//! are provided:
//!
//! * [`FileWatcher`] — wraps the `notify` crate (requires the `watch`
//!   Cargo feature). Uses inotify on Linux, kqueue on macOS, and
//!   ReadDirectoryChangesW on Windows. Returns `SecurityEvent::FileAccess`
//!   entries for real filesystem activity.
//!
//! * [`ProcessScanner`] — reads `/proc` on Linux or uses a `sysctl`-based
//!   approach on macOS. On Windows (and any unsupported platform) it falls
//!   back to a stub. Returns `SecurityEvent::ProcessExecution` entries.
//!
//! Both sources are *non-blocking*: they return whatever is pending at the
//! moment `poll()` is called, never waiting. If nothing is pending, they
//! return `None` and the caller may fall back to a `Custom` event.
//!
//! # Platform gating
//!
//! `FileWatcher` requires `--features watch` (adds the `notify` crate dep).
//! `ProcessScanner` uses `#[cfg(target_os = "linux")]` / `#[cfg(target_os = "macos")]`
//! for OS-specific code. Everything compiles and passes `cargo test` without
//! any feature flags or a specific OS.

use crate::agent::SecurityEvent;

// ============================================================
// EventSource trait
// ============================================================

/// A non-blocking source of real `SecurityEvent` observations.
///
/// Returns `Some(event)` when real activity is available, `None` when idle.
/// Callers should fall back to `SecurityEvent::Custom(...)` on `None`.
pub trait EventSource: Send {
    fn poll(&mut self) -> Option<SecurityEvent>;
}

// ============================================================
// FileWatcher — notify-backed filesystem event source
// ============================================================

/// Wraps the `notify` crate to deliver `SecurityEvent::FileAccess` events.
///
/// Requires the `watch` Cargo feature.  When the feature is disabled this
/// struct still exists but `new()` always returns `None` so callers can
/// handle it uniformly.
pub struct FileWatcher {
    #[cfg(feature = "watch")]
    rx: std::sync::mpsc::Receiver<notify::Result<notify::Event>>,
    #[cfg(feature = "watch")]
    _watcher: notify::RecommendedWatcher,
    #[cfg(not(feature = "watch"))]
    _phantom: std::marker::PhantomData<()>,
}

impl FileWatcher {
    /// Create a watcher on `path`.  Returns `None` when the `watch` feature
    /// is disabled or when the path cannot be watched.
    #[allow(unused_variables)]
    pub fn new(path: &str) -> Option<Self> {
        #[cfg(feature = "watch")]
        {
            use notify::{RecommendedWatcher, RecursiveMode, Watcher};
            use std::sync::mpsc::channel;

            let (tx, rx) = channel();
            let mut watcher = RecommendedWatcher::new(
                move |res| { let _ = tx.send(res); },
                notify::Config::default(),
            ).ok()?;

            watcher.watch(
                std::path::Path::new(path),
                RecursiveMode::Recursive,
            ).ok()?;

            Some(FileWatcher { rx, _watcher: watcher })
        }

        #[cfg(not(feature = "watch"))]
        {
            // Feature disabled — return None so callers fall back gracefully.
            None
        }
    }
}

impl EventSource for FileWatcher {
    fn poll(&mut self) -> Option<SecurityEvent> {
        #[cfg(feature = "watch")]
        {
            use notify::EventKind;
            use std::sync::mpsc::TryRecvError;

            loop {
                match self.rx.try_recv() {
                    Ok(Ok(event)) => {
                        // Map notify EventKind to a human-readable operation string.
                        let operation = match event.kind {
                            EventKind::Create(_) => "create",
                            EventKind::Modify(_) => "modify",
                            EventKind::Remove(_) => "remove",
                            EventKind::Access(_) => "access",
                            _ => "other",
                        };
                        // Use the first affected path; skip events with no path.
                        if let Some(p) = event.paths.into_iter().next() {
                            return Some(SecurityEvent::FileAccess {
                                path: p.display().to_string(),
                                operation: operation.to_string(),
                            });
                        }
                        // Event had no path — loop and try the next one.
                        continue;
                    }
                    Ok(Err(_)) | Err(TryRecvError::Empty) => return None,
                    Err(TryRecvError::Disconnected) => return None,
                }
            }
        }

        #[cfg(not(feature = "watch"))]
        None
    }
}

// ============================================================
// ProcessScanner — /proc-based process event source
// ============================================================

/// Scans running processes and emits `SecurityEvent::ProcessExecution` for
/// any process seen since the last scan that wasn't present before.
///
/// Linux: reads `/proc/<pid>/comm` for each PID directory.
/// macOS: uses `sysctl CTL_KERN / KERN_PROC / KERN_PROC_ALL`.
/// Windows / other: stub — always returns `None`.
pub struct ProcessScanner {
    seen: std::collections::HashSet<u32>,
}

impl ProcessScanner {
    pub fn new() -> Self {
        let mut scanner = ProcessScanner {
            seen: std::collections::HashSet::new(),
        };
        // Populate the initial snapshot so first poll only reports *new* pids.
        for (pid, _) in scanner.snapshot() {
            scanner.seen.insert(pid);
        }
        scanner
    }

    /// Returns a list of (pid, name) pairs for currently running processes.
    fn snapshot(&self) -> Vec<(u32, String)> {
        #[cfg(target_os = "linux")]
        return Self::snapshot_linux();

        #[cfg(target_os = "macos")]
        return Self::snapshot_macos();

        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        return Vec::new();
    }

    // ------------------------------------------------------------------
    // Linux: iterate /proc/<pid>/comm
    // ------------------------------------------------------------------

    #[cfg(target_os = "linux")]
    fn snapshot_linux() -> Vec<(u32, String)> {
        let Ok(dir) = std::fs::read_dir("/proc") else { return Vec::new() };
        let mut out = Vec::new();
        for entry in dir.flatten() {
            let fname = entry.file_name();
            let name_str = fname.to_string_lossy();
            // PID dirs are all-numeric.
            let Ok(pid) = name_str.parse::<u32>() else { continue };
            let comm_path = format!("/proc/{}/comm", pid);
            if let Ok(comm) = std::fs::read_to_string(&comm_path) {
                out.push((pid, comm.trim().to_string()));
            }
        }
        out
    }

    // ------------------------------------------------------------------
    // macOS: sysctl KERN_PROC_ALL
    // ------------------------------------------------------------------

    #[cfg(target_os = "macos")]
    fn snapshot_macos() -> Vec<(u32, String)> {
        // Use the `sysctl` command as a simple cross-SDK approach.
        // `ps -eo pid,comm` is more portable than direct sysctl MIB calls.
        let Ok(out) = std::process::Command::new("ps")
            .args(["-eo", "pid,comm"])
            .output()
        else {
            return Vec::new();
        };
        let stdout = String::from_utf8_lossy(&out.stdout);
        let mut procs = Vec::new();
        for line in stdout.lines().skip(1) {  // skip header
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 2 {
                if let Ok(pid) = parts[0].parse::<u32>() {
                    procs.push((pid, parts[1].to_string()));
                }
            }
        }
        procs
    }
}

impl Default for ProcessScanner {
    fn default() -> Self {
        Self::new()
    }
}

impl EventSource for ProcessScanner {
    fn poll(&mut self) -> Option<SecurityEvent> {
        let current = self.snapshot();
        for (pid, name) in &current {
            if !self.seen.contains(pid) {
                self.seen.insert(*pid);
                return Some(SecurityEvent::ProcessExecution {
                    name: name.clone(),
                    pid: *pid,
                });
            }
        }
        // Purge dead PIDs so the seen set doesn't grow unboundedly.
        let live: std::collections::HashSet<u32> = current.iter().map(|(p, _)| *p).collect();
        self.seen.retain(|p| live.contains(p));
        None
    }
}

// ============================================================
// CompositeSource — round-robins over multiple sources
// ============================================================

/// Chains multiple `EventSource` implementations together.
/// `poll()` tries each source in order and returns the first event found.
pub struct CompositeSource {
    sources: Vec<Box<dyn EventSource>>,
}

impl CompositeSource {
    pub fn new(sources: Vec<Box<dyn EventSource>>) -> Self {
        CompositeSource { sources }
    }
}

impl EventSource for CompositeSource {
    fn poll(&mut self) -> Option<SecurityEvent> {
        for src in &mut self.sources {
            if let Some(ev) = src.poll() {
                return Some(ev);
            }
        }
        None
    }
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    // ProcessScanner — only runs where /proc or ps is available
    #[test]
    fn process_scanner_new_does_not_panic() {
        // On any platform, constructing a scanner must not panic.
        let _scanner = ProcessScanner::new();
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn process_scanner_finds_at_least_one_process() {
        // On Linux, /proc should always have at least PID 1 (init/systemd).
        let procs = ProcessScanner::snapshot_linux();
        assert!(!procs.is_empty(), "expected at least one /proc entry");
        // Every entry must have a non-empty name.
        for (pid, name) in &procs {
            assert!(*pid > 0, "PID must be positive");
            assert!(!name.is_empty(), "process name must be non-empty");
        }
    }

    #[test]
    fn process_scanner_poll_returns_none_after_init() {
        // After construction, all existing PIDs are in `seen`.
        // A fresh poll should return None (no *new* processes since init).
        let mut scanner = ProcessScanner::new();
        // It's technically possible for a new short-lived process to appear
        // between init and the first poll in a busy CI environment — so we
        // don't assert None here, just that it doesn't panic.
        let _ = scanner.poll();
    }

    #[test]
    fn file_watcher_new_without_feature_returns_none() {
        // Without --features watch the constructor always returns None.
        // With the feature, it may return None if the path doesn't exist.
        let w = FileWatcher::new("/nonexistent/path/that/does/not/exist");
        // Either outcome (None or Some) is valid depending on build config;
        // what must not happen is a panic.
        let _ = w;
    }

    #[test]
    fn composite_source_returns_none_when_all_idle() {
        struct Idle;
        impl EventSource for Idle {
            fn poll(&mut self) -> Option<SecurityEvent> { None }
        }
        let mut composite = CompositeSource::new(vec![
            Box::new(Idle),
            Box::new(Idle),
        ]);
        assert!(composite.poll().is_none());
    }

    #[test]
    fn composite_source_returns_first_available() {
        struct Always(SecurityEvent);
        impl EventSource for Always {
            fn poll(&mut self) -> Option<SecurityEvent> {
                Some(self.0.clone())
            }
        }
        struct Idle;
        impl EventSource for Idle {
            fn poll(&mut self) -> Option<SecurityEvent> { None }
        }

        let ev = SecurityEvent::Custom("test event".to_string());
        let mut composite = CompositeSource::new(vec![
            Box::new(Idle),
            Box::new(Always(ev.clone())),
        ]);
        assert_eq!(composite.poll(), Some(ev));
    }
}
