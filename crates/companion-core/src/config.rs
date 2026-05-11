//! Top-level companion configuration.
//!
//! Loaded from `companion.toml`. Section ownership:
//! - `[zeroclaw]`     — connection to the upstream agent daemon. Despite
//!   the name (kept for back-compat), the companion can drive zeroclaw,
//!   openclaw, or hermes-agent here — pick via `kind`.
//! - `[server]`       — companion's own HTTP/WS bind
//! - `[avatar]`       — Live2D avatar subsystem (companion-avatar consumes)
//! - `[avatar.tts]`   — TTS port + language config
//! - `[avatar.subagent]` — expression / translation LLM
//! - `[pulse]`        — Pulse dashboard (companion-pulse consumes)

use std::path::Path;

use serde::{Deserialize, Serialize};

/// Which upstream agent daemon the companion is driving.
///
/// All three are members of the pi-agent-family (zeroclaw is a Rust
/// fork, openclaw is the Node fork, hermes-agent is the Python fork
/// from Nous Research), but they expose *different* HTTP surfaces:
///
/// | kind     | chat endpoint                  | request shape                                        | response shape                              |
/// |----------|--------------------------------|------------------------------------------------------|---------------------------------------------|
/// | Zeroclaw | `POST /webhook`                | `{"message": "..."}`                                 | `{"model","response"}`                      |
/// | Openclaw | `POST /v1/chat/completions`    | `{"model":"openclaw","messages":[{...}]}` (OpenAI)   | OpenAI completion (`choices[0].message`)    |
/// | Hermes   | `POST /webhook`                | `{"message": "..."}` *(via the bridge — see README)* | `{"model","response"}`                      |
/// | Custom   | `POST /webhook`                | same as Zeroclaw                                     | same as Zeroclaw                            |
///
/// Hermes is reached through a small Python HTTP shim (`hermes-bridge.py`)
/// that shells out to `hermes -z "<msg>"` because hermes-agent itself
/// has no synchronous HTTP chat endpoint. The bridge mirrors zeroclaw's
/// `/webhook` shape so this code can treat them the same.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentKind {
    Zeroclaw,
    Openclaw,
    Hermes,
    Custom,
}

impl Default for AgentKind {
    fn default() -> Self {
        Self::Zeroclaw
    }
}

impl AgentKind {
    /// Default port each agent's gateway binds to. Used by the Settings
    /// UI to prefill the URL when the user picks a kind, and by the
    /// LAN-probe helper to know which ports are worth trying.
    pub fn default_port(self) -> u16 {
        match self {
            AgentKind::Zeroclaw | AgentKind::Custom => 42617,
            AgentKind::Openclaw => 18790,
            AgentKind::Hermes => 18791,
        }
    }
    /// Human-friendly label for log lines and error messages.
    pub fn label(self) -> &'static str {
        match self {
            AgentKind::Zeroclaw => "zeroclaw",
            AgentKind::Openclaw => "openclaw",
            AgentKind::Hermes => "hermes",
            AgentKind::Custom => "custom",
        }
    }
    /// Parse the lowercase string form (matches the serde rename).
    /// Unknown values fall through to `Zeroclaw` — the safe default.
    pub fn from_str_lossy(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "openclaw" => AgentKind::Openclaw,
            "hermes" => AgentKind::Hermes,
            "custom" => AgentKind::Custom,
            _ => AgentKind::Zeroclaw,
        }
    }
}

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
    /// Optional override for `[avatar]` top-level knobs (enabled,
    /// chat_language, tts.language, tts.speed). Saved by Settings → Avatar.
    #[serde(default)]
    pub avatar: Option<AvatarOverride>,
    /// Optional override for `[avatar.subagent]` knobs.
    #[serde(default)]
    pub subagent: Option<SubagentOverride>,
    /// Optional override for `[zeroclaw]` connection (url, pair token,
    /// timeout). Lets the user point the companion at a zeroclaw on
    /// another machine — a home server, a Raspberry Pi, a laptop on
    /// the LAN — without editing companion.toml. The companion never
    /// gives zeroclaw access to the machine it runs on; it's a thin
    /// client (avatar + TTS + chat UI) that POSTs chat to zeroclaw's
    /// `/webhook` and renders the reply.
    #[serde(default)]
    pub zeroclaw: Option<ZeroclawOverride>,
}

/// Upstream agent connection overrides. `Some` replaces the companion.toml
/// value; `None` leaves it. Changing any of these needs a companion-
/// server restart — the `ZeroclawClient` is built once at startup.
///
/// Despite the type name (kept for back-compat with the runtime.json
/// schema), this also covers openclaw and hermes via `kind`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ZeroclawOverride {
    /// Which agent flavor sits at `url`. Drives both the HTTP shape used
    /// for chat and which default port the UI prefills. `None` leaves
    /// the value from companion.toml in place.
    #[serde(default)]
    pub kind: Option<AgentKind>,
    /// Base URL of the agent HTTP gateway, e.g.
    /// `http://192.168.1.50:42617` for a LAN box.
    #[serde(default)]
    pub url: Option<String>,
    /// Pairing/bearer token, if the deployment requires one (zeroclaw
    /// with pairing on; openclaw requires it when binding to LAN).
    #[serde(default)]
    pub pair_token: Option<String>,
    /// Per-request timeout for the chat call, in seconds.
    #[serde(default)]
    pub timeout_secs: Option<u64>,
}

/// Avatar/TTS knobs the user flips frequently. Anything `Some` replaces
/// the value parsed from companion.toml; `None` leaves the TOML value
/// in place. We intentionally keep this set small — settings that need
/// a process restart (TTS engine change, launch_command, model_path)
/// stay in companion.toml so they don't appear flippable in the UI.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AvatarOverride {
    /// Master toggle for the avatar subsystem.
    #[serde(default)]
    pub enabled: Option<bool>,
    /// Language code the user types in (e.g. "en", "ja"). Drives the
    /// translation subagent.
    #[serde(default)]
    pub chat_language: Option<String>,
    /// Subagent toggle (kept under avatar to mirror the TOML hierarchy).
    #[serde(default)]
    pub subagent_enabled: Option<bool>,
    /// If true, skip the subagent when chat_language matches tts_language.
    #[serde(default)]
    pub subagent_only_when_translating: Option<bool>,
    /// Stream the translation token-by-token. When true, TTS starts on
    /// the first complete sentence ~3s after the LLM begins, instead
    /// of waiting ~15-25s for the bulk JSON analyze call to finish.
    /// Trades the LLM-driven expression pick for a faster keyword fallback.
    #[serde(default)]
    pub subagent_streaming: Option<bool>,
    /// TTS speech language code.
    #[serde(default)]
    pub tts_language: Option<String>,
    /// TTS playback speed multiplier (1.0 = normal).
    #[serde(default)]
    pub tts_speed: Option<f64>,
    /// TTS engine identifier (e.g. "gpt-sovits-v4", "edge-tts").
    #[serde(default)]
    pub tts_engine: Option<String>,
    /// Full launch command for the TTS server process (e.g.
    /// `"<conda>/python.exe tools/avatar/asuna_tts_server.py"`).
    /// companion-server spawns this with `auto_start = true`.
    #[serde(default)]
    pub tts_launch_command: Option<String>,
    /// Path to the reference audio clip GPT-SoVITS uses for zero-shot
    /// voice cloning (e.g. a 3–10s sample of the target voice).
    #[serde(default)]
    pub tts_reference_audio: Option<String>,
    /// Transcript of the reference audio clip (in `tts_reference_language`).
    #[serde(default)]
    pub tts_reference_text: Option<String>,
    /// Language code of the reference clip (e.g. "ja", "en", "zh").
    #[serde(default)]
    pub tts_reference_language: Option<String>,
    /// Filesystem path to the GPT-SoVITS install root (forwarded to
    /// the wrapper as `TTS_MODEL_PATH`).
    #[serde(default)]
    pub tts_model_path: Option<String>,
    /// CUDA device index for TTS inference. 0 = first GPU; -1 = CPU.
    #[serde(default)]
    pub tts_gpu_device: Option<i32>,
    /// Voice identifier for preset-voice engines (e.g.
    /// `"ja-JP-NanamiNeural"` for edge-tts, `"JP"` for melotts).
    /// Ignored by zero-shot engines, which use the reference clip
    /// for voice characteristics instead.
    #[serde(default)]
    pub tts_voice: Option<String>,
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
    /// `avatar.*` JSON subtrees directly because companion-core stores
    /// `avatar` as a Value (so it can tolerate schema drift on
    /// nested tables like TTS engine-specific knobs).
    pub fn apply(&self, cfg: &mut CompanionConfig) {
        // Ensure avatar is an object we can mutate; both override
        // branches need this.
        if (self.avatar.is_some() || self.subagent.is_some()) && !cfg.avatar.is_object() {
            cfg.avatar = serde_json::json!({});
        }
        if let Some(ref a) = self.avatar {
            let avatar_obj = cfg.avatar.as_object_mut().unwrap();
            if let Some(v) = a.enabled {
                avatar_obj.insert("enabled".into(), serde_json::Value::Bool(v));
            }
            if let Some(ref v) = a.chat_language {
                avatar_obj.insert("chat_language".into(), serde_json::Value::String(v.clone()));
            }
            // TTS nested table (avatar.tts.*).
            let needs_tts_obj = a.tts_language.is_some()
                || a.tts_speed.is_some()
                || a.tts_engine.is_some()
                || a.tts_launch_command.is_some()
                || a.tts_reference_audio.is_some()
                || a.tts_reference_text.is_some()
                || a.tts_reference_language.is_some()
                || a.tts_model_path.is_some()
                || a.tts_gpu_device.is_some()
                || a.tts_voice.is_some();
            if needs_tts_obj {
                let tts = avatar_obj.entry("tts").or_insert_with(|| serde_json::json!({}));
                if !tts.is_object() { *tts = serde_json::json!({}); }
                let tts_obj = tts.as_object_mut().unwrap();
                if let Some(ref v) = a.tts_language {
                    tts_obj.insert("language".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(v) = a.tts_speed {
                    if let Some(n) = serde_json::Number::from_f64(v) {
                        tts_obj.insert("speed".into(), serde_json::Value::Number(n));
                    }
                }
                if let Some(ref v) = a.tts_engine {
                    tts_obj.insert("engine".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = a.tts_launch_command {
                    tts_obj.insert("launch_command".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = a.tts_reference_audio {
                    tts_obj.insert("reference_audio".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = a.tts_reference_text {
                    tts_obj.insert("reference_text".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = a.tts_reference_language {
                    tts_obj.insert("reference_language".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(ref v) = a.tts_model_path {
                    tts_obj.insert("model_path".into(), serde_json::Value::String(v.clone()));
                }
                if let Some(v) = a.tts_gpu_device {
                    tts_obj.insert("gpu_device".into(), serde_json::Value::Number(v.into()));
                }
                if let Some(ref v) = a.tts_voice {
                    tts_obj.insert("voice".into(), serde_json::Value::String(v.clone()));
                }
            }
            // Subagent toggles (avatar.subagent.{enabled,only_when_translating}).
            if a.subagent_enabled.is_some()
                || a.subagent_only_when_translating.is_some()
                || a.subagent_streaming.is_some()
            {
                let sub = avatar_obj.entry("subagent").or_insert_with(|| serde_json::json!({}));
                if !sub.is_object() { *sub = serde_json::json!({}); }
                let sub_obj = sub.as_object_mut().unwrap();
                if let Some(v) = a.subagent_enabled {
                    sub_obj.insert("enabled".into(), serde_json::Value::Bool(v));
                }
                if let Some(v) = a.subagent_only_when_translating {
                    sub_obj.insert("only_when_translating".into(), serde_json::Value::Bool(v));
                }
                if let Some(v) = a.subagent_streaming {
                    sub_obj.insert("streaming".into(), serde_json::Value::Bool(v));
                }
            }
        }
        if let Some(ref s) = self.subagent {
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
        if let Some(ref z) = self.zeroclaw {
            // Zeroclaw is a typed struct (not a JSON Value like avatar),
            // so we patch its fields directly.
            if let Some(k) = z.kind {
                cfg.zeroclaw.kind = k;
            }
            if let Some(ref v) = z.url {
                if !v.trim().is_empty() {
                    cfg.zeroclaw.url = v.trim().trim_end_matches('/').to_string();
                }
            }
            if let Some(ref v) = z.pair_token {
                cfg.zeroclaw.pair_token = if v.is_empty() { None } else { Some(v.clone()) };
            }
            if let Some(v) = z.timeout_secs {
                cfg.zeroclaw.timeout_secs = v;
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

/// Connection to the upstream agent daemon (zeroclaw / openclaw / hermes).
///
/// Type name kept as `ZeroclawConfig` for back-compat — the actual kind
/// is selected by the `kind` field.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ZeroclawConfig {
    /// Which agent flavor `url` points at. Default `zeroclaw`.
    #[serde(default)]
    pub kind: AgentKind,
    /// Base URL of the agent HTTP gateway. Default `http://127.0.0.1:42617`
    /// (zeroclaw's default port).
    #[serde(default = "default_zeroclaw_url")]
    pub url: String,
    /// Optional pairing/bearer token for authenticated deployments.
    /// Required for openclaw when binding to LAN; optional for zeroclaw.
    #[serde(default)]
    pub pair_token: Option<String>,
    /// HTTP timeout in seconds for the chat call.
    ///
    /// Default 300s — enough headroom for the agent's full tool-use loop
    /// (web search, browser tool, cron schedule, shell). Smaller values
    /// return 502 from companion's /api/chat when the agent runs longer
    /// than the budget.
    #[serde(default = "default_zeroclaw_timeout")]
    pub timeout_secs: u64,
}

fn default_zeroclaw_url() -> String {
    "http://127.0.0.1:42617".into()
}

fn default_zeroclaw_timeout() -> u64 {
    300
}

impl Default for ZeroclawConfig {
    fn default() -> Self {
        Self {
            kind: AgentKind::default(),
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
        assert_eq!(cfg.zeroclaw.url, "http://127.0.0.1:42617");
        assert_eq!(cfg.zeroclaw.kind, AgentKind::Zeroclaw);
        assert_eq!(cfg.server.port, 9181);
    }

    #[test]
    fn parses_agent_kind() {
        let toml = r#"
            [zeroclaw]
            kind = "openclaw"
            url = "http://192.168.1.100:18790"
            pair_token = "abc"
        "#;
        let cfg: CompanionConfig = toml::from_str(toml).unwrap();
        assert_eq!(cfg.zeroclaw.kind, AgentKind::Openclaw);
        assert_eq!(cfg.zeroclaw.pair_token.as_deref(), Some("abc"));
    }

    #[test]
    fn agent_kind_default_ports() {
        assert_eq!(AgentKind::Zeroclaw.default_port(), 42617);
        assert_eq!(AgentKind::Openclaw.default_port(), 18790);
        assert_eq!(AgentKind::Hermes.default_port(), 18791);
        assert_eq!(AgentKind::Custom.default_port(), 42617);
    }

    #[test]
    fn override_patches_kind() {
        let mut cfg = CompanionConfig::default();
        let over = RuntimeOverride {
            zeroclaw: Some(ZeroclawOverride {
                kind: Some(AgentKind::Hermes),
                url: Some("http://10.0.0.5:18791".into()),
                pair_token: None,
                timeout_secs: Some(60),
            }),
            ..Default::default()
        };
        over.apply(&mut cfg);
        assert_eq!(cfg.zeroclaw.kind, AgentKind::Hermes);
        assert_eq!(cfg.zeroclaw.url, "http://10.0.0.5:18791");
        assert_eq!(cfg.zeroclaw.timeout_secs, 60);
    }
}
