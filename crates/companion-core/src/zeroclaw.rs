//! Read-only client for the upstream agent daemon.
//!
//! Despite the file name (kept for back-compat), this drives any of the
//! three pi-agent-family flavors via [`AgentKind`]:
//!
//! - **Zeroclaw** / **Hermes** / **Custom** — `POST /webhook`
//!   `{"message": "..."}` → `{"model","response"}`. Hermes piggy-backs
//!   on the same shape via the bridge shim (`hermes-bridge.py`).
//! - **Openclaw** — `POST /v1/chat/completions` (OpenAI-compatible)
//!   `{"model":"openclaw","messages":[{...}]}` → standard OpenAI
//!   completion. Auth is Bearer token (required for LAN binding).
//!
//! All flavors expose `GET /health` (no auth) so reachability checks
//! are uniform.
//!
//! Two operations matter for the avatar pipeline:
//!
//! 1. [`ZeroclawClient::send_chat`] — turn a user message into a reply.
//!    Dispatches by `kind`.
//! 2. [`ZeroclawClient::events`] — subscribe to `/api/events` SSE.
//!    Zeroclaw-only today; the other flavors don't broadcast a
//!    comparable event stream, so the call returns an empty stream
//!    for them. The avatar pipeline doesn't actually depend on SSE
//!    (we drive off the synchronous chat reply), so an empty stream
//!    is safe.

use std::time::Duration;

use eventsource_stream::Eventsource;
use futures_util::{Stream, StreamExt};
use serde::{Deserialize, Serialize};

use crate::config::{AgentKind, ZeroclawConfig};

/// A typed wrapper for events the companion cares about. Zeroclaw's SSE
/// stream carries many event types; we only surface what's actionable
/// for avatar / pulse.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AgentEvent {
    /// The agent finished a turn. `text` is the assistant's reply.
    AgentReply {
        text: String,
        #[serde(default)]
        session_id: Option<String>,
    },
    /// A streaming token from the agent (when streaming is enabled).
    AgentToken {
        text: String,
        #[serde(default)]
        session_id: Option<String>,
    },
    /// Something else we don't classify. `raw` carries the original JSON
    /// so callers can pattern-match on payload shape if they care.
    Other {
        raw: serde_json::Value,
    },
}

/// Client for the upstream agent HTTP gateway.
///
/// Type name kept as `ZeroclawClient` for back-compat across the
/// workspace; the actual protocol is selected per call via [`AgentKind`].
#[derive(Clone)]
pub struct ZeroclawClient {
    kind: AgentKind,
    base_url: String,
    pair_token: Option<String>,
    timeout_secs: u64,
    http: reqwest::Client,
}

impl ZeroclawClient {
    /// Build from config.
    pub fn new(cfg: &ZeroclawConfig) -> anyhow::Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(cfg.timeout_secs))
            .build()?;
        Ok(Self {
            kind: cfg.kind,
            base_url: cfg.url.trim_end_matches('/').to_string(),
            pair_token: cfg.pair_token.clone(),
            timeout_secs: cfg.timeout_secs,
            http,
        })
    }

    /// Which agent flavor this client drives.
    pub fn kind(&self) -> AgentKind {
        self.kind
    }
    /// The configured gateway base URL (no trailing slash).
    pub fn base_url(&self) -> &str {
        &self.base_url
    }
    /// Whether a pairing/bearer token is configured (we never expose
    /// the token itself).
    pub fn has_pair_token(&self) -> bool {
        self.pair_token.is_some()
    }
    /// Per-request timeout in seconds.
    pub fn timeout_secs(&self) -> u64 {
        self.timeout_secs
    }

    /// Health-check the upstream gateway (`GET /health`). All three
    /// flavors expose this without auth; we don't even send the bearer.
    pub async fn health(&self) -> anyhow::Result<bool> {
        let url = format!("{}/health", self.base_url);
        let resp = self.http.get(&url).send().await?;
        Ok(resp.status().is_success())
    }

    /// Send a user message to the agent and return the textual reply.
    /// Dispatches by `kind`.
    pub async fn send_chat(&self, message: &str) -> anyhow::Result<String> {
        match self.kind {
            // zeroclaw and hermes-via-bridge both speak `/webhook`
            // {message}→{response}. "custom" defaults to the same shape
            // so any other webhook-style endpoint Just Works.
            AgentKind::Zeroclaw | AgentKind::Hermes | AgentKind::Custom => {
                self.send_chat_webhook(message).await
            }
            // openclaw exposes an OpenAI-compatible chat-completions
            // endpoint (we enable `gateway.http.endpoints.chatCompletions`
            // server-side). `model:"openclaw"` is the magic string that
            // routes to the agent rather than a raw LLM model.
            AgentKind::Openclaw => self.send_chat_openai(message).await,
        }
    }

    /// `POST /webhook` shape (zeroclaw + hermes bridge + custom).
    async fn send_chat_webhook(&self, message: &str) -> anyhow::Result<String> {
        let url = format!("{}/webhook", self.base_url);
        let body = serde_json::json!({ "message": message });
        let mut req = self.http.post(&url).json(&body);
        if let Some(ref tok) = self.pair_token {
            req = req.bearer_auth(tok);
        }
        let resp = req.send().await?;
        let status = resp.status();
        if !status.is_success() {
            let txt = resp.text().await.unwrap_or_default();
            anyhow::bail!(
                "{} /webhook returned {status}: {txt}",
                self.kind.label()
            );
        }
        let payload: serde_json::Value = resp.json().await?;
        for key in &["response", "reply", "text", "content", "output"] {
            if let Some(s) = payload.get(*key).and_then(|v| v.as_str()) {
                return Ok(s.to_string());
            }
        }
        // Last-resort: stringify the whole payload.
        Ok(payload.to_string())
    }

    /// `POST /v1/chat/completions` OpenAI-compatible shape (openclaw).
    async fn send_chat_openai(&self, message: &str) -> anyhow::Result<String> {
        let url = format!("{}/v1/chat/completions", self.base_url);
        // openclaw expects model="openclaw" or "openclaw/<agentId>" —
        // not the underlying LLM model id. That selects the agent;
        // the LLM model is configured on the openclaw side.
        let body = serde_json::json!({
            "model": "openclaw",
            "messages": [{"role": "user", "content": message}],
        });
        let mut req = self.http.post(&url).json(&body);
        if let Some(ref tok) = self.pair_token {
            req = req.bearer_auth(tok);
        }
        let resp = req.send().await?;
        let status = resp.status();
        if !status.is_success() {
            let txt = resp.text().await.unwrap_or_default();
            anyhow::bail!(
                "openclaw /v1/chat/completions returned {status}: {txt}"
            );
        }
        let payload: serde_json::Value = resp.json().await?;
        if let Some(s) = payload
            .pointer("/choices/0/message/content")
            .and_then(|v| v.as_str())
        {
            return Ok(s.to_string());
        }
        // OpenAI streaming/delta fallback shapes — unlikely without
        // stream:true but cheap to try before giving up.
        if let Some(s) = payload
            .pointer("/choices/0/text")
            .and_then(|v| v.as_str())
        {
            return Ok(s.to_string());
        }
        if let Some(arr) = payload.get("error") {
            anyhow::bail!("openclaw error payload: {arr}");
        }
        Ok(payload.to_string())
    }

    /// Subscribe to the upstream SSE event stream and yield typed
    /// [`AgentEvent`]s. Zeroclaw-only — the other flavors return an
    /// empty stream because they don't expose a comparable
    /// observability feed. The avatar pipeline doesn't depend on SSE
    /// (we drive off the synchronous chat reply), so an empty stream
    /// is harmless. Reconnects are the caller's responsibility.
    pub async fn events(&self) -> anyhow::Result<impl Stream<Item = AgentEvent>> {
        if !matches!(self.kind, AgentKind::Zeroclaw | AgentKind::Custom) {
            // Yield an empty stream — the SSE bridge in companion-server
            // will sit idle for this kind, which is what we want.
            return Ok(futures_util::stream::empty().left_stream());
        }
        let url = format!("{}/api/events", self.base_url);
        let mut req = self.http.get(&url);
        if let Some(ref tok) = self.pair_token {
            req = req.bearer_auth(tok);
        }
        let resp = req.send().await?;
        if !resp.status().is_success() {
            anyhow::bail!("zeroclaw /api/events returned {}", resp.status());
        }
        let stream = resp
            .bytes_stream()
            .eventsource()
            .filter_map(|ev| async move {
                let ev = match ev {
                    Ok(e) => e,
                    Err(err) => {
                        tracing::debug!("companion: sse decode error: {err}");
                        return None;
                    }
                };
                let raw: serde_json::Value = match serde_json::from_str(&ev.data) {
                    Ok(v) => v,
                    Err(_) => return None,
                };
                Some(classify_event(raw))
            });
        Ok(stream.right_stream())
    }
}

/// Best-effort classifier for upstream SSE payloads.
///
/// Zeroclaw v0.7.5's `/api/events` stream emits **observability** events
/// only (`agent_start`, `agent_end`, `llm_request`, `tool_call`, …) —
/// the agent's reply text comes back synchronously from `POST /webhook`,
/// NOT as an SSE event. So classifying SSE as `AgentReply` is essentially
/// best-effort speculation; the avatar pipeline drives off the synchronous
/// chat response, not this classifier.
///
/// We still keep the classifier permissive enough to recognize hypothetical
/// future event shapes (`event:"agent.reply"`, `role:"assistant"+final:true`)
/// in case a downstream zeroclaw fork or future version starts broadcasting
/// reply text. Today, every zeroclaw v0.7.x event lands as `Other`, which
/// the SSE bridge logs at debug level.
fn classify_event(raw: serde_json::Value) -> AgentEvent {
    let event_type = raw
        .get("event")
        .or_else(|| raw.get("type"))
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let session_id = raw
        .get("session_id")
        .and_then(|v| v.as_str())
        .map(String::from);

    let text = raw
        .get("text")
        .or_else(|| raw.get("content"))
        .and_then(|v| v.as_str())
        .map(String::from);

    let is_reply = event_type.contains("reply") || event_type == "agent.reply";
    let is_token = event_type.contains("token") || event_type == "agent.token";
    let is_final = raw.get("final").and_then(|v| v.as_bool()).unwrap_or(false);
    let role_assistant = raw
        .get("role")
        .and_then(|v| v.as_str())
        .map(|s| s == "assistant")
        .unwrap_or(false);

    if let Some(text) = text.clone() {
        if is_reply || (role_assistant && is_final) {
            return AgentEvent::AgentReply { text, session_id };
        }
        if is_token || role_assistant {
            return AgentEvent::AgentToken { text, session_id };
        }
    }
    AgentEvent::Other { raw }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_reply() {
        let raw = serde_json::json!({
            "event": "agent.reply",
            "text": "hello",
            "session_id": "abc",
        });
        match classify_event(raw) {
            AgentEvent::AgentReply { text, session_id } => {
                assert_eq!(text, "hello");
                assert_eq!(session_id.as_deref(), Some("abc"));
            }
            _ => panic!("expected AgentReply"),
        }
    }

    #[test]
    fn classify_token_streaming() {
        let raw = serde_json::json!({
            "event": "agent.token",
            "text": "he",
        });
        match classify_event(raw) {
            AgentEvent::AgentToken { text, .. } => assert_eq!(text, "he"),
            _ => panic!("expected AgentToken"),
        }
    }

    #[test]
    fn classify_unknown_falls_through() {
        let raw = serde_json::json!({"event": "tool.call", "name": "shell"});
        matches!(classify_event(raw), AgentEvent::Other { .. });
    }
}
