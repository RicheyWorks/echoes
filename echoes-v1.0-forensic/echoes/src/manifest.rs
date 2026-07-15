//! Offline state-diff manifests (ADR-002 Phase 9a).
//!
//! Chained micro-runs (Phase 9) leave blind-spot windows between runs: file
//! events that occur while no process is watching are lost to inotify. The
//! manifest closes most of that gap. At the end of a run, `scan_tree`
//! snapshots the watched tree (path, size, mtime, SHA-256) into the store;
//! at the start of the next run, the tree is rescanned and `diff` emits
//! synthetic `FileAccess` events — `created-while-offline`,
//! `changed-while-offline`, `deleted-while-offline` — into the hash chain
//! before live watching begins.
//!
//! Content is SHA-256 hashed, so timestomping (resetting mtime after an
//! edit) does not evade the diff. Symlinks are not followed.

use sha2::{Digest, Sha256};
use std::path::Path;

/// One file's recorded state.
#[derive(Debug, Clone, PartialEq)]
pub struct FileState {
    /// Absolute (or root-relative, as scanned) path.
    pub path: String,
    pub size: u64,
    /// Seconds since the Unix epoch; 0 if unavailable.
    pub mtime: i64,
    /// Lowercase hex SHA-256 of the content; empty if unreadable.
    pub sha_hex: String,
}

/// Recursively snapshot all regular files under `root`, sorted by path.
/// Unreadable entries are skipped — a forensic scan must not abort because
/// one file is locked.
pub fn scan_tree(root: &str) -> Vec<FileState> {
    let mut out = Vec::new();
    walk(Path::new(root), &mut out);
    out.sort_by(|a, b| a.path.cmp(&b.path));
    out
}

fn walk(dir: &Path, out: &mut Vec<FileState>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for entry in rd.flatten() {
        let path = entry.path();
        // DirEntry::metadata does not traverse symlinks.
        let Ok(meta) = entry.metadata() else { continue };
        if meta.is_dir() {
            walk(&path, out);
        } else if meta.is_file() {
            let mtime = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            out.push(FileState {
                path: path.display().to_string(),
                size: meta.len(),
                mtime,
                sha_hex: hash_file(&path).unwrap_or_default(),
            });
        }
    }
}

fn hash_file(path: &Path) -> Option<String> {
    let mut f = std::fs::File::open(path).ok()?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut f, &mut hasher).ok()?;
    Some(hex::encode(hasher.finalize()))
}

/// Compare two manifests. Returns `(path, operation)` pairs, sorted, with
/// operation one of `created-while-offline`, `changed-while-offline`,
/// `deleted-while-offline`.
pub fn diff(old: &[FileState], new: &[FileState]) -> Vec<(String, String)> {
    use std::collections::HashMap;
    let old_map: HashMap<&str, &FileState> =
        old.iter().map(|f| (f.path.as_str(), f)).collect();
    let new_map: HashMap<&str, &FileState> =
        new.iter().map(|f| (f.path.as_str(), f)).collect();

    let mut out = Vec::new();
    for f in new {
        match old_map.get(f.path.as_str()) {
            None => out.push((f.path.clone(), "created-while-offline".to_string())),
            Some(o) => {
                if o.sha_hex != f.sha_hex || o.size != f.size || o.mtime != f.mtime {
                    out.push((f.path.clone(), "changed-while-offline".to_string()));
                }
            }
        }
    }
    for f in old {
        if !new_map.contains_key(f.path.as_str()) {
            out.push((f.path.clone(), "deleted-while-offline".to_string()));
        }
    }
    out.sort();
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_root(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "echoes_manifest_{}_{}",
            tag,
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        dir
    }

    #[test]
    fn scan_finds_nested_files_sorted() {
        let dir = tmp_root("scan");
        std::fs::write(dir.join("b.txt"), b"bee").unwrap();
        std::fs::write(dir.join("sub/a.txt"), b"aye").unwrap();

        let m = scan_tree(dir.to_str().unwrap());
        assert_eq!(m.len(), 2);
        assert!(m[0].path <= m[1].path, "must be sorted");
        for f in &m {
            assert!(f.size > 0);
            assert_eq!(f.sha_hex.len(), 64, "sha256 hex expected");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn diff_reports_created_changed_deleted() {
        let dir = tmp_root("diff");
        std::fs::write(dir.join("stays.txt"), b"same").unwrap();
        std::fs::write(dir.join("edited.txt"), b"before").unwrap();
        std::fs::write(dir.join("doomed.txt"), b"bye").unwrap();
        let before = scan_tree(dir.to_str().unwrap());

        std::fs::write(dir.join("edited.txt"), b"after!").unwrap();
        std::fs::remove_file(dir.join("doomed.txt")).unwrap();
        std::fs::write(dir.join("fresh.txt"), b"new").unwrap();
        let after = scan_tree(dir.to_str().unwrap());

        let changes = diff(&before, &after);
        let ops: Vec<(String, String)> = changes
            .iter()
            .map(|(p, o)| {
                let name = std::path::Path::new(p)
                    .file_name().unwrap().to_string_lossy().to_string();
                (name, o.clone())
            })
            .collect();
        assert!(ops.contains(&("edited.txt".into(), "changed-while-offline".into())), "{:?}", ops);
        assert!(ops.contains(&("doomed.txt".into(), "deleted-while-offline".into())), "{:?}", ops);
        assert!(ops.contains(&("fresh.txt".into(), "created-while-offline".into())), "{:?}", ops);
        assert_eq!(ops.len(), 3, "unchanged file must not appear: {:?}", ops);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn diff_catches_timestomped_content_change() {
        // Same size, same (recorded) mtime, different content → SHA catches it.
        let a = vec![FileState {
            path: "/x".into(), size: 4, mtime: 100, sha_hex: "aa".into(),
        }];
        let b = vec![FileState {
            path: "/x".into(), size: 4, mtime: 100, sha_hex: "bb".into(),
        }];
        assert_eq!(diff(&a, &b), vec![("/x".to_string(), "changed-while-offline".to_string())]);
    }

    #[test]
    fn identical_manifests_diff_empty() {
        let a = vec![FileState {
            path: "/x".into(), size: 1, mtime: 1, sha_hex: "cc".into(),
        }];
        assert!(diff(&a, &a).is_empty());
    }
}
