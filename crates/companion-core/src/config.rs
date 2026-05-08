//! Top-level companion configuration.
//!
//! Loaded from `companion.toml`. Section ownership:
//! - `[zeroclaw]`     — connection to upstream zeroclaw daemon
//! - `[server]`       — companion's own HTTP/WS bind
//! - `[avatar]`       — Live2D avatar subsystem (companion-avatar consumes)
//! - `[avatar.tts]`   — TTS port + language config
//! - `[avatar.subagent]` — expression / translation LLM
//! - `[pulse]`        — Pulse dashboard (companion-pulse consumes)

use std::path::Path;

use serde::{Deserialize, Serialize};

/// Top-level companion configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompanionConfig {
    #[serde(default)]
    pub zeroclaw: ZeroclawConfig,
    #[serde(default)]
    pub server: ServerConfig,
    /// Free-form avatar config table; companion-avatar deserializes
    /// its own typed shape. Keeping it as a Value here keeps companion-core
    /// independent of the avatar crate.
    #[serde(default)]
    pub avatar: serde_json::Value,
    /// Same pattern for pulse.
    #[serde(default)]
    pub pulse: serde_json::Value,
}

impl CompanionConfig {
    /// Load from a TOML file. If the path doesn't exist, returns defaults.
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        if !path.exists() {
            tracing::info!("companion.toml not found at {}; using defaults", path.display());
            return Ok(Self::default());
        }
        let body = std::fs::read_to_string(path)?;
        let cfg: CompanionConfig = toml::from_str(&body)?;
        Ok(cfg)
    }
}

impl Default for CompanionConfig {
    fn default() -> Self {
        Self {
            zeroclaw: ZeroclawConfig::default(),
            server: ServerConfig::default(),
            avatar: serde_json::json!({}),
            pulse: serde_json::json!({}),
        }
    }
}

/// Connection to the upstream zeroclaw daemon.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ZeroclawConfig {
    /// Base URL of the zeroclaw HTTP gateway. Default `http://127.0.0.1:8080`.
    #[serde(default = "default_zeroclaw_url")]
    pub url: String,
    /// Optional pairing token for authenticated zeroclaw deployments.
    #[serde(default)]
    pub pair_token: Option<String>,
    /// HTTP timeout in seconds for `POST /webhook` chat calls.
    ///
    /// Default 300s — enough headroom for zeroclaw's full agent loop
    /// including tool-use rounds. Common queries that need this:
    /// - "tell me news about X" (multi-step web_search)
    /// - "browse this URL" (browser tool)
    /// - any cron schedule / shell command path
    /// Smaller values will return 502 from companion's /api/chat when
    /// the agent runs longer than the budget.
    #[serde(default = "default_zeroclaw_timeout")]
    pub timeout_secs: u64,
}

fn default_zeroclaw_url() -> String {
    "http://127.0.0.1:8080".into()
}

fn default_zeroclaw_timeout() -> u64 {
    300
}

impl Default for ZeroclawConfig {
    fn default() -> Self {
        Self {
            url: default_zeroclaw_url(),
            pair_token: None,
            timeout_secs: default_zeroclaw_timeout(),
        }
    }
}

/// Companion's own HTTP/WS server bind.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerConfig {
    #[serde(default = "default_server_host")]
    pub host: String,
    #[serde(default = "default_server_port")]
    pub port: u16,
    /// Path on disk to serve the companion web bundle from. Falls back to
    /// `./web/dist` relative to the binary.
    #[serde(default)]
    pub web_dist_dir: Option<String>,
}

fn default_server_host() -> String {
    "127.0.0.1".into()
}

fn default_server_port() -> u16 {
    9181
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            host: default_server_host(),
            port: default_server_port(),
            web_dist_dir: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_toml() {
        let toml = r#"
            [zeroclaw]
            url = "http://127.0.0.1:9090"

            [server]
            port = 9000
        "#;
        let cfg: CompanionConfig = toml::from_str(toml).unwrap();
        assert_eq!(cfg.zeroclaw.url, "http://127.0.0.1:9090");
        assert_eq!(cfg.server.port, 9000);
    }

    #[test]
    fn defaults_apply_when_omitted() {
        let cfg: CompanionConfig = toml::from_str("").unwrap();
        assert_eq!(cfg.zeroclaw.url, "http://127.0.0.1:8080");
        assert_eq!(cfg.server.port, 9181);
    }
}
