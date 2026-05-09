//! Avatar subagent: a cheap LLM call that produces structured Live2D
//! control signals from each agent reply, and (when `chat_language ≠
//! tts_language`) translates that reply into the TTS speech language.
//!
//! On failure or timeout, returns `None` and the caller falls back to
//! keyword-based expression detection + the original (un-translated)
//! text — the avatar still animates and speaks something.

use std::sync::Arc;
use std::time::Duration;

/// Char-boundary-safe slice for diagnostic logging. `s[..s.len().min(n)]`
/// panics on Windows when the byte at position n is mid-codepoint —
/// emoji and CJK in agent replies trip this constantly. This walks
/// back to the nearest char boundary.
fn safe_prefix(s: &str, max_bytes: usize) -> &str {
    let mut end = s.len().min(max_bytes);
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Strip surrounding ```...``` or ```json...``` fences that some
/// instruction-following models can't help wrapping their output in.
fn strip_code_fence(s: &str) -> &str {
    s.trim()
        .trim_start_matches("```json")
        .trim_start_matches("```")
        .trim_end_matches("```")
        .trim()
}

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
///
/// **Scope.** The subagent is a narrow utility: clean the text, pick an
/// expression, translate. It does NOT inject personality or rewrite tone.
/// The main chat agent owns voice and persona; the subagent is plumbing.
/// (User feedback 2026-05-08: persona-rewriting at this layer caused
/// emphasis-duplication artifacts and drift away from what the user
/// reads in the chat bubble.)
const DEFAULT_SYSTEM_PROMPT: &str = r#"You are a narrow utility that processes a chat agent's raw output for
display + TTS. You do TWO things only: clean the text, and translate
it. You do NOT add personality, do NOT rewrite tone, do NOT inject
emphasis. The main agent handles persona; you handle plumbing.

Inputs:
- The agent's raw output (in language: {chat_lang}). It MAY contain
  thinking-trace preamble like "The user said …", "Let me check …",
  "Looking at the context …", "Let me store this as …",
  "Let me respond naturally.", or references to "webhook_msg_…".
- The TTS speech language: {tts_lang}.

Output ONLY a JSON object (no markdown, no commentary):
{
  "expression": "<name>",
  "intensity": <0.0-1.0>,
  "motion": {"group": "<group>", "index": <number>} or null,
  "clean_chat_text": "<reply with thinking-preamble stripped, in {chat_lang}>",
  "translated_text": "<the same reply rendered in {tts_lang}>"
}

Rules:
- clean_chat_text:
  - Strip thinking-style preamble. Keep ONLY what the agent says to
    the user.
  - Preserve everything else verbatim — wording, tone, emoji, line
    breaks, markdown. Do NOT paraphrase. Do NOT shorten.
- expression: pick a name from the model's available expressions
  (callers may specialize this list via a custom system_prompt). For
  the default avatar use F01..F08: F01 neutral; F02 sad; F03 angry;
  F04 surprised; F05 happy; F06 amazed; F07 playful; F08 shy.
- intensity: between 0.4 and 0.9.
- motion: only when the text clearly warrants a physical reaction.
  group "Idle" (index 0|1) for idle; "TapBody" (index 0..3) for
  reactions. null for most replies.
- translated_text:
  - Translate clean_chat_text into {tts_lang} faithfully — same
    meaning, same tone, same sentence count. The reader's chat bubble
    and the TTS playback must align.
  - Do NOT add emphasis duplicates (no extra "ありがとう" for an
    English text that says "thank you" once).
  - Strip emoji and markdown decorations (**bold**, headers, bullet
    symbols) before voicing — the TTS speaks the words, not the markup.
  - If {chat_lang} == {tts_lang}, copy clean_chat_text verbatim with
    markdown stripped.
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
    /// Retries once on empty/parse failure — these are usually transient
    /// (z.ai sometimes returns empty bodies under load) and a redo lands
    /// before the user notices, where falling all the way back to "speak
    /// raw English with Japanese TTS" is audibly broken.
    pub async fn analyze(
        &self,
        text: &str,
        chat_language: &str,
        tts_language: &str,
    ) -> Option<SubagentAnalysis> {
        for attempt in 1..=2 {
            match self.analyze_once(text, chat_language, tts_language, attempt).await {
                Some(a) => return Some(a),
                None if attempt < 2 => {
                    tracing::warn!("avatar subagent: attempt {attempt} failed, retrying");
                }
                None => {}
            }
        }
        None
    }

    /// Translate a single chunk of text. Plain output, no JSON. Designed
    /// to be called per-paragraph for long replies — each call stays
    /// small enough that z.ai's coding-paas endpoint doesn't choke
    /// (its 60s connection budget + per-key rate limits both bite for
    /// 1KB+ inputs in one shot).
    ///
    /// Retries with backoff on 429 (rate limit) — z.ai's per-key
    /// minute-window quota is strict and a long reply may need
    /// several seconds of breathing room between calls.
    pub async fn translate_chunk(
        &self,
        text: &str,
        target_language: &str,
    ) -> Option<String> {
        let prompt = format!(
            "Translate the following text into {target_language}. \
             Output ONLY the translation — no preamble, no quotation marks, \
             no markdown decoration, no explanation. Preserve sentence \
             count. If the text is already in {target_language}, return it \
             unchanged.\n\nText:\n{text}",
        );
        // Cap per-attempt to 30s. Translate is meant for ~150c chunks
        // that should complete in 1-5s; anything past 30s means the
        // upstream LLM is wedged and burning more wall time on it just
        // delays the user's audio further. With 2 retries × 30s, worst
        // case is 60s per failed chunk vs 240s under self.timeout (60s)
        // × 4 attempts.
        let attempt_budget = std::cmp::min(self.timeout, Duration::from_secs(30));
        for attempt in 1..=3 {
            tracing::info!(
                "avatar subagent.translate: → attempt={attempt} ({}c → {target_language}): {:?}",
                text.chars().count(),
                safe_prefix(text, 100),
            );
            let result = tokio::time::timeout(
                attempt_budget,
                self.backend.ask("", &prompt),
            )
            .await;
            match result {
                Ok(Ok(out)) => {
                    let cleaned = strip_code_fence(out.trim()).trim().to_string();
                    if cleaned.is_empty() {
                        tracing::warn!(
                            "avatar subagent.translate: empty response (attempt {attempt})"
                        );
                        tokio::time::sleep(Duration::from_secs(2)).await;
                        continue;
                    }
                    tracing::info!(
                        "avatar subagent.translate: ← attempt={attempt} ({}c): {:?}",
                        cleaned.chars().count(),
                        safe_prefix(&cleaned, 100),
                    );
                    return Some(cleaned);
                }
                Ok(Err(e)) => {
                    let msg = e.to_string();
                    let is_rate_limit = msg.contains("429") || msg.contains("Rate limit");
                    tracing::warn!(
                        "avatar subagent.translate: LLM failed (attempt {attempt}): {e}"
                    );
                    if attempt < 3 {
                        // Exponential backoff for 429, brief retry otherwise.
                        let wait = if is_rate_limit { 1u64 << attempt } else { 1 };
                        tokio::time::sleep(Duration::from_secs(wait)).await;
                    }
                }
                Err(_) => {
                    tracing::warn!(
                        "avatar subagent.translate: timeout (attempt {attempt}) after {}s",
                        attempt_budget.as_secs()
                    );
                    if attempt < 3 {
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                }
            }
        }
        None
    }

    async fn analyze_once(
        &self,
        text: &str,
        chat_language: &str,
        tts_language: &str,
        attempt: u32,
    ) -> Option<SubagentAnalysis> {
        let truncated = if text.len() > 2000 { &text[..2000] } else { text };

        let system_prompt = self
            .system_prompt_template
            .replace("{chat_lang}", chat_language)
            .replace("{tts_lang}", tts_language);

        tracing::info!(
            "avatar subagent: → input attempt={attempt} ({}c, chat={chat_language}, tts={tts_language}): {:?}",
            truncated.chars().count(),
            safe_prefix(truncated, 200),
        );
        let result =
            tokio::time::timeout(self.timeout, self.backend.ask(&system_prompt, truncated)).await;

        match result {
            Ok(Ok(response_text)) => {
                tracing::info!(
                    "avatar subagent: ← raw LLM response attempt={attempt} ({}c): {:?}",
                    response_text.chars().count(),
                    safe_prefix(&response_text, 500),
                );
                if response_text.trim().is_empty() {
                    tracing::warn!("avatar subagent: empty response (attempt {attempt})");
                    return None;
                }
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
                                format!("{}c: {:?}", s.chars().count(), safe_prefix(s, 120))
                            }),
                            analysis.translated_text.as_ref().map(|s| {
                                format!("{}c: {:?}", s.chars().count(), safe_prefix(s, 120))
                            }),
                        );
                        Some(analysis)
                    }
                    Err(e) => {
                        tracing::warn!(
                            "avatar subagent: JSON parse failed attempt={attempt} ({e}), raw: {}",
                            safe_prefix(cleaned, 200)
                        );
                        None
                    }
                }
            }
            Ok(Err(e)) => {
                tracing::warn!("avatar subagent: LLM call failed (attempt {attempt}): {e}");
                None
            }
            Err(_) => {
                tracing::warn!(
                    "avatar subagent: timed out (attempt {attempt}) after {}s",
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
