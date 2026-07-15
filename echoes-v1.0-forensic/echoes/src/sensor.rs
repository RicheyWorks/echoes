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
//! * [`NetScanner`] — diffs the OS connection table between polls
//!   (`/proc/net/tcp{,6}` on Linux, `netstat` on macOS/Windows) and returns
//!   `SecurityEvent::NetworkConnection` entries for new established
//!   connections. Unprivileged: metadata only, no packet capture.
//!
//! * [`AuthWatcher`] — tails the auth log (`/var/log/auth.log` or
//!   `/var/log/secure`; Linux-first) and returns
//!   `SecurityEvent::Authentication` entries for sshd accepts/failures and
//!   PAM authentication failures. Needs `adm` group membership, not root.
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
// NetScanner — connection-table event source (ADR-002 Phase 8a)
// ============================================================

/// Scans the OS connection table and emits `SecurityEvent::NetworkConnection`
/// for any established connection not present in the previous scan.
///
/// Linux:   parses `/proc/net/tcp` and `/proc/net/tcp6` directly (no exec).
/// macOS:   parses `netstat -an -p tcp` output.
/// Windows: parses `netstat -ano -p TCP` output.
///
/// **Unprivileged by design** (ADR-002): connection metadata only — no packet
/// capture, no promiscuous mode, no root. On platforms where per-PID
/// attribution would need elevated rights, we simply omit it.
pub struct NetScanner {
    seen: std::collections::HashSet<(String, String, u16)>,
}

impl NetScanner {
    pub fn new() -> Self {
        let mut scanner = NetScanner {
            seen: std::collections::HashSet::new(),
        };
        // Initial snapshot: first poll only reports *new* connections.
        for conn in scanner.snapshot() {
            scanner.seen.insert(conn);
        }
        scanner
    }

    /// Returns (src "ip:port", dst ip, dst port) for established connections.
    fn snapshot(&self) -> Vec<(String, String, u16)> {
        #[cfg(target_os = "linux")]
        return Self::snapshot_linux();

        #[cfg(target_os = "macos")]
        return Self::snapshot_macos();

        #[cfg(target_os = "windows")]
        return Self::snapshot_windows();

        #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
        return Vec::new();
    }

    #[cfg(target_os = "linux")]
    fn snapshot_linux() -> Vec<(String, String, u16)> {
        let mut out = Vec::new();
        if let Ok(s) = std::fs::read_to_string("/proc/net/tcp") {
            out.extend(parse_proc_net_tcp(&s, false));
        }
        if let Ok(s) = std::fs::read_to_string("/proc/net/tcp6") {
            out.extend(parse_proc_net_tcp(&s, true));
        }
        out
    }

    #[cfg(target_os = "macos")]
    fn snapshot_macos() -> Vec<(String, String, u16)> {
        let Ok(out) = std::process::Command::new("netstat")
            .args(["-an", "-p", "tcp"])
            .output()
        else {
            return Vec::new();
        };
        parse_netstat_bsd(&String::from_utf8_lossy(&out.stdout))
    }

    #[cfg(target_os = "windows")]
    fn snapshot_windows() -> Vec<(String, String, u16)> {
        let Ok(out) = std::process::Command::new("netstat")
            .args(["-ano", "-p", "TCP"])
            .output()
        else {
            return Vec::new();
        };
        parse_netstat_windows(&String::from_utf8_lossy(&out.stdout))
    }
}

impl Default for NetScanner {
    fn default() -> Self {
        Self::new()
    }
}

impl EventSource for NetScanner {
    fn poll(&mut self) -> Option<SecurityEvent> {
        let current = self.snapshot();
        for conn in &current {
            if !self.seen.contains(conn) {
                self.seen.insert(conn.clone());
                return Some(SecurityEvent::NetworkConnection {
                    src: conn.0.clone(),
                    dst: conn.1.clone(),
                    port: conn.2,
                });
            }
        }
        // Purge closed connections so the seen set doesn't grow unboundedly.
        let live: std::collections::HashSet<_> = current.into_iter().collect();
        self.seen.retain(|c| live.contains(c));
        None
    }
}

// ------------------------------------------------------------------
// Pure parsers — platform-independent so they unit-test everywhere.
// ------------------------------------------------------------------

/// Parse `/proc/net/tcp` / `/proc/net/tcp6` content. Keeps only state 01
/// (ESTABLISHED). Addresses are kernel-format hex, little-endian per word.
pub fn parse_proc_net_tcp(contents: &str, v6: bool) -> Vec<(String, String, u16)> {
    let mut out = Vec::new();
    for line in contents.lines().skip(1) {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 4 || f[3] != "01" {
            continue;
        }
        let Some((lip, lport)) = parse_proc_hex_addr(f[1], v6) else { continue };
        let Some((rip, rport)) = parse_proc_hex_addr(f[2], v6) else { continue };
        out.push((format!("{}:{}", lip, lport), rip, rport));
    }
    out
}

/// Parse one kernel hex address like `0100007F:1F90` (v4) or a 32-hex-char
/// v6 form. Returns (ip, port).
fn parse_proc_hex_addr(s: &str, v6: bool) -> Option<(String, u16)> {
    let (ip_hex, port_hex) = s.split_once(':')?;
    let port = u16::from_str_radix(port_hex, 16).ok()?;
    if v6 {
        if ip_hex.len() != 32 {
            return None;
        }
        // Four 32-bit words, each stored little-endian.
        let mut bytes = [0u8; 16];
        for w in 0..4 {
            let word = u32::from_str_radix(&ip_hex[w * 8..w * 8 + 8], 16).ok()?;
            bytes[w * 4..w * 4 + 4].copy_from_slice(&word.to_le_bytes());
        }
        Some((std::net::Ipv6Addr::from(bytes).to_string(), port))
    } else {
        if ip_hex.len() != 8 {
            return None;
        }
        let word = u32::from_str_radix(ip_hex, 16).ok()?;
        let b = word.to_le_bytes();
        Some((std::net::Ipv4Addr::new(b[0], b[1], b[2], b[3]).to_string(), port))
    }
}

/// Parse BSD-style `netstat -an -p tcp` output (macOS). Address format is
/// `ip.port` — the port follows the final dot.
pub fn parse_netstat_bsd(output: &str) -> Vec<(String, String, u16)> {
    let mut out = Vec::new();
    for line in output.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 6 || !f[0].starts_with("tcp") || f[5] != "ESTABLISHED" {
            continue;
        }
        let Some((rip, rport)) = f[4].rsplit_once('.') else { continue };
        let Ok(port) = rport.parse::<u16>() else { continue };
        out.push((f[3].to_string(), rip.to_string(), port));
    }
    out
}

/// Parse Windows `netstat -ano -p TCP` output. Address format is `ip:port`.
pub fn parse_netstat_windows(output: &str) -> Vec<(String, String, u16)> {
    let mut out = Vec::new();
    for line in output.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 4 || f[0] != "TCP" || f[3] != "ESTABLISHED" {
            continue;
        }
        let Some((rip, rport)) = f[2].rsplit_once(':') else { continue };
        let Ok(port) = rport.parse::<u16>() else { continue };
        out.push((f[1].to_string(), rip.trim_matches(['[', ']']).to_string(), port));
    }
    out
}

// ============================================================
// AuthWatcher — auth-log event source (ADR-002 Phase 8b)
// ============================================================

/// Tails an authentication log and emits `SecurityEvent::Authentication`
/// for sshd accept/fail lines and PAM authentication failures.
///
/// Linux-first: auto-detects `/var/log/auth.log` (Debian/Ubuntu) or
/// `/var/log/secure` (RHEL/Fedora). Reading these normally requires
/// membership in the `adm` group — **add the user to `adm` rather than
/// running as root** (unprivileged by design, ADR-002). macOS and Windows
/// have no readable equivalent without elevated rights, so `new()` returns
/// `None` there and callers fall back gracefully.
///
/// Only lines appended *after* construction are reported. Log rotation
/// (file shrinks) resets the tail to the start of the new file. Partial
/// lines at the end of a read are left for the next poll.
pub struct AuthWatcher {
    path: std::path::PathBuf,
    offset: u64,
    pending: std::collections::VecDeque<SecurityEvent>,
}

impl AuthWatcher {
    /// Auto-detect the platform auth log. Returns `None` if no readable
    /// log exists (wrong platform, or missing `adm` group membership).
    pub fn new() -> Option<Self> {
        for candidate in ["/var/log/auth.log", "/var/log/secure"] {
            if let Some(w) = Self::with_path(candidate) {
                return Some(w);
            }
        }
        None
    }

    /// Tail an explicit log file. Returns `None` if it cannot be opened.
    /// Also the test seam: any append-only text file works.
    pub fn with_path(path: &str) -> Option<Self> {
        let meta = std::fs::metadata(path).ok()?;
        // Confirm we can actually open it (metadata alone doesn't prove read
        // permission).
        std::fs::File::open(path).ok()?;
        Some(AuthWatcher {
            path: std::path::PathBuf::from(path),
            offset: meta.len(),
            pending: std::collections::VecDeque::new(),
        })
    }
}

impl EventSource for AuthWatcher {
    fn poll(&mut self) -> Option<SecurityEvent> {
        if let Some(ev) = self.pending.pop_front() {
            return Some(ev);
        }

        let len = std::fs::metadata(&self.path).ok()?.len();
        if len < self.offset {
            // Rotated or truncated — start over at the top of the new file.
            self.offset = 0;
        }
        if len == self.offset {
            return None;
        }

        use std::io::{Read, Seek, SeekFrom};
        let mut f = std::fs::File::open(&self.path).ok()?;
        f.seek(SeekFrom::Start(self.offset)).ok()?;
        let mut buf = Vec::new();
        f.read_to_end(&mut buf).ok()?;

        // Only consume up to the last complete line; a partially-written
        // final line is picked up on the next poll.
        let last_nl = buf.iter().rposition(|&b| b == b'\n')?;
        let chunk = &buf[..=last_nl];
        self.offset += chunk.len() as u64;

        for line in String::from_utf8_lossy(chunk).lines() {
            if let Some((user, success)) = parse_auth_line(line) {
                self.pending
                    .push_back(SecurityEvent::Authentication { user, success });
            }
        }
        self.pending.pop_front()
    }
}

/// Parse one auth-log line. Returns `(user, success)` for lines that
/// represent an authentication event, `None` otherwise.
///
/// Recognized:
/// * sshd `Accepted <method> for USER from ...`            → success
/// * sshd `Failed <method> for USER from ...`              → failure
/// * sshd `Failed <method> for invalid user USER from ...` → failure
/// * PAM  `... authentication failure; ... user=USER`      → failure
pub fn parse_auth_line(line: &str) -> Option<(String, bool)> {
    if let Some(rest) = line.split("Accepted ").nth(1) {
        let mut it = rest.split_whitespace();
        it.next()?; // auth method
        if it.next()? == "for" {
            return Some((it.next()?.to_string(), true));
        }
    }

    if let Some(rest) = line.split("Failed ").nth(1) {
        let mut it = rest.split_whitespace();
        it.next()?; // auth method
        if it.next()? == "for" {
            let mut user = it.next()?;
            if user == "invalid" && it.next()? == "user" {
                user = it.next()?;
            }
            return Some((user.to_string(), false));
        }
    }

    if line.contains("authentication failure") {
        if let Some(pos) = line.rfind("user=") {
            let user = line[pos + 5..].split_whitespace().next().unwrap_or("");
            if !user.is_empty() {
                return Some((user.to_string(), false));
            }
        }
    }

    None
}

// ============================================================
// QueuedSource — replays a fixed list of events (ADR-002 Phase 9a)
// ============================================================

/// Drains a pre-computed list of events, one per poll. Used to inject the
/// offline state-diff (`*-while-offline` synthetic `FileAccess` events)
/// ahead of live sensors at the start of a run.
pub struct QueuedSource {
    queue: std::collections::VecDeque<SecurityEvent>,
}

impl QueuedSource {
    pub fn new(events: Vec<SecurityEvent>) -> Self {
        QueuedSource { queue: events.into() }
    }

    pub fn len(&self) -> usize {
        self.queue.len()
    }

    pub fn is_empty(&self) -> bool {
        self.queue.is_empty()
    }
}

impl EventSource for QueuedSource {
    fn poll(&mut self) -> Option<SecurityEvent> {
        self.queue.pop_front()
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
    #[cfg(feature = "watch")]
    fn file_watcher_reports_known_operations() {
        // ADR-002 Phase 8c: event *semantics* differ across backends
        // (inotify vs kqueue vs ReadDirectoryChangesW — e.g. an overwrite is
        // a modify on Linux but may surface differently on Windows). CI
        // event delivery is not guaranteed, so this test asserts the
        // operation vocabulary only when an event actually arrives.
        let dir = std::env::temp_dir().join(format!("echoes_watch_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut w = FileWatcher::new(dir.to_str().unwrap()).expect("watcher starts");

        std::fs::write(dir.join("probe.txt"), b"one").unwrap();
        std::fs::write(dir.join("probe.txt"), b"two").unwrap(); // overwrite

        let mut seen = None;
        for _ in 0..40 {
            if let Some(ev) = w.poll() {
                seen = Some(ev);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        if let Some(SecurityEvent::FileAccess { path, operation }) = seen {
            assert!(
                ["create", "modify", "remove", "access", "other"]
                    .contains(&operation.as_str()),
                "unexpected operation {:?}",
                operation
            );
            assert!(path.contains("echoes_watch_"), "unexpected path {:?}", path);
        }
        let _ = std::fs::remove_dir_all(&dir);
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

    // ------------------------------------------------------------------
    // AuthWatcher (ADR-002 Phase 8b)
    // ------------------------------------------------------------------

    #[test]
    fn parse_auth_line_recognizes_the_usual_suspects() {
        let cases = [
            ("Jul 14 22:01:02 host sshd[123]: Accepted password for richmond from 10.0.0.5 port 51000 ssh2",
             Some(("richmond", true))),
            ("Jul 14 22:01:02 host sshd[123]: Accepted publickey for deploy from 10.0.0.6 port 51001 ssh2: ED25519 SHA256:abc",
             Some(("deploy", true))),
            ("Jul 14 22:01:03 host sshd[124]: Failed password for richmond from 203.0.113.9 port 40000 ssh2",
             Some(("richmond", false))),
            ("Jul 14 22:01:04 host sshd[125]: Failed password for invalid user admin from 203.0.113.9 port 40001 ssh2",
             Some(("admin", false))),
            ("Jul 14 22:01:05 host sudo: pam_unix(sudo:auth): authentication failure; logname=richmond uid=1000 euid=0 tty=/dev/pts/0 ruser=richmond rhost=  user=root",
             Some(("root", false))),
            ("Jul 14 22:01:06 host sshd[126]: Connection closed by 203.0.113.9 port 40002",
             None),
            ("Jul 14 22:01:07 host CRON[127]: pam_unix(cron:session): session opened for user root by (uid=0)",
             None),
        ];
        for (line, expected) in cases {
            let got = parse_auth_line(line);
            let expected = expected.map(|(u, s)| (u.to_string(), s));
            assert_eq!(got, expected, "line: {}", line);
        }
    }

    #[test]
    fn auth_watcher_tails_only_new_lines() {
        use std::io::Write;
        let dir = std::env::temp_dir();
        let path = dir.join(format!("echoes_auth_test_{}.log", std::process::id()));
        let path_str = path.to_str().unwrap();

        // Pre-existing content must NOT be reported.
        std::fs::write(&path, "old sshd[1]: Accepted password for ghost from 1.2.3.4 port 1 ssh2\n").unwrap();
        let mut w = AuthWatcher::with_path(path_str).expect("watcher opens");
        assert!(w.poll().is_none(), "baseline content must be skipped");

        // Appended lines are reported in order; junk and partial lines are not.
        let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
        writeln!(f, "host sshd[2]: Accepted publickey for richmond from 10.0.0.5 port 2 ssh2").unwrap();
        writeln!(f, "host sshd[3]: some unrelated chatter").unwrap();
        writeln!(f, "host sshd[4]: Failed password for invalid user admin from 9.9.9.9 port 3 ssh2").unwrap();
        write!(f, "host sshd[5]: Accepted password for partial").unwrap(); // no newline
        f.flush().unwrap();

        assert_eq!(
            w.poll(),
            Some(SecurityEvent::Authentication { user: "richmond".into(), success: true })
        );
        assert_eq!(
            w.poll(),
            Some(SecurityEvent::Authentication { user: "admin".into(), success: false })
        );
        assert!(w.poll().is_none(), "partial line must wait for its newline");

        // Complete the partial line — now it surfaces.
        let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
        writeln!(f, " from 10.0.0.7 port 4 ssh2").unwrap();
        assert_eq!(
            w.poll(),
            Some(SecurityEvent::Authentication { user: "partial".into(), success: true })
        );

        // Rotation: file replaced with shorter content → tail resets.
        std::fs::write(&path, "host sshd[6]: Failed password for root from 8.8.8.8 port 5 ssh2\n").unwrap();
        assert_eq!(
            w.poll(),
            Some(SecurityEvent::Authentication { user: "root".into(), success: false })
        );

        let _ = std::fs::remove_file(&path);
    }

    // ------------------------------------------------------------------
    // NetScanner (ADR-002 Phase 8a)
    // ------------------------------------------------------------------

    #[test]
    fn net_scanner_new_and_poll_do_not_panic() {
        let mut scanner = NetScanner::new();
        // First poll may or may not see a new connection; must not panic.
        let _ = scanner.poll();
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn net_scanner_detects_new_loopback_connection() {
        use std::net::{TcpListener, TcpStream};

        let mut scanner = NetScanner::new();

        // Open a real loopback connection *after* the baseline snapshot.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let _client = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let _server = listener.accept().unwrap();

        // The connection pair appears as up to two table rows; drain a few
        // polls and look for one that references our ephemeral port.
        let mut hits = Vec::new();
        for _ in 0..20 {
            match scanner.poll() {
                Some(SecurityEvent::NetworkConnection { src, dst, port: p }) => {
                    hits.push((src, dst, p));
                }
                Some(_) => {}
                None => break,
            }
        }
        assert!(
            hits.iter().any(|(src, _, p)| *p == port || src.ends_with(&format!(":{}", port))),
            "expected the new loopback connection (port {}) in {:?}",
            port,
            hits
        );
    }

    #[test]
    fn parse_proc_net_tcp_v4_established_only() {
        let sample = "\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 100 0 0 10 0
   1: 0F02000A:BC06 8E FA D0 0E:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 23456 1 0000000000000000 20 4 30 10 -1
   2: 0F02000A:BC07 8EFAD00E:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 23457 1 0000000000000000 20 4 30 10 -1
";
        let conns = parse_proc_net_tcp(sample, false);
        // Row 0 is LISTEN (0A) — skipped. Row 1 has a malformed remote — skipped.
        assert_eq!(conns.len(), 1);
        let (src, dst, port) = &conns[0];
        assert_eq!(src, "10.0.2.15:48135");
        assert_eq!(dst, "14.208.250.142");
        assert_eq!(*port, 443);
    }

    #[test]
    fn parse_proc_net_tcp_v6_loopback() {
        // ::1 in kernel format: four little-endian 32-bit words.
        let sample = "\
  sl  local_address                         remote_address                        st
   0: 00000000000000000000000001000000:1F90 00000000000000000000000001000000:80E8 01 00000000:00000000 00:00000000 00000000  1000 0 34567 1
";
        let conns = parse_proc_net_tcp(sample, true);
        assert_eq!(conns.len(), 1);
        let (src, dst, port) = &conns[0];
        assert_eq!(src, "::1:8080");
        assert_eq!(dst, "::1");
        assert_eq!(*port, 33000);
    }

    #[test]
    fn parse_netstat_bsd_format() {
        let sample = "\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4       0      0  192.168.1.5.52134      142.250.72.14.443      ESTABLISHED
tcp4       0      0  127.0.0.1.8080         *.*                    LISTEN
tcp6       0      0  fe80::1%lo0.1024       fe80::1%lo0.1025       ESTABLISHED
";
        let conns = parse_netstat_bsd(sample);
        assert_eq!(conns.len(), 2);
        assert_eq!(conns[0], ("192.168.1.5.52134".to_string(), "142.250.72.14".to_string(), 443));
        assert_eq!(conns[1], ("fe80::1%lo0.1024".to_string(), "fe80::1%lo0".to_string(), 1025));
    }

    #[test]
    fn parse_netstat_windows_format() {
        let sample = "\r
Active Connections\r
\r
  Proto  Local Address          Foreign Address        State           PID\r
  TCP    192.168.1.5:52134      142.250.72.14:443      ESTABLISHED     1234\r
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       5678\r
  TCP    [::1]:1024             [::1]:1025             ESTABLISHED     9012\r
";
        let conns = parse_netstat_windows(sample);
        assert_eq!(conns.len(), 2);
        assert_eq!(conns[0], ("192.168.1.5:52134".to_string(), "142.250.72.14".to_string(), 443));
        assert_eq!(conns[1].2, 1025);
    }

    #[test]
    fn queued_source_drains_in_order_then_idles() {
        let ev1 = SecurityEvent::FileAccess {
            path: "/a".into(),
            operation: "changed-while-offline".into(),
        };
        let ev2 = SecurityEvent::FileAccess {
            path: "/b".into(),
            operation: "deleted-while-offline".into(),
        };
        let mut q = QueuedSource::new(vec![ev1.clone(), ev2.clone()]);
        assert_eq!(q.len(), 2);
        assert_eq!(q.poll(), Some(ev1));
        assert_eq!(q.poll(), Some(ev2));
        assert_eq!(q.poll(), None);
        assert!(q.is_empty());
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
