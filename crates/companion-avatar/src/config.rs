//! Avatar config types — owned by companion-avatar, deserialized from
//! the `[avatar]` table in `companion.toml`.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use companion_core::llm::LlmConfig;

/// Top-level avatar configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvatarConfig {
    #[serde(default)]
    pub enabled: bool,
    /// Language the user chats with the agent in. Subtitles use this language;
    /// when it differs from `tts.language` the subagent translates each reply
    /// before TTS synthesis.
    #[serde(default = "default_chat_language")]
    pub chat_language: String,
    #[serde(default)]
    pub tts: AvatarTtsConfig,
    #[serde(default)]
    pub model: Live2DModelConfig,
    #[serde(default)]
    pub expressions: ExpressionMappingConfig,
    #[serde(default)]
    pub lip_sync: LipSyncConfig,
    #[serde(default)]
    pub subagent: AvatarSubagentConfig,
}

impl Default for AvatarConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            chat_language: default_chat_language(),
            tts: AvatarTtsConfig::default(),
            model: Live2DModelConfig::default(),
            expressions: ExpressionMappingConfig::default(),
            lip_sync: LipSyncConfig::default(),
            subagent: AvatarSubagentConfig::default(),
        }
    }
}

fn default_chat_language() -> String {
    "en".into()
}

/// TTS port configuration.
///
/// The companion speaks a single, model-agnostic HTTP contract:
///
/// - `POST {api_url}/tts` JSON: `{"text", "language", "voice"?, "speed"?}`
///   → audio bytes (optional `X-Sample-Rate`, `X-Channels`, `X-Format`).
/// - `GET {api_url}/health` → 200 OK when ready.
///
/// Engine-specific knobs are forwarded to the spawned wrapper as env vars
/// (`TTS_MODEL_PATH`, `TTS_REFERENCE_AUDIO`, `TTS_REFERENCE_TEXT`,
/// `TTS_REFERENCE_LANG`, `TTS_VOICE`, `CUDA_VISIBLE_DEVICES`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvatarTtsConfig {
    #[serde(default = "default_tts_engine")]
    pub engine: String,
    #[serde(default)]
    pub api_url: Option<String>,
    #[serde(default)]
    pub model_path: Option<String>,
    #[serde(default)]
    pub reference_audio: Option<String>,
    #[serde(default)]
    pub reference_text: Option<String>,
    #[serde(default)]
    pub reference_language: Option<String>,
    #[serde(default = "default_gpu_device")]
    pub gpu_device: i32,
    #[serde(default)]
    pub port: u16,
    #[serde(default)]
    pub launch_command: Option<String>,
    #[serde(default = "default_true")]
    pub auto_start: bool,
    #[serde(default)]
    pub voice: Option<String>,
    #[serde(default = "default_tts_language")]
    pub language: String,
    #[serde(default = "default_tts_speed")]
    pub speed: f32,
}

fn default_tts_engine() -> String {
    "edge-tts".into()
}
fn default_gpu_device() -> i32 {
    0
}
fn default_tts_language() -> String {
    "en".into()
}
fn default_tts_speed() -> f32 {
    1.0
}
fn default_true() -> bool {
    true
}

impl Default for AvatarTtsConfig {
    fn default() -> Self {
        Self {
            engine: default_tts_engine(),
            api_url: None,
            model_path: None,
            reference_audio: None,
            reference_text: None,
            reference_language: None,
            gpu_device: default_gpu_device(),
            port: 9880,
            launch_command: None,
            auto_start: true,
            voice: None,
            language: default_tts_language(),
            speed: default_tts_speed(),
        }
    }
}

/// Live2D model configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Live2DModelConfig {
    #[serde(default)]
    pub model_dir: Option<String>,
    #[serde(default = "default_avatar_expression")]
    pub default_expression: String,
    #[serde(default = "default_model_scale")]
    pub scale: f32,
    #[serde(default = "default_model_anchor")]
    pub anchor: String,
}

fn default_avatar_expression() -> String {
    "neutral".into()
}
fn default_model_scale() -> f32 {
    0.2
}
fn default_model_anchor() -> String {
    "center".into()
}

impl Default for Live2DModelConfig {
    fn default() -> Self {
        Self {
            model_dir: None,
            default_expression: default_avatar_expression(),
            scale: default_model_scale(),
            anchor: default_model_anchor(),
        }
    }
}

/// Expression mapping from agent emotions to Live2D expressions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExpressionMappingConfig {
    #[serde(default)]
    pub mapping: HashMap<String, String>,
    #[serde(default = "default_avatar_expression")]
    pub default: String,
    #[serde(default = "default_emotion_detection")]
    pub detection_mode: String,
    #[serde(default)]
    pub keyword_map: HashMap<String, String>,
}

fn default_emotion_detection() -> String {
    "keyword".into()
}

impl Default for ExpressionMappingConfig {
    fn default() -> Self {
        Self {
            mapping: HashMap::from([
                ("happy".to_string(), "smile".to_string()),
                ("sad".to_string(), "depressed".to_string()),
                ("angry".to_string(), "angry".to_string()),
                ("surprised".to_string(), "surprised".to_string()),
            ]),
            default: default_avatar_expression(),
            detection_mode: default_emotion_detection(),
            keyword_map: HashMap::from([
                ("happy".to_string(), "happy".to_string()),
                ("glad".to_string(), "happy".to_string()),
                ("sad".to_string(), "sad".to_string()),
                ("sorry".to_string(), "sad".to_string()),
                ("angry".to_string(), "angry".to_string()),
                ("wow".to_string(), "surprised".to_string()),
                ("surprised".to_string(), "surprised".to_string()),
            ]),
        }
    }
}

/// Lip sync configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LipSyncConfig {
    #[serde(default = "default_lip_sync_method")]
    pub method: String,
    #[serde(default = "default_lip_sync_smoothing")]
    pub smoothing: f32,
    #[serde(default = "default_mouth_open_param")]
    pub mouth_open_param: String,
    #[serde(default = "default_mouth_smile_param")]
    pub mouth_smile_param: String,
    #[serde(default = "default_lip_sync_fps")]
    pub fps: u32,
}

fn default_lip_sync_method() -> String {
    "volume".into()
}
fn default_lip_sync_smoothing() -> f32 {
    0.3
}
fn default_mouth_open_param() -> String {
    "ParamMouthOpenY".into()
}
fn default_mouth_smile_param() -> String {
    "ParamMouthSmile".into()
}
fn default_lip_sync_fps() -> u32 {
    30
}

impl Default for LipSyncConfig {
    fn default() -> Self {
        Self {
            method: default_lip_sync_method(),
            smoothing: default_lip_sync_smoothing(),
            mouth_open_param: default_mouth_open_param(),
            mouth_smile_param: default_mouth_smile_param(),
            fps: default_lip_sync_fps(),
        }
    }
}

/// Avatar subagent: a cheap LLM call that emits expression JSON and (when
/// `chat_language ≠ tts.language`) a translated reply.
///
/// Two backends:
/// - `llm` (default): direct OpenAI-compatible call. Fastest. Requires
///   a plaintext API key in this config (or via env var).
/// - `use_zeroclaw_webhook = true`: re-uses upstream zeroclaw as the LLM
///   by POSTing to its `/webhook`. No plaintext key needed in companion —
///   zeroclaw already has its keys decrypted. Slower (each agent reply
///   triggers a second zeroclaw round trip), but very simple to set up.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvatarSubagentConfig {
    #[serde(default)]
    pub enabled: bool,
    /// When `true`, route subagent calls through the configured zeroclaw
    /// daemon (via `[zeroclaw] url`) instead of a direct LLM endpoint.
    /// Reuses zeroclaw's keys; no plaintext key needed below.
    #[serde(default)]
    pub use_zeroclaw_webhook: bool,
    /// LLM endpoint + model. Use any OpenAI-compatible provider
    /// (OpenAI, OpenRouter, Together, Groq, Ollama, vLLM, …). Ignored
    /// when `use_zeroclaw_webhook = true`.
    #[serde(default)]
    pub llm: LlmConfig,
    /// Custom system prompt override (replaces the built-in default).
    /// Supports `{chat_lang}` / `{tts_lang}` placeholders.
    #[serde(default)]
    pub system_prompt: Option<String>,
    /// Per-call timeout in seconds.
    #[serde(default = "default_subagent_timeout")]
    pub timeout_secs: u64,
}

fn default_subagent_timeout() -> u64 {
    3
}

impl Default for AvatarSubagentConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            use_zeroclaw_webhook: false,
            llm: LlmConfig::default(),
            system_prompt: None,
            timeout_secs: default_subagent_timeout(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_avatar_toml() {
        let toml = r#"
            enabled = true
            chat_language = "en"
            [tts]
            language = "ja"
            engine = "gpt-sovits-v4"
        "#;
        let cfg: AvatarConfig = toml::from_str(toml).unwrap();
        assert!(cfg.enabled);
        assert_eq!(cfg.chat_language, "en");
        assert_eq!(cfg.tts.language, "ja");
        assert_eq!(cfg.tts.engine, "gpt-sovits-v4");
    }
}
