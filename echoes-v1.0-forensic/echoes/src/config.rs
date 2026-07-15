//! `echoes.toml` configuration file (ADR-002 Phase 8d).
//!
//! The sensor flags outgrew themselves (`--watch`, `--procs`, `--net`,
//! `--auth`, `--remote-store`, …), so `echoes run --config echoes.toml`
//! loads them from a file instead. **CLI flags always override the file**;
//! boolean sensors are enabled by either side.
//!
//! ```toml
//! # echoes.toml — everything is optional
//! db    = "/var/lib/echoes/monitor.db"
//! name  = "Monitor"
//! goal  = "watch the fleet"
//! ticks = 50
//!
//! watch = "/var/log"            # FileWatcher path (needs --features watch)
//! procs = true                  # ProcessScanner
//! net   = true                  # NetScanner
//! auth  = true                  # AuthWatcher (auto-detect log path)
//! # auth = "/var/log/auth.log"  # …or an explicit path
//!
//! remote_store = "http://192.168.1.10:8080"
//! token        = "atk_..."      # prefer the AUTOMATON_TOKEN env var
//! ```

use serde::Deserialize;

/// Parsed `echoes.toml`. Every field is optional; `None` means "not set,
/// fall through to the CLI flag or the built-in default".
#[derive(Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub db: Option<String>,
    pub name: Option<String>,
    pub goal: Option<String>,
    pub ticks: Option<u32>,
    pub watch: Option<String>,
    pub procs: Option<bool>,
    pub net: Option<bool>,
    pub auth: Option<AuthSetting>,
    pub remote_store: Option<String>,
    pub token: Option<String>,
}

/// `auth = true` (auto-detect the log) or `auth = "/path/to/log"`.
#[derive(Debug, Deserialize, PartialEq)]
#[serde(untagged)]
pub enum AuthSetting {
    Enabled(bool),
    Path(String),
}

impl Config {
    /// Load and parse a config file. Unknown keys are an error — typos in a
    /// forensic tool's config should fail loudly, not silently disable a
    /// sensor.
    pub fn load(path: &str) -> Result<Self, String> {
        let s = std::fs::read_to_string(path)
            .map_err(|e| format!("could not read config '{}': {}", path, e))?;
        toml::from_str(&s).map_err(|e| format!("invalid config '{}': {}", path, e))
    }

    /// Normalize the auth setting into the same shape as the `--auth` CLI
    /// flag: `None` (disabled), `Some("")` (auto-detect), `Some(path)`.
    pub fn auth_flag(&self) -> Option<String> {
        match &self.auth {
            None | Some(AuthSetting::Enabled(false)) => None,
            Some(AuthSetting::Enabled(true)) => Some(String::new()),
            Some(AuthSetting::Path(p)) => Some(p.clone()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_config_parses() {
        let cfg: Config = toml::from_str(
            r#"
            db    = "/var/lib/echoes/m.db"
            name  = "Monitor"
            goal  = "watch"
            ticks = 50
            watch = "/var/log"
            procs = true
            net   = true
            auth  = "/var/log/auth.log"
            remote_store = "http://h:8080"
            token = "atk_x"
            "#,
        )
        .unwrap();
        assert_eq!(cfg.db.as_deref(), Some("/var/lib/echoes/m.db"));
        assert_eq!(cfg.ticks, Some(50));
        assert_eq!(cfg.procs, Some(true));
        assert_eq!(cfg.auth_flag().as_deref(), Some("/var/log/auth.log"));
    }

    #[test]
    fn empty_config_is_all_none() {
        let cfg: Config = toml::from_str("").unwrap();
        assert_eq!(cfg, Config::default());
        assert_eq!(cfg.auth_flag(), None);
    }

    #[test]
    fn auth_accepts_bool_or_path() {
        let t: Config = toml::from_str("auth = true").unwrap();
        assert_eq!(t.auth_flag().as_deref(), Some(""));

        let f: Config = toml::from_str("auth = false").unwrap();
        assert_eq!(f.auth_flag(), None);

        let p: Config = toml::from_str(r#"auth = "/x/y""#).unwrap();
        assert_eq!(p.auth_flag().as_deref(), Some("/x/y"));
    }

    #[test]
    fn unknown_keys_fail_loudly() {
        let r: Result<Config, _> = toml::from_str("nett = true");
        assert!(r.is_err(), "typo'd key must be rejected");
    }

    #[test]
    fn load_reports_missing_file() {
        let err = Config::load("/nonexistent/echoes.toml").unwrap_err();
        assert!(err.contains("could not read config"), "got: {}", err);
    }
}
