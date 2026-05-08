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
    /// Also merges `companion.runtime.json` (sibling file, if present) over
    /// the loaded TOML — that's where per-machine UI overrides live.
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let mut cfg = if !path.exists() {
            tracing::info!("companion.toml not found at {}; using defaults", path.display());
            Self::default()
        } else {
            let body = std::fs::read_to_string(path)?;
            toml::from_str(&body)?
        };

        // Per-machine runtime overrides (UI-driven). Sits next to the TOML
        // so users see both files together. JSON because the UI saves it
        // through serde_json — no need to round-trip TOML formatting.
        let runtime_path = runtime_override_path(path);
        if runtime_path.exists() {
            match std::fs::read_to_string(&runtime_path) {
                Ok(body) => match serde_json::from_str::<RuntimeOverride>(&body) {
                    Ok(over) => {
                        over.apply(&mut cfg);
                        tracing::info!(
                            "companion: applied runtime override from {}",
                            runtime_path.display()
                        );
                    }
                    Err(e) => tracing::warn!(
                        "companion: runtime override at {} failed to parse: {e}",
                        runtime_path.display()
                    ),
                },
                Err(e) => tracing::warn!(
                    "companion: runtime override at {} unreadable: {e}",
                    runtime_path.display()
                ),
            }
        }
        Ok(cfg)
    }
}

/// Where the runtime override file lives relative to `companion.toml`.
/// Always `<config-dir>/companion.runtime.json`.
pub fn runtime_override_path(toml_path: &Path) -> std::path::PathBuf {
    let dir = toml_path.parent().unwrap_or_else(|| Path::new("."));
    dir.join("companion.runtime.json")
}

/// Per-machine runtime overrides. Saved by the UI's settings page,
/// merged over `companion.toml` on startup. Keep this small — every
/// field here is something the user can flip without editing TOML.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RuntimeOverride {
    /// Optional override for `[avatar.subagent]` knobs.
    #[serde(default)]
    pub subagent: Option<SubagentOverride>,
}

/// Subagent backend + LLM connection overrides. Anything `Some` replaces
/// the value parsed from companion.toml; `None` leaves the TOML value
/// in place.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SubagentOverride {
    /// `true` → route through zeroclaw's webhook (slow, no key needed).
    /// `false` → direct LLM call (fast, needs api_key or api_key_env).
    #[serde(default)]
    pub use_zeroclaw_webhook: Option<bool>,
    /// Direct-LLM API key, stored verbatim. Takes precedence over
    /// `api_key_env` if set.
    #[serde(default)]
    pub api_key: Option<String>,
    /// Direct-LLM model name (e.g. "glm-4.5-flash", "gpt-4o-mini").
    #[serde(default)]
    pub model: Option<String>,
    /// Direct-LLM base URL (e.g. "https://api.z.ai/api/coding/paas/v4").
    #[serde(default)]
    pub base_url: Option<String>,
    /// Subagent timeout in seconds (covers the whole LLM call).
    #[serde(default)]
    pub timeout_secs: Option<u64>,
}

impl RuntimeOverride {
    /// Merge this override into a loaded config. We patch the
    /// `avatar.subagent` and `avatar.subagent.llm` JSON subtrees
    /// directly because companion-core stores `avatar` as a Value.
    pub fn apply(&self, cfg: &mut CompanionConfig) {
        if let Some(ref s) = self.subagent {
            // Ensure avatar is an object we can mutate.
            if !cfg.avatar.is_object() {
                cfg.avatar = serde_json::json!({});
            }
            let avatar_obj = cfg.avatar.as_object_mut().unwrap();
            let subagent = avatar_obj
                .entry("subagent")
                .or_insert_with(|| serde_json::json!({}));
            if !subagent.is_object() {
                *subagent = serde_json::json!({});
            }
            let sub_obj = subagent.as_object_mut().unwrap();
            if let Some(v) = s.use_zeroclaw_webhook {
                sub_obj.insert("use_zeroclaw_webhook".into(), serde_json::Value::Bool(v));
            }
            if let Some(v) = s.timeout_secs {
                sub_obj.insert(
                    "timeout_secs".into(),
                    serde_json::Value::Number(v.into()),
                );
            }
            // LLM nested table.
            if s.api_key.is_some() || s.model.is_some() || s.base_url.is_some() {
                let llm = sub_obj
                    .entry("llm")
                    .or_insert_with(|| serde_json::json!({}));
                if !llm.is_object() {
                    *llm = serde_json::json!({});
                }
                let llm_obj = llm.as_object_mut().unwrap();
                if let Some(ref v) = s.api_key {
                    llm_obj.insert("api_key".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = s.model {
                    llm_obj.insert("model".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = s.base_url {
                    llm_obj.insert("base_url".into(), serde_json::Value::String(v.clone()));
                }
            }
        }
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
