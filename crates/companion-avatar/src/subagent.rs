//! Avatar subagent: a cheap LLM call that produces structured Live2D
//! control signals from each agent reply, and (when `chat_language ≠
//! tts_language`) translates that reply into the TTS speech language.
//!
//! On failure or timeout, returns `None` and the caller falls back to
//! keyword-based expression detection + the original (un-translated)
//! text — the avatar still animates and speaks something.

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::config::AvatarSubagentConfig;
use crate::expression::Live2DExpression;
use companion_core::llm::{ChatMessage, LlmClient, Role};
use companion_core::zeroclaw::ZeroclawClient;

/// LLM backend the subagent calls. Two impls ship:
/// - [`LlmClient`] (companion-core): direct OpenAI-compatible.
/// - [`ZeroclawWebhookBackend`]: round-trips through upstream zeroclaw,
///   reusing whatever provider/key zeroclaw is configured with.
#[async_trait]
pub trait SubagentBackend: Send + Sync {
    async fn ask(&self, system_prompt: &str, user_msg: &str) -> Result<String>;
}

#[async_trait]
impl SubagentBackend for LlmClient {
    async fn ask(&self, system_prompt: &str, user_msg: &str) -> Result<String> {
        let messages = vec![
            ChatMessage {
                role: Role::System,
                content: system_prompt.to_string(),
            },
            ChatMessage {
                role: Role::User,
                content: user_msg.to_string(),
            },
        ];
        self.chat(&messages).await
    }
}

/// Subagent backend that calls upstream zeroclaw via `POST /webhook`.
/// We pack the system prompt into the user message because zeroclaw's
/// webhook is a single-shot agent-loop trigger — no role separation.
pub struct ZeroclawWebhookBackend {
    client: ZeroclawClient,
}

impl ZeroclawWebhookBackend {
    pub fn new(client: ZeroclawClient) -> Self {
        Self { client }
    }
}

#[async_trait]
impl SubagentBackend for ZeroclawWebhookBackend {
    async fn ask(&self, system_prompt: &str, user_msg: &str) -> Result<String> {
        let combined = format!(
            "{system_prompt}\n\n--- begin reply to analyze ---\n{user_msg}\n--- end reply ---",
        );
        self.client.send_chat(&combined).await
    }
}

/// System prompt template. `{chat_lang}` and `{tts_lang}` are substituted
/// at call time so the subagent knows whether to translate.
const DEFAULT_SYSTEM_PROMPT: &str = r#"You are the voice director for an anime avatar (Yuuki Asuna from SAO).
You receive the raw output text from a chat agent and produce both the
clean version users see in the chat bubble AND the natural-sounding
version the avatar speaks out loud.

Inputs:
- The agent's raw output (in language: {chat_lang}). It MAY contain
  thinking-trace preamble like "The user said …", "Let me check …",
  "Looking at the context …", scratchpad notes, or memory bookkeeping
  (e.g. references to "webhook_msg_…", "Let me store this as …",
  "Let me respond naturally."). Anything before the actual reply to
  the user is preamble that must be stripped.
- The TTS speech language: {tts_lang}.

Output ONLY a JSON object (no markdown, no commentary):
{
  "expression": "<name>",
  "intensity": <0.0-1.0>,
  "motion": {"group": "<group>", "index": <number>} or null,
  "clean_chat_text": "<reply with thinking-preamble stripped, in {chat_lang}>",
  "translated_text": "<the same reply rendered in {tts_lang}, voiced as Asuna>"
}

Rules:
- clean_chat_text:
  - Drop ALL thinking-style preamble. Keep ONLY what Asuna actually
    says to the user. The chat bubble should never show the agent's
    internal monologue.
  - Preserve emoji and formatting in the kept reply.
- expression: pick a name from the model's available expressions
  (e.g. F01..F08). Default to "F01" for neutral/factual replies.
  F05 happy/closed eyes; F04 surprised; F02 sad; F03 angry; F06 wide eyes
  (curious/amazed); F07 playful; F08 shy.
- intensity: between 0.4 and 0.9.
- motion: only when the text clearly warrants a physical reaction.
  group "Idle" (index 0|1) for idle; "TapBody" (index 0..3) for reactions.
  null for most replies.
- translated_text:
  - Translate the CLEANED reply (NOT the preamble) into {tts_lang}.
  - Voice it as Asuna would say it: warm, friendly, slightly playful,
    casual register. NOT formal/business Japanese. Use natural
    everyday phrasing — のだ/だよ/だね/かな endings where appropriate,
    light contractions, short sentences. Avoid dictionary-literal
    translation; render the MEANING the way an anime girl would say it.
  - Strip emoji and inline emotion tags before voicing.
  - Drop markdown decorations (**bold**, headers, bullet symbols) —
    speak the words, not the markup.
  - If {chat_lang} == {tts_lang}, copy clean_chat_text verbatim.
  - Do NOT add narration, stage directions, or quotation marks.
- Output ONLY the JSON object."#;

/// Structured output from the subagent LLM call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubagentAnalysis {
    pub expression: String,
    pub intensity: f32,
    pub motion: Option<SubagentMotion>,
    /// Reply rendered in the TTS speech language. Equal to the input text
    /// when chat and TTS languages match.
    #[serde(default)]
    pub translated_text: Option<String>,
    /// Reply with any thinking-style preamble stripped, in the chat
    /// language. This is what the chat bubble should display. None when
    /// the subagent skipped or the legacy prompt was used.
    #[serde(default)]
    pub clean_chat_text: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubagentMotion {
    pub group: String,
    pub index: u32,
}

/// The avatar subagent. Holds whichever backend the config selected.
pub struct AvatarSubagent {
    backend: Arc<dyn SubagentBackend>,
    timeout: Duration,
    system_prompt_template: String,
}

impl AvatarSubagent {
    /// Build with the default backend chosen from config:
    /// - `use_zeroclaw_webhook=true` → [`ZeroclawWebhookBackend`] (needs
    ///   `zeroclaw_client`).
    /// - else → direct [`LlmClient`].
    pub fn new(
        config: &AvatarSubagentConfig,
        zeroclaw_client: Option<ZeroclawClient>,
    ) -> Result<Self> {
        let backend: Arc<dyn SubagentBackend> = if config.use_zeroclaw_webhook {
            let client = zeroclaw_client
                .ok_or_else(|| anyhow::anyhow!(
                    "subagent.use_zeroclaw_webhook = true but no ZeroclawClient was supplied"
                ))?;
            Arc::new(ZeroclawWebhookBackend::new(client))
        } else {
            Arc::new(LlmClient::new(&config.llm)?)
        };
        let system_prompt_template = config
            .system_prompt
            .clone()
            .unwrap_or_else(|| DEFAULT_SYSTEM_PROMPT.to_string());
        Ok(Self {
            backend,
            timeout: Duration::from_secs(config.timeout_secs),
            system_prompt_template,
        })
    }

    /// Build with an explicit backend. Useful for tests and for callers
    /// that already have a backend instance they want to inject.
    pub fn with_backend(
        backend: Arc<dyn SubagentBackend>,
        config: &AvatarSubagentConfig,
    ) -> Self {
        let system_prompt_template = config
            .system_prompt
            .clone()
            .unwrap_or_else(|| DEFAULT_SYSTEM_PROMPT.to_string());
        Self {
            backend,
            timeout: Duration::from_secs(config.timeout_secs),
            system_prompt_template,
        }
    }

    /// Analyze a main agent reply. Returns `None` on timeout/parse failure.
    pub async fn analyze(
        &self,
        text: &str,
        chat_language: &str,
        tts_language: &str,
    ) -> Option<SubagentAnalysis> {
        let truncated = if text.len() > 2000 { &text[..2000] } else { text };

        let system_prompt = self
            .system_prompt_template
            .replace("{chat_lang}", chat_language)
            .replace("{tts_lang}", tts_language);

        tracing::info!(
            "avatar subagent: → input ({}c, chat={chat_language}, tts={tts_language}): {:?}",
            truncated.chars().count(),
            &truncated[..truncated.len().min(200)],
        );
        let result =
            tokio::time::timeout(self.timeout, self.backend.ask(&system_prompt, truncated)).await;

        match result {
            Ok(Ok(response_text)) => {
                tracing::info!(
                    "avatar subagent: ← raw LLM response ({}c): {:?}",
                    response_text.chars().count(),
                    &response_text[..response_text.len().min(500)],
                );
                let cleaned = response_text
                    .trim()
                    .trim_start_matches("```json")
                    .trim_start_matches("```")
                    .trim_end_matches("```")
                    .trim();
                match serde_json::from_str::<SubagentAnalysis>(cleaned) {
                    Ok(analysis) => {
                        tracing::info!(
                            "avatar subagent: parsed expr={} clean_chat={:?} translated={:?}",
                            analysis.expression,
                            analysis.clean_chat_text.as_ref().map(|s| {
                                let n = s.chars().count();
                                format!("{}c: {:?}", n, &s[..s.len().min(120)])
                            }),
                            analysis.translated_text.as_ref().map(|s| {
                                let n = s.chars().count();
                                format!("{}c: {:?}", n, &s[..s.len().min(120)])
                            }),
                        );
                        Some(analysis)
                    }
                    Err(e) => {
                        tracing::warn!(
                            "avatar subagent: JSON parse failed ({e}), raw: {}",
                            &cleaned[..cleaned.len().min(200)]
                        );
                        None
                    }
                }
            }
            Ok(Err(e)) => {
                tracing::warn!("avatar subagent: LLM call failed: {e}");
                None
            }
            Err(_) => {
                tracing::warn!(
                    "avatar subagent: timed out after {}s",
                    self.timeout.as_secs()
                );
                None
            }
        }
    }

    /// Convert a SubagentAnalysis into a Live2DExpression, falling back to
    /// the keyword-detection result for invalid fields.
    pub fn to_expression(
        analysis: &SubagentAnalysis,
        fallback: &Live2DExpression,
    ) -> Live2DExpression {
        Live2DExpression {
            name: if analysis.expression.is_empty() {
                fallback.name.clone()
            } else {
                analysis.expression.clone()
            },
            intensity: if analysis.intensity <= 0.0 || analysis.intensity > 1.0 {
                fallback.intensity
            } else {
                analysis.intensity
            },
            duration_ms: fallback.duration_ms,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_response_with_translation() {
        let json = r#"{
            "expression": "F05",
            "intensity": 0.7,
            "motion": null,
            "translated_text": "こんにちは"
        }"#;
        let a: SubagentAnalysis = serde_json::from_str(json).unwrap();
        assert_eq!(a.expression, "F05");
        assert_eq!(a.translated_text.as_deref(), Some("こんにちは"));
    }

    #[test]
    fn parses_legacy_response_without_translation() {
        let json = r#"{"expression":"F01","intensity":0.5,"motion":null}"#;
        let a: SubagentAnalysis = serde_json::from_str(json).unwrap();
        assert!(a.translated_text.is_none());
    }

    #[test]
    fn parses_response_with_motion() {
        let json = r#"{
            "expression": "F04",
            "intensity": 0.8,
            "motion": {"group": "TapBody", "index": 2}
        }"#;
        let a: SubagentAnalysis = serde_json::from_str(json).unwrap();
        let m = a.motion.expect("motion");
        assert_eq!(m.group, "TapBody");
        assert_eq!(m.index, 2);
    }

    #[test]
    fn to_expression_uses_subagent_fields() {
        let analysis = SubagentAnalysis {
            expression: "F03".into(),
            intensity: 0.85,
            motion: None,
            translated_text: None,
            clean_chat_text: None,
        };
        let fallback = Live2DExpression {
            name: "neutral".into(),
            intensity: 0.5,
            duration_ms: Some(1000),
        };
        let out = AvatarSubagent::to_expression(&analysis, &fallback);
        assert_eq!(out.name, "F03");
        assert!((out.intensity - 0.85).abs() < f32::EPSILON);
        // duration_ms always inherits from the keyword fallback (subagent
        // doesn't return one)
        assert_eq!(out.duration_ms, Some(1000));
    }

    #[test]
    fn to_expression_falls_back_on_invalid_intensity() {
        let analysis = SubagentAnalysis {
            expression: "F03".into(),
            intensity: -0.5,
            motion: None,
            translated_text: None,
            clean_chat_text: None,
        };
        let fallback = Live2DExpression {
            name: "neutral".into(),
            intensity: 0.5,
            duration_ms: None,
        };
        let out = AvatarSubagent::to_expression(&analysis, &fallback);
        assert!((out.intensity - 0.5).abs() < f32::EPSILON);
    }

    #[test]
    fn to_expression_falls_back_on_empty_name() {
        let analysis = SubagentAnalysis {
            expression: "".into(),
            intensity: 0.7,
            motion: None,
            translated_text: None,
            clean_chat_text: None,
        };
        let fallback = Live2DExpression {
            name: "F01".into(),
            intensity: 0.5,
            duration_ms: None,
        };
        let out = AvatarSubagent::to_expression(&analysis, &fallback);
        assert_eq!(out.name, "F01");
    }

    #[test]
    fn rejects_intensity_above_one_and_falls_back() {
        let analysis = SubagentAnalysis {
            expression: "F02".into(),
            intensity: 1.5,
            motion: None,
            translated_text: None,
            clean_chat_text: None,
        };
        let fallback = Live2DExpression {
            name: "neutral".into(),
            intensity: 0.6,
            duration_ms: None,
        };
        let out = AvatarSubagent::to_expression(&analysis, &fallback);
        assert!((out.intensity - 0.6).abs() < f32::EPSILON);
    }

    #[test]
    fn default_system_prompt_contains_substitution_markers() {
        // The template is loaded at construction time and {chat_lang} /
        // {tts_lang} are substituted at call time. Make sure the canonical
        // template has both markers.
        assert!(DEFAULT_SYSTEM_PROMPT.contains("{chat_lang}"));
        assert!(DEFAULT_SYSTEM_PROMPT.contains("{tts_lang}"));
    }
}
