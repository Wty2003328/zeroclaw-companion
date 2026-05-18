//! WebSocket handler for the Live2D avatar.
//!
//! The companion mounts this at `GET /ws/avatar` on its OWN HTTP server
//! (not on zeroclaw's). It is driven by [`AvatarEvent`]s broadcast on
//! the shared channel — typically by an SSE bridge that subscribes to
//! upstream zeroclaw's `/api/events` and forwards `agent.reply` events.
//!
//! Wire protocol:
//! - On connect: server sends `Connected` + `ModelInfo`.
//! - Frontend sends `Ready` after loading the Live2D model.
//! - For each agent turn, server emits `Expression` → `Audio`
//!   (with lip sync) → `Idle`. The `Audio` payload is base64-encoded
//!   bytes from the configured TTS port.
//! - Frontend may send `Touch` / `MotionRequest` / `ExpressionRequest`
//!   for interactive feedback (currently logged only).

use std::sync::Arc;

use anyhow::Result;
use axum::{
    extract::State,
    extract::ws::{Message, WebSocket, WebSocketUpgrade},
    response::IntoResponse,
};

/// Char-boundary-safe slice for diagnostic logging. Prevents panics when
/// the byte-position cap lands inside a multi-byte UTF-8 codepoint
/// (emoji and CJK in agent replies trip this constantly).
fn safe_prefix(s: &str, max_bytes: usize) -> &str {
    let mut end = s.len().min(max_bytes);
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Strip emoji + markdown decorations from a TTS-bound string.
///
/// Even with a strong "remove emoji" instruction in the subagent
/// system prompt, glm-4.5-flash (and similar small models) frequently
/// preserves them or replaces them with full-width "？？" — both of
/// which the TTS reads aloud as gibberish. This is the deterministic
/// safety net: post-process the model's output before handing it to
/// the TTS engine.
///
/// What we drop:
///   - Emoji (the entire pictograph block at U+1F300+, plus the
///     compatibility set in the BMP at U+2600–27BF, U+2700–27BF, etc.),
///     ZWJ glue, variation selectors, regional indicators.
///   - Markdown decorations: `*` `_` `~` `\`` `#` `>` when used as
///     surrounding punctuation. We deliberately keep them when
///     embedded inside a word (rare in TTS text).
///   - Run of full-width punctuation `？！。、` are preserved (they
///     belong in CJK speech).
fn strip_emoji_and_markdown_for_tts(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for c in input.chars() {
        let cp = c as u32;
        let is_emoji = matches!(cp,
            0x1F300..=0x1FAFF      // pictographs, supplemental, etc.
            | 0x1F1E6..=0x1F1FF    // regional indicators (flags)
            | 0x2600..=0x27BF      // misc symbols + dingbats
            | 0xFE0F | 0xFE0E      // variation selectors (text/emoji)
            | 0x200D                // zero-width joiner
            | 0x20E3                // combining enclosing keycap
        );
        if is_emoji {
            continue;
        }
        // Common markdown decorators when on their own (not embedded
        // in CJK / words). Replace with a space so adjacent words
        // don't fuse, then collapse runs below.
        if matches!(c, '*' | '_' | '~' | '`' | '#' | '>' | '|' | '\\') {
            out.push(' ');
            continue;
        }
        out.push(c);
    }
    // Collapse runs of whitespace introduced by stripping.
    let collapsed: String = out.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed.trim().to_string()
}

/// Strip leading thinking-trace preamble that some upstream agents
/// (zeroclaw with reasoning models, glm-4.5 without `thinking: disabled`,
/// etc.) leak into their reply text.
///
/// Two stripping passes run in tandem per leading sentence; we drop the
/// sentence if EITHER fires, then re-evaluate the next sentence:
///
/// 1. **Marker prefix match.** Drop sentences that begin with known
///    thinking-trace markers ("Let me check", "The user is", "Based on
///    USER.md", "I'll check the weather", "webhook_msg_…", etc.).
///    Case-insensitive on the leading ASCII portion.
///
/// 2. **Mostly-Latin sentence drop (when `prefer_cjk = true`).** Drop
///    leading sentences whose CJK content ratio is below 20%. Catches
///    the pattern `"明天" (tomorrow) likely means May 14...` where the
///    sentence STARTS with a CJK character in quotes but is structurally
///    English commentary. The previous "leading-Latin-char-count" gate
///    missed this because the first char was the `"` (Latin) followed
///    by CJK — only one leading Latin char.
///
/// The 20% threshold is the sweet spot: a real CJK reply with a few
/// English loanwords ("sunny", "14°C") sits well above 50%; a thinking
/// sentence with one quoted CJK term sits below 10%.
fn strip_thinking_preamble(text: &str, prefer_cjk: bool) -> String {
    const MARKERS: &[&str] = &[
        "the user said",
        "the user is asking",
        "the user is",
        "the user wants",
        "the user mentioned",
        "the user needs",
        "let me check",
        "let me get",
        "let me store",
        "let me respond",
        "let me look",
        "let me ",  // catch-all "let me X"
        "looking at the context",
        "looking at ",
        "based on the context",
        "based on user.md",
        "based on the user",
        "based on ",
        "first, let me",
        "first let me",
        "i should ",
        "i need to ",
        "i'll check",
        "i'll look",
        "webhook_msg_",
    ];

    /// Boundary chars used by both the lookahead (sentence we're judging)
    /// and the advance (where we cut to drop it). Includes CJK terminators
    /// so a thinking sentence ending with 。 still gets a clean cut.
    fn sentence_end_byte(s: &str) -> Option<usize> {
        s.char_indices()
            .find(|(_, c)| matches!(*c, '.' | '!' | '?' | '\n' | '。' | '！' | '？'))
            .map(|(i, c)| i + c.len_utf8())
    }

    /// True when the (already-isolated) sentence is well above the noise
    /// floor in length AND its CJK ratio is below 20%. Short sentences
    /// (`OK!`, `Yes.`, `はい。`) are too short to judge reliably — we
    /// preserve them.
    fn is_mostly_latin(sentence: &str) -> bool {
        let total = sentence.chars().count();
        if total < 12 {
            return false;
        }
        let cjk = sentence.chars().filter(|c| is_cjk(*c)).count();
        (cjk as f64) / (total as f64) < 0.20
    }

    let mut current: &str = text.trim_start();
    loop {
        // Lookahead: the leading sentence.
        let first_sentence_end = sentence_end_byte(current);
        let first_sentence = match first_sentence_end {
            Some(idx) => &current[..idx],
            None => current,
        };

        // Marker check on the leading ASCII portion (CJK passes through
        // `to_ascii_lowercase` unchanged, which is what we want).
        let probe: String = first_sentence.chars().take(60).collect();
        let probe_lc = probe.to_ascii_lowercase();
        let marker_match = MARKERS.iter().any(|m| probe_lc.starts_with(m));

        // CJK-aware drop. Only fires when the user is targeting a CJK
        // TTS language; otherwise it'd happily eat legitimate English
        // chat replies.
        let latin_drop = prefer_cjk && is_mostly_latin(first_sentence);

        if !marker_match && !latin_drop {
            break;
        }

        // Drop this sentence. If it has no terminator (a runaway one-
        // sentence "reply" that's just thinking), drop the whole text.
        match first_sentence_end {
            Some(idx) if idx < current.len() => {
                current = current[idx..].trim_start();
            }
            _ => {
                current = "";
                break;
            }
        }
    }

    current.to_string()
}

fn is_cjk(c: char) -> bool {
    matches!(c as u32,
        0x3040..=0x309F        // hiragana
        | 0x30A0..=0x30FF      // katakana
        | 0x3400..=0x4DBF      // CJK ext A
        | 0x4E00..=0x9FFF      // CJK unified
        | 0xF900..=0xFAFF      // CJK compat
        | 0x20000..=0x2FFFF    // CJK ext B-F
    )
}

/// Best-effort source-language detection from reply text. The companion's
/// `chat_language` setting is what the USER types in, but the agent
/// frequently replies in a different language than the user's input —
/// the user types Chinese, the agent replies Chinese; the user has
/// chat_language set to "en" because they originally configured it for
/// English chat. Without auto-detect, we'd hand NLLB the wrong
/// `src_lang` and get tokenizer garbage.
///
/// Heuristic over codepoint distribution:
///   - kana ratio ≥ 8%   → "ja" (kana is JA-exclusive; even small amounts disambiguate)
///   - han  ratio ≥ 30%  → "zh" (no kana → not JA → Chinese)
///   - cyrillic ratio ≥ 50% → "ru"
///   - hangul ratio ≥ 30%  → "ko"
///   - arabic ratio ≥ 50%  → "ar"
///   - otherwise None → caller falls back to the configured chat_language.
///
/// We deliberately don't try to distinguish further (Spanish vs French
/// vs Italian etc.) — NLLB on `en` source is OK for most Latin-script
/// languages; the failure mode we're fixing is the dramatic one
/// (Latin src on CJK input).
fn detect_source_lang(text: &str) -> Option<&'static str> {
    let total: usize = text.chars().filter(|c| !c.is_whitespace()).count();
    if total < 4 {
        return None;  // too short to judge
    }
    let mut kana = 0usize;
    let mut han = 0usize;
    let mut cyrillic = 0usize;
    let mut hangul = 0usize;
    let mut arabic = 0usize;
    for c in text.chars() {
        let cp = c as u32;
        if (0x3040..=0x309F).contains(&cp) || (0x30A0..=0x30FF).contains(&cp) {
            kana += 1;
        } else if (0x4E00..=0x9FFF).contains(&cp) || (0x3400..=0x4DBF).contains(&cp) {
            han += 1;
        } else if (0x0400..=0x04FF).contains(&cp) {
            cyrillic += 1;
        } else if (0xAC00..=0xD7AF).contains(&cp) {
            hangul += 1;
        } else if (0x0600..=0x06FF).contains(&cp) {
            arabic += 1;
        }
    }
    let t = total as f64;
    if (kana as f64) / t >= 0.08 { return Some("ja"); }
    if (han as f64) / t >= 0.30 { return Some("zh"); }
    if (hangul as f64) / t >= 0.30 { return Some("ko"); }
    if (cyrillic as f64) / t >= 0.50 { return Some("ru"); }
    if (arabic as f64) / t >= 0.50 { return Some("ar"); }
    None
}

// `split_for_translation` and its test module were removed in 2026-05-14
// cleanup. They served the per-paragraph LLM fallback path that the
// bulk `subagent.analyze()` architecture (2026-05) replaced — one
// big LLM call instead of N small ones. The function was orphaned
// after that refactor.
use tokio::sync::broadcast;

use crate::config::AvatarConfig;
use crate::expression::ExpressionMapper;
use crate::protocol::{AvatarMessage, AvatarNotification};
use arc_swap::{ArcSwap, ArcSwapOption};

use crate::subagent::AvatarSubagent;
use crate::tts_server::AnimeTtsManager;

/// Events published by the companion-server's chat handler and consumed
/// by every connected `/ws/avatar` client.
///
/// The expensive work (subagent translation + TTS synthesis) runs ONCE,
/// PER TURN, on the producer side. The resulting frames are broadcast
/// pre-rendered. This keeps:
/// - subagent token usage flat regardless of how many viewers are open
/// - TTS load flat (one synthesis per turn, not N)
/// - audio playback synchronized — every viewer plays the same bytes
#[derive(Debug, Clone)]
pub enum AvatarEvent {
    /// Pre-rendered notification to fan out to every client. Producer
    /// sends one of these for each frame in the sequence
    /// (Expression → Motion? → Text → Debug → Audio → Idle).
    Frame(AvatarNotification),
    /// Trigger a motion on the avatar (manual override path).
    Motion { group: String, name: String },
    /// Shutdown signal.
    Shutdown,
}

/// Shared state for the avatar WebSocket route.
///
/// Three fields are swappable at runtime so settings changes apply
/// without a process restart:
///
/// - `config` ([`ArcSwap<AvatarConfig>`]) — chat / TTS language, speed,
///   voice, subagent toggles, expression mappings.
/// - `subagent` ([`ArcSwapOption<AvatarSubagent>`]) — backend, key, model.
/// - `tts` ([`ArcSwap<AnimeTtsManager>`]) — engine, launch command, model
///   path, reference clip, GPU device. The hot-swap performs the
///   graceful `stop_server()` → `start_server()` dance so the previous
///   TTS child process exits cleanly (CUDA `empty_cache()` + sync).
///
/// Read path is lock-free: each call site `.load_full()`s a snapshot at
/// the top of a turn so a swap that lands mid-turn doesn't tear state.
pub struct AvatarWsState {
    pub config: ArcSwap<AvatarConfig>,
    pub event_tx: broadcast::Sender<AvatarEvent>,
    pub subagent: ArcSwapOption<AvatarSubagent>,
    pub tts: ArcSwap<AnimeTtsManager>,
    /// NMT sidecar lifecycle manager. `Some` when
    /// `[avatar.subagent.translator] backend = "http"`; `None` for the
    /// LLM-only path. Held here (rather than inside the subagent) so
    /// shutdown can stop it cleanly in symmetry with the TTS manager.
    pub translator_mgr: ArcSwapOption<crate::translator::TranslatorManager>,
    /// Speech (STT) sidecar lifecycle manager. `Some` when
    /// `[avatar.speech] enabled = true`. Used for voice input and (when
    /// `[avatar.speech] verify_tts = true`) post-synthesis TTS
    /// verification. Same shutdown discipline as the other two managers.
    pub speech_mgr: ArcSwapOption<crate::speech_server::SpeechManager>,
}

/// Axum handler for `GET /ws/avatar`.
pub async fn handle_ws_avatar(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AvatarWsState>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_avatar_socket(socket, state))
}

async fn handle_avatar_socket(mut socket: WebSocket, state: Arc<AvatarWsState>) {
    let session_id = uuid::Uuid::new_v4().to_string();

    let connected = AvatarNotification::Connected {
        session_id: session_id.clone(),
    };
    if send_notification(&mut socket, &connected).await.is_err() {
        return;
    }

    // Default model URL lives under `/live2d/`, NOT `/avatar/`, to avoid
    // colliding with the React Router route `/avatar`. The frontend
    // serves these files from web/public/live2d/.
    let cfg_snap = state.config.load();
    let model_info = AvatarNotification::ModelInfo {
        model_url: cfg_snap
            .model
            .model_dir
            .clone()
            .unwrap_or_else(|| "/live2d/models/haru/Haru.model3.json".to_string()),
        scale: cfg_snap.model.scale,
        anchor: cfg_snap.model.anchor.clone(),
        default_expression: cfg_snap.model.default_expression.clone(),
    };
    drop(cfg_snap);
    if send_notification(&mut socket, &model_info).await.is_err() {
        return;
    }

    tracing::info!("avatar: client connected (session={session_id})");

    let mut event_rx = state.event_tx.subscribe();

    loop {
        tokio::select! {
            msg = socket.recv() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(avatar_msg) = serde_json::from_str::<AvatarMessage>(&text) {
                            if let AvatarMessage::Ready = avatar_msg {
                                tracing::info!("avatar: frontend ready");
                            } else {
                                handle_avatar_message(&avatar_msg);
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => {
                        tracing::info!("avatar: client disconnected (session={session_id})");
                        break;
                    }
                    _ => {}
                }
            }

            event = event_rx.recv() => {
                match event {
                    Ok(AvatarEvent::Frame(frame)) => {
                        let _ = send_notification(&mut socket, &frame).await;
                    }
                    Ok(AvatarEvent::Motion { group, name }) => {
                        let motion = AvatarNotification::Motion { group, name };
                        let _ = send_notification(&mut socket, &motion).await;
                    }
                    Ok(AvatarEvent::Shutdown) | Err(broadcast::error::RecvError::Closed) => {
                        break;
                    }
                    Err(broadcast::error::RecvError::Lagged(count)) => {
                        tracing::warn!("avatar: event channel lagged by {count} events");
                    }
                }
            }
        }
    }
}

/// Producer-side: run subagent + TTS ONCE, then broadcast each rendered
/// frame to all connected viewers. Call this from the chat handler
/// (or anywhere else that wants the avatar to speak); never call it
/// from a per-client task.
///
/// Returns the chat-language reply text on success so the caller can
/// echo it (e.g. as the synchronous /api/chat response body).
pub async fn process_speak(state: &Arc<AvatarWsState>, text: &str) -> Result<String> {
    // Snapshot the three hot-swappable handles for the duration of this
    // turn. Once captured, the rest of the function can read freely
    // even if the user flips settings mid-call — the in-flight turn
    // keeps using the snapshot, the next call picks up the new state.
    let cfg = state.config.load_full();
    let tts = state.tts.load_full();
    let subagent_snap = state.subagent.load_full();

    let chat_lang = cfg.chat_language.clone();
    let tts_lang = cfg.tts.language.clone();
    let expression_mapper = ExpressionMapper::new(&cfg.expressions);

    // Belt-and-suspenders: strip thinking-trace preamble before any
    // downstream consumer (keyword detector, subagent.analyze input,
    // subtitle, translator). analyze()'s LLM prompt also strips, but
    // we don't want to depend solely on the LLM complying.
    //
    // `prefer_cjk` depends only on the TTS target language. The earlier
    // version also gated on `chat_lang != CJK`, which broke the case
    // where the user chats in Chinese: the agent's reply mixes English
    // thinking with Chinese content, and the CJK-aware drop needs to
    // fire regardless of what language the user types in.
    let prefer_cjk_pre = matches!(tts_lang.as_str(), "ja" | "zh");
    let stripped_owned = strip_thinking_preamble(text, prefer_cjk_pre);
    if stripped_owned.chars().count() != text.chars().count() {
        tracing::info!(
            "avatar: process_speak stripped thinking preamble ({} → {} chars)",
            text.chars().count(),
            stripped_owned.chars().count(),
        );
    }
    let text: &str = &stripped_owned;

    let keyword_expr = expression_mapper.detect(text);
    let mut motion_to_send: Option<AvatarNotification> = None;
    let mut subagent_used = false;

    // Skip subagent when chat == tts language and the user opted into
    // the fast-path: the "translation" would be a no-op and keyword
    // detection picks a sensible expression. Saves ~5–10s per turn.
    let need_translation = chat_lang != tts_lang;

    // What backend produced the spoken text. Sent on every Debug
    // frame so the UI can label the analysis path honestly (the iter-13
    // version hardcoded "LLM-driven" even when the local NMT path
    // was active — user-reported iter 14). Computed once near the top
    // so all three emission sites agree.
    let translation_path: &'static str = if !need_translation {
        "none"
    } else if matches!(
        cfg.subagent.translator.backend,
        crate::translator::TranslatorBackendKind::Http,
    ) {
        "nmt"
    } else {
        "llm"
    };
    let should_run_subagent = subagent_snap.is_some()
        && (need_translation || !cfg.subagent.only_when_translating);

    // Streaming branch: when enabled + the backend supports it, take
    // a different path that fires TTS per sentence as the LLM streams
    // tokens. Skips the bulk JSON analyze() (saves 15-25s on long
    // replies); uses keyword-based expression detection.
    let streaming_eligible = should_run_subagent
        && cfg.subagent.streaming
        && need_translation
        && subagent_snap.as_ref().map(|s| s.supports_streaming()).unwrap_or(false);
    if streaming_eligible {
        return run_streaming_speak(
            state,
            text,
            &chat_lang,
            &tts_lang,
            &expression_mapper,
            keyword_expr,
        )
        .await;
    }

    // Always go through the bulk subagent.analyze() — one call for
    // the whole reply. Reasons for the 2026-05 architecture change:
    //   - Per-paragraph fallback fired N LLM calls + 800ms sleep
    //     between them; long replies took ~25s vs ~5s for one bulk
    //     call.
    //   - With thinking-disabled + max_tokens raised to 8000, glm-4.5-
    //     flash handles 2KB+ inputs in one call without truncation
    //     (verified empirically; see zai_thinking_disable memory).
    //   - TTS still streams sentence-by-sentence (chunker below)
    //     so the user starts hearing audio before the whole bulk
    //     translation finishes generating downstream chunks.
    let (expression, tts_text_opt, clean_chat_opt) = if should_run_subagent {
        let subagent = subagent_snap.as_ref().unwrap();
        match subagent.analyze(text, &chat_lang, &tts_lang).await {
            Some(analysis) => {
                subagent_used = true;
                if let Some(ref motion) = analysis.motion {
                    motion_to_send = Some(AvatarNotification::Motion {
                        group: motion.group.clone(),
                        name: format!("{}", motion.index),
                    });
                }
                let translated = analysis.translated_text.clone();
                let cleaned = analysis.clean_chat_text.clone();
                let expr = AvatarSubagent::to_expression(&analysis, &keyword_expr);
                (expr, translated, cleaned)
            }
            None => (keyword_expr, None, None),
        }
    } else {
        if subagent_snap.is_some() && !need_translation {
            tracing::debug!(
                "avatar: subagent skipped (same language; only_when_translating=true)"
            );
        }
        (keyword_expr, None, None)
    };

    // Subtitle = the cleaned chat-language reply (subagent-stripped) when
    // available, else the raw input with expression tags removed. The
    // subagent strips thinking-style preamble like "The user said …" /
    // "Let me check …" that some upstream agents leak into their output.
    let raw_subtitle = expression_mapper.strip_tags(text);
    let subtitle_text = match clean_chat_opt {
        Some(t) if !t.trim().is_empty() => expression_mapper.strip_tags(&t),
        _ => raw_subtitle.clone(),
    };
    // Decide what text the TTS will speak.
    //
    // - same-language: speak the subtitle (no translation needed).
    // - cross-language: speak the bulk subagent translation. We
    //   always do exactly one subagent.analyze() call (above) — the
    //   per-paragraph fallback was removed in 2026-05 because each
    //   extra LLM round-trip added 5–10s, and the bulk path is
    //   reliable now that we send `thinking: disabled` + max_tokens
    //   = 8000.
    //
    // Whatever we end up with here is sentence-chunked downstream
    // for streaming, so even a long bulk translation starts speaking
    // the first sentence quickly.
    let tts_text: String = if chat_lang == tts_lang {
        subtitle_text.clone()
    } else {
        let bulk = tts_text_opt
            .as_deref()
            .map(|t| expression_mapper.strip_tags(t))
            .filter(|t| !t.trim().is_empty())
            .unwrap_or_default();
        if bulk.is_empty() {
            tracing::warn!(
                "avatar: bulk translation empty; SKIPPING TTS for this turn"
            );
        } else {
            tracing::info!(
                "avatar: bulk translation accepted ({} chars)",
                bulk.chars().count()
            );
        }
        bulk
    };
    let tts_chunks: Vec<String> = if tts_text.trim().is_empty() {
        Vec::new()
    } else {
        vec![tts_text]
    };
    // Strip emoji + markdown from each chunk before TTS sees it. The
    // subagent's prompt asks for this but small models (glm-4.5-flash,
    // groq llama-3-8b, etc.) commonly leak emoji or full-width "？？"
    // that the TTS reads aloud — kills the immersion. Doing it here
    // is deterministic and provider-agnostic.
    let tts_chunks: Vec<String> = tts_chunks
        .into_iter()
        .map(|c| strip_emoji_and_markdown_for_tts(&c))
        .filter(|c| !c.trim().is_empty())
        .collect();
    let tts_text = tts_chunks.join("\n");

    tracing::info!(
        "avatar: process_speak (chat_lang={chat_lang} → tts_lang={tts_lang}, \
         subagent_used={subagent_used}, chat_chars={}, spoken_chars={}, \
         subscribers={})",
        subtitle_text.chars().count(),
        tts_text.chars().count(),
        state.event_tx.receiver_count(),
    );
    tracing::info!(
        "avatar: process_speak SUBTITLE = {:?}",
        safe_prefix(&subtitle_text, 300)
    );
    tracing::info!(
        "avatar: process_speak TTS_TEXT = {:?}",
        safe_prefix(&tts_text, 300)
    );

    let bcast = |frame: AvatarNotification| {
        // Send returns Err if there are zero receivers; that's fine,
        // we just skip the broadcast in that case.
        let _ = state.event_tx.send(AvatarEvent::Frame(frame));
    };

    // Intro frames (Expression / Motion / Text / Debug) are deferred
    // until just before the FIRST Audio frame fires, mirroring the
    // streaming path's `emit_intro_once!` pattern. Without this, the
    // chat bubble + facial expression land immediately (when this
    // function is called) while the audio doesn't arrive for several
    // seconds — the user sees "she said X" but doesn't hear her
    // speak it until the TTS finishes. The frontend reads the text
    // and the audio almost simultaneously this way.
    let expression_name_for_intro = expression.name.clone();
    let expression_intensity_for_intro = expression.intensity;
    let expression_duration_for_intro = expression.duration_ms;
    let subtitle_for_intro = subtitle_text.clone();
    let tts_text_for_intro = tts_text.clone();
    let motion_for_intro = motion_to_send;
    let mut intro_sent = false;
    let emit_intro_once = |intro_sent: &mut bool| {
        if *intro_sent { return; }
        *intro_sent = true;
        bcast(AvatarNotification::Expression {
            name: expression_name_for_intro.clone(),
            intensity: expression_intensity_for_intro,
            duration_ms: expression_duration_for_intro,
        });
        if let Some(ref m) = motion_for_intro {
            bcast(m.clone());
        }
        bcast(AvatarNotification::Text {
            content: subtitle_for_intro.clone(),
        });
        bcast(AvatarNotification::Debug {
            chat_text: subtitle_for_intro.clone(),
            spoken_text: tts_text_for_intro.clone(),
            expression: expression_name_for_intro.clone(),
            subagent_used,
            translation_path: translation_path.to_string(),
        });
    };

    // Empty tts_text means we deliberately skipped speech for this turn
    // (cross-language without a translation). Emit the intro frames so
    // the chat bubble still appears, then Idle and bail.
    if tts_text.trim().is_empty() {
        tracing::info!("avatar: process_speak skipping TTS (no spoken text)");
        emit_intro_once(&mut intro_sent);
        bcast(AvatarNotification::Idle);
        return Ok(subtitle_text);
    }

    // Sentence-chunked synthesis when streaming is enabled. All chunks
    // of one turn share `turn_id` so the frontend can queue them
    // sequentially without confusing them for stale audio.
    //
    // History: `\n\n` paragraph splitting was tried (task #137) on the
    // theory that "most chat replies are one paragraph anyway." That
    // assumption is wrong — a typical LLM reply is one paragraph of
    // several sentences. Paragraph splitting collapsed to single-shot
    // synth and made the user wait for a 30-60s audio chunk before
    // playback started. Even after fixing the NMT translator to
    // preserve `\n\n`, single-paragraph replies still collapsed.
    //
    // Sentence-level splitting (via `split_sentences`, which is
    // punctuation- and abbreviation-aware) targets ~80 chars per
    // chunk = roughly 10-15 seconds of JA audio. Small enough to
    // start playback fast; large enough to avoid hundreds of tiny
    // synth calls. The Tauri audio worker's jitter buffer
    // (rodio Sink + the prebuffer logic) smooths transitions.
    //
    // If upstream gave us pre-split chunks (LLM streaming sentence
    // by sentence), we honour that and skip the secondary split.
    let turn_id = uuid::Uuid::new_v4().to_string();
    let chunks = compute_tts_chunks(&tts_text, tts_chunks, cfg.tts.streaming);
    let total = chunks.len();
    tracing::info!(
        "avatar: tts streaming={} chunks={} turn_id={}",
        cfg.tts.streaming,
        total,
        turn_id,
    );

    for (i, chunk) in chunks.iter().enumerate() {
        let is_last = i + 1 == total;
        tracing::info!(
            "avatar: TTS chunk {}/{} ({}c, last={is_last}, turn_id={turn_id}): {:?}",
            i + 1, total, chunk.chars().count(),
            safe_prefix(chunk, 120)
        );
        match tts
            .synthesize_with_opts(
                chunk,
                &tts_lang,
                None,
                Some(cfg.tts.speed),
                cfg.tts.voice.as_deref(),
            )
            .await
        {
            Ok(audio) => {
                use base64::Engine;
                let audio_b64 = base64::engine::general_purpose::STANDARD.encode(&audio.audio_bytes);
                // Emit the chat-bubble + expression intro now — right
                // before the FIRST audio frame, so the user sees the
                // text and hears the voice together.
                emit_intro_once(&mut intro_sent);
                bcast(AvatarNotification::Audio {
                    audio: audio_b64,
                    format: audio.format,
                    sample_rate: audio.sample_rate,
                    lip_sync: crate::protocol::LipSyncDataProto {
                        frames: Vec::new(),
                        frame_duration_ms: 30,
                    },
                    turn_id: turn_id.clone(),
                    seq: i as u32,
                    last: is_last,
                });
            }
            Err(e) => {
                tracing::warn!(
                    "avatar: TTS synthesize failed on chunk {}/{}: {e}",
                    i + 1, total
                );
                // Still mark the last chunk so the frontend doesn't
                // wait forever for audio that won't arrive. Surface the
                // intro frames if they never made it out (TTS failed
                // before the first successful chunk).
                if is_last {
                    emit_intro_once(&mut intro_sent);
                    bcast(AvatarNotification::Audio {
                        audio: String::new(),
                        format: "wav".into(),
                        sample_rate: 0,
                        lip_sync: crate::protocol::LipSyncDataProto {
                            frames: Vec::new(),
                            frame_duration_ms: 30,
                        },
                        turn_id: turn_id.clone(),
                        seq: i as u32,
                        last: true,
                    });
                }
            }
        }
    }

    // Safety net: if every TTS call errored before is_last, the intro
    // would never have fired. Emit it now so the chat bubble at least
    // shows the reply text.
    emit_intro_once(&mut intro_sent);
    bcast(AvatarNotification::Idle);
    Ok(subtitle_text)
}

fn handle_avatar_message(msg: &AvatarMessage) {
    match msg {
        AvatarMessage::Touch { hit_area, x, y } => {
            tracing::debug!("avatar: touch on '{hit_area}' at ({x:.0}, {y:.0})");
        }
        AvatarMessage::MotionRequest { group, name } => {
            tracing::debug!("avatar: motion requested ({group}/{name})");
        }
        AvatarMessage::ExpressionRequest { name } => {
            tracing::debug!("avatar: expression requested ({name})");
        }
        AvatarMessage::Ready => {}
    }
}

async fn send_notification(
    socket: &mut WebSocket,
    notification: &AvatarNotification,
) -> Result<()> {
    let json = serde_json::to_string(notification)?;
    socket.send(Message::Text(json.into())).await?;
    Ok(())
}

/// Pop the first complete *paragraph* from `buf`. A paragraph boundary
/// is two consecutive newlines (`\n\n`) — that's the universal "blank
/// line between paragraphs" marker emitted by every LLM we've seen.
///
/// Returns the prefix up to (but not including) the first `\n\n`,
/// trimmed. Leaves the suffix in `buf`. Returns `None` when no `\n\n`
/// is present yet (caller should wait for more text). Empty/whitespace-
/// only paragraphs are skipped silently — `\n\n\n\n` doesn't produce
/// noise frames.
///
/// Paragraph-wise streaming (as opposed to sentence-wise) trades a tiny
/// bit of TTFA for far better prosody: the TTS plans intonation across
/// a whole thought, and the inter-paragraph cold-start gap is naturally
/// large enough (an LLM "double newline" usually signals a topic shift)
/// that listeners forgive it.
/// Decide how to chunk a TTS-bound reply for streaming.
///
/// History: `\n\n` paragraph splitting was tried (task #137) on the
/// theory that "most chat replies are one paragraph anyway." That
/// assumption is wrong — a typical LLM reply is one paragraph of
/// several sentences. Paragraph splitting collapsed to single-shot
/// synth and made the user wait for a 30-60s audio chunk before
/// playback started, AND made VITS-style engines stutter on the
/// long input.
///
/// Sentence-level splitting (via `split_sentences`, which is
/// punctuation- and abbreviation-aware) targets ~80 chars per
/// chunk = roughly 10-15 seconds of JA audio. Small enough to
/// start playback fast; large enough to avoid hundreds of tiny
/// synth calls. The Tauri audio worker's jitter buffer
/// (rodio Sink + the prebuffer logic) smooths transitions.
///
/// If `pre_chunked` already has >1 entries (LLM streaming sentence
/// by sentence emitted them), we honour that and skip our split.
pub fn compute_tts_chunks(
    tts_text: &str,
    pre_chunked: Vec<String>,
    streaming: bool,
) -> Vec<String> {
    // target=40 chars ≈ 1-2 short sentences or 1 long sentence per
    // chunk. With JA ~5 chars/s of speech, that's ~8s of audio per
    // chunk — small enough that the first sound starts fast, large
    // enough to avoid hundreds of synth calls per turn. target=80
    // was too eager: 6 short JA sentences packed into 2 chunks of
    // ~30s each, defeating the streaming intent and pushing each
    // chunk back into VITS's stutter zone.
    const TTS_SENT_TARGET: usize = 40;
    if pre_chunked.len() > 1 {
        return pre_chunked;
    }
    if !streaming {
        return vec![tts_text.to_string()];
    }
    let parts = crate::config::split_sentences(tts_text, TTS_SENT_TARGET);
    if parts.is_empty() {
        vec![tts_text.to_string()]
    } else {
        parts
    }
}

fn pop_first_paragraph(buf: &mut String) -> Option<String> {
    // First try a sentence-boundary pop — that's the granularity we
    // actually want for streaming TTS. Falls back to `\n\n` paragraph
    // boundaries when there's no sentence terminator yet but the
    // upstream stream emitted a paragraph break (rare).
    //
    // Why this matters: NMT (NLLB) returns the full translation as a
    // single stream delta with no `\n\n` markers. Without the
    // sentence-level fallback, the whole reply accumulated in `buf`
    // until the stream ended, then dispatched as ONE chunk via the
    // "leftover" branch in the streaming pipeline. The user heard
    // only the first part of a 30-60s synth before SBV2's attention
    // drifted on the long input.
    if let Some(s) = pop_first_sentence(buf) {
        return Some(s);
    }
    loop {
        let idx = buf.find("\n\n")?;
        let prefix = buf[..idx].trim().to_string();
        let rest = buf[idx + 2..].to_string();
        *buf = rest;
        if !prefix.is_empty() {
            *buf = buf.trim_start_matches('\n').to_string();
            return Some(prefix);
        }
    }
}

/// Drain the first complete sentence from `buf`. Recognises ASCII
/// terminators (`.!?`), CJK terminators (`。！？`), and `\n\n`. A `.`
/// only counts as a sentence end if it's NOT preceded by a known
/// abbreviation (`Mr.`, `e.g.`, etc.) — same abbreviation list as
/// `config::raw_sentences`.
///
/// Returns None if there's no complete sentence yet (the buffer is
/// still being filled by an in-flight stream delta).
fn pop_first_sentence(buf: &mut String) -> Option<String> {
    let s = buf.as_str();
    let bytes = s.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        // Walk by char so we never split a multi-byte codepoint.
        let c = s[i..].chars().next()?;
        let c_len = c.len_utf8();
        let is_end = matches!(c, '.' | '!' | '?' | '。' | '！' | '？' | '\n');
        if is_end {
            // For ASCII `.`, defer if it's part of an abbreviation
            // (Mr., e.g., …) — we'd otherwise emit "Hi Mr" without
            // its trailing context.
            let is_period = c == '.';
            let in_ellipsis = is_period
                && (s.as_bytes().get(i + c_len) == Some(&b'.')
                    || s.as_bytes().get(i.saturating_sub(1)) == Some(&b'.'));
            if in_ellipsis {
                i += c_len;
                continue;
            }
            if is_period {
                // Look back to the start of the word ending here.
                let word_start = s[..i]
                    .char_indices()
                    .rev()
                    .find(|&(_, ch)| ch.is_whitespace())
                    .map(|(idx, ch)| idx + ch.len_utf8())
                    .unwrap_or(0);
                let word = &s[word_start..i];
                if crate::config::is_abbreviation_pub(word) {
                    i += c_len;
                    continue;
                }
                // Decimal: digit . digit
                let prev = s[..i].chars().next_back();
                let next = s[i + c_len..].chars().next();
                if prev.map_or(false, |p| p.is_ascii_digit())
                    && next.map_or(false, |n| n.is_ascii_digit())
                {
                    i += c_len;
                    continue;
                }
            }
            // Found a real sentence end. Pull in trailing close-quotes
            // / parens / CJK closers.
            let mut end = i + c_len;
            while end < bytes.len() {
                let nc = match s[end..].chars().next() {
                    Some(c) => c,
                    None => break,
                };
                if matches!(nc, '"' | '\'' | ')' | ']' | '}' | '」' | '』' | '）') {
                    end += nc.len_utf8();
                } else {
                    break;
                }
            }
            let sentence = s[..end].trim().to_string();
            let remainder = s[end..].trim_start().to_string();
            *buf = remainder;
            if sentence.is_empty() {
                // A leading `\n` with no preceding content — keep going.
                return pop_first_sentence(buf);
            }
            return Some(sentence);
        }
        i += c_len;
    }
    None
}

/// Streaming pipeline. Bcasts initial Expression/Text/Debug, then
/// runs `subagent.translate_stream` while a sidecar task drains
/// completed *paragraphs* and dispatches them to TTS in order. Final
/// chunk gets `last=true`; Idle bcasts when the dispatcher exits.
///
/// Paragraph-wise streaming (vs the older sentence-wise approach):
/// most chat replies fit in a single paragraph, so the dispatcher
/// fires exactly one synth + one Audio frame per reply (effectively
/// single-shot). Long multi-paragraph replies stream paragraph by
/// paragraph — the cold-start gap at each `\n\n` boundary is large
/// enough that listeners forgive it, and intra-paragraph prosody
/// stays intact because the TTS sees the whole thought.
///
/// Order is preserved because (a) the dispatcher consumes from the
/// channel sequentially and (b) `synthesize_with` is awaited inline
/// before the next paragraph is taken — so each Audio frame's `seq`
/// is broadcast in monotonic order.
async fn run_streaming_speak(
    state: &Arc<AvatarWsState>,
    text: &str,
    chat_lang: &str,
    tts_lang: &str,
    expression_mapper: &ExpressionMapper,
    keyword_expr: crate::expression::Live2DExpression,
) -> Result<String> {
    // Strip thinking-trace preamble BEFORE everything else. The bulk
    // analyze() path lets the subagent LLM strip via system prompt,
    // but the streaming path skips analyze() — without this we'd
    // route the agent's "Let me check the weather…" line through
    // both the subtitle and the translator. `prefer_cjk` only checks
    // tts_lang — the agent can leak English thinking regardless of
    // what language the user chats in.
    let prefer_cjk = matches!(tts_lang, "ja" | "zh");
    let stripped = strip_thinking_preamble(text, prefer_cjk);
    if stripped.chars().count() != text.chars().count() {
        tracing::info!(
            "avatar: streaming stripped thinking preamble ({} → {} chars)",
            text.chars().count(),
            stripped.chars().count(),
        );
    }
    let raw_subtitle = expression_mapper.strip_tags(&stripped);
    let subtitle_text = raw_subtitle.clone();
    // Detect the language the agent actually replied in. The companion's
    // `chat_language` setting reflects what the USER typed, but the
    // agent may reply in a different language (typed Chinese →
    // received Chinese, even when chat_language is "en"). Hand the
    // *detected* language to the translator so NLLB tokenises the
    // input through the correct vocabulary slice.
    let detected = detect_source_lang(&subtitle_text);
    let effective_src_lang: &str = detected.unwrap_or(chat_lang);
    if let Some(d) = detected
        && d != chat_lang {
            tracing::info!(
                "avatar: detected reply language={d:?} differs from chat_language={chat_lang:?}; \
                 forwarding {d:?} as NMT src",
            );
        }
    let turn_id = uuid::Uuid::new_v4().to_string();

    // Translation backend label (iter 14): "llm" / "nmt" / "none".
    // Captured here from the live config so all Debug emissions in
    // this turn agree on which path actually ran.
    let translation_path: &'static str = {
        let cfg = state.config.load();
        if chat_lang == tts_lang {
            "none"
        } else if matches!(
            cfg.subagent.translator.backend,
            crate::translator::TranslatorBackendKind::Http,
        ) {
            "nmt"
        } else {
            "llm"
        }
    };

    let bcast = |frame: AvatarNotification| {
        let _ = state.event_tx.send(AvatarEvent::Frame(frame));
    };

    // NOTE: the "intro" frames (Expression + Text + Debug) are NOT sent
    // up front. They're emitted by the dispatcher task immediately before
    // the *first* Audio frame leaves — so the chat-bubble / subtitle land
    // *with* the sound instead of ~10-20s ahead of it (the agent reply
    // returns long before TTS produces audio). The frontend's HTTP
    // fallback (handleSendChat) still backstops a dropped WS connection.

    // Channel: (paragraph_text, is_final). is_final marks the last
    // paragraph so the dispatcher knows to stamp the Audio frame's
    // last=true. A None paragraph with is_final=true is allowed for
    // the empty-trailer case.
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<(Option<String>, bool)>();
    let dispatcher_state = state.clone();
    let dispatcher_lang = tts_lang.to_string();
    let dispatcher_turn = turn_id.clone();
    let dispatcher_subtitle = subtitle_text.clone();
    let dispatcher_expr_name = keyword_expr.name.clone();
    let dispatcher_expr_intensity = keyword_expr.intensity;
    let dispatcher_translation_path = translation_path.to_string();
    let dispatcher = tokio::spawn(async move {
        let mut seq: u32 = 0;
        // The macro below mutates `intro_sent` to track "have I fired
        // the intro frames yet?"; the LAST assignment is flagged as
        // "never read" by the lint because the dispatcher exits right
        // after — but the assignment is load-bearing for the
        // *intermediate* invocations.
        #[allow(unused_assignments)]
        let mut intro_sent = false;
        // Expression + chat-bubble text + (empty) debug, exactly once,
        // just before the first Audio frame — keeps text synced to sound.
        macro_rules! emit_intro_once {
            () => {{
                if !intro_sent {
                    #[allow(unused_assignments)]  // last-iteration write isn't read
                    { intro_sent = true; }
                    let _ = dispatcher_state.event_tx.send(AvatarEvent::Frame(
                        AvatarNotification::Expression {
                            name: dispatcher_expr_name.clone(),
                            intensity: dispatcher_expr_intensity,
                            duration_ms: None,
                        },
                    ));
                    let _ = dispatcher_state.event_tx.send(AvatarEvent::Frame(
                        AvatarNotification::Text { content: dispatcher_subtitle.clone() },
                    ));
                    let _ = dispatcher_state.event_tx.send(AvatarEvent::Frame(
                        AvatarNotification::Debug {
                            chat_text: dispatcher_subtitle.clone(),
                            spoken_text: String::new(),
                            expression: dispatcher_expr_name.clone(),
                            subagent_used: true,
                            translation_path: dispatcher_translation_path.clone(),
                        },
                    ));
                }
            }};
        }
        macro_rules! send_empty_terminator {
            () => {
                let _ = dispatcher_state.event_tx.send(AvatarEvent::Frame(
                    AvatarNotification::Audio {
                        audio: String::new(),
                        format: "wav".into(),
                        sample_rate: 0,
                        lip_sync: crate::protocol::LipSyncDataProto {
                            frames: Vec::new(),
                            frame_duration_ms: 30,
                        },
                        turn_id: dispatcher_turn.clone(),
                        seq,
                        last: true,
                    },
                ));
            };
        }
        while let Some((sentence_opt, is_final)) = rx.recv().await {
            let cleaned: String = match sentence_opt {
                Some(s) => strip_emoji_and_markdown_for_tts(&s),
                None => String::new(),
            };
            if cleaned.trim().is_empty() {
                if is_final {
                    // No more audio coming — still surface the reply text
                    // (even if TTS produced nothing), then close the turn
                    // so the frontend can drop "speaking" state.
                    emit_intro_once!();
                    send_empty_terminator!();
                    break;
                } else {
                    continue;
                }
            }
            tracing::info!(
                "avatar: TTS streaming chunk seq={seq} ({}c, last={is_final}): {:?}",
                cleaned.chars().count(),
                safe_prefix(&cleaned, 100),
            );
            // Resnap the TTS each chunk so a hot-swap mid-stream picks
            // up the new manager on the next sentence. Cheap (Arc clone).
            // Re-read the live config too so a CFM-steps change in UI
            // applies to the very next sentence — no engine restart.
            let tts_snap = dispatcher_state.tts.load_full();
            let cfg_snap = dispatcher_state.config.load();

            // Helper to broadcast one AudioOutput as a WS frame at a
            // given seq + last flag. Closes over dispatcher state.
            // Returns the next seq value to use.
            let broadcast_chunk = |audio: crate::tts_server::AudioOutput,
                                   chunk_seq: u32,
                                   last: bool| {
                use base64::Engine;
                let audio_b64 = base64::engine::general_purpose::STANDARD
                    .encode(&audio.audio_bytes);
                let _ = dispatcher_state.event_tx.send(AvatarEvent::Frame(
                    AvatarNotification::Audio {
                        audio: audio_b64,
                        format: audio.format,
                        sample_rate: audio.sample_rate,
                        lip_sync: crate::protocol::LipSyncDataProto {
                            frames: Vec::new(),
                            frame_duration_ms: 30,
                        },
                        turn_id: dispatcher_turn.clone(),
                        seq: chunk_seq,
                        last,
                    },
                ));
            };

            // Paragraph-wise synthesis: one blocking synth per paragraph.
            // No SSE intra-paragraph streaming — paragraphs are large
            // enough that listeners forgive the cold-start gap at each
            // `\n\n` boundary, and full-paragraph synthesis keeps the
            // TTS planning prosody across the whole thought.
            match tts_snap
                .synthesize_with_opts(
                    &cleaned,
                    &dispatcher_lang,
                    None,
                    Some(cfg_snap.tts.speed),
                    cfg_snap.tts.voice.as_deref(),
                )
                .await
            {
                Ok(audio) => {
                    emit_intro_once!();
                    broadcast_chunk(audio, seq, is_final);
                    seq += 1;
                }
                Err(e) => {
                    tracing::warn!(
                        "avatar: paragraph-streaming TTS failed for chunk seq={seq}: {e}"
                    );
                    if is_final {
                        emit_intro_once!();
                        send_empty_terminator!();
                    }
                }
            }
            if is_final {
                break;
            }
        }
        // Safety net: if the channel closed without ever delivering a
        // usable paragraph (shouldn't happen), still surface the reply
        // and close the turn so the frontend doesn't hang in "speaking".
        if !intro_sent {
            emit_intro_once!();
            send_empty_terminator!();
        }
    });

    // Drive the streaming translation. Each delta append triggers a
    // paragraph-pop loop; complete paragraphs flow to the dispatcher.
    let subagent_snap = state.subagent.load_full();
    let subagent = subagent_snap
        .as_ref()
        .expect("run_streaming_speak called without a subagent — process_speak gates this");
    let translation_buf = std::sync::Arc::new(std::sync::Mutex::new(String::new()));
    let buf_clone = translation_buf.clone();
    let tx_clone = tx.clone();
    let full = subagent
        .translate_stream(&subtitle_text, Some(effective_src_lang), tts_lang, move |delta| {
            let mut buf = buf_clone.lock().unwrap();
            buf.push_str(delta);
            while let Some(paragraph) = pop_first_paragraph(&mut buf) {
                let _ = tx_clone.send((Some(paragraph), false));
            }
        })
        .await;

    // Send any remaining buffer as the final chunk.
    let leftover = translation_buf.lock().unwrap().trim().to_string();
    if !leftover.is_empty() {
        let _ = tx.send((Some(leftover), true));
    } else {
        // Empty leftover: signal completion via empty terminator.
        let _ = tx.send((None, true));
    }
    drop(tx);
    let _ = dispatcher.await;

    // The early Debug frame above was sent with an empty `spoken_text`
    // because streaming hadn't produced the translation yet. Now that
    // it has, re-emit Debug with the full spoken-language text so the
    // chat-bubble "details" panel reflects what was actually voiced.
    // translate_stream returns Some(full_translation) on success, None
    // when it couldn't stream (and fell back to chunk-translate, or
    // failed outright). On None we leave spoken_text blank — the panel
    // just stays empty rather than lying.
    let spoken_full = full.as_deref().map(|s| s.trim().to_string()).unwrap_or_default();
    if !spoken_full.is_empty() {
        bcast(AvatarNotification::Debug {
            chat_text: subtitle_text.clone(),
            spoken_text: spoken_full,
            expression: keyword_expr.name.clone(),
            subagent_used: true,
            translation_path: translation_path.to_string(),
        });
    }

    bcast(AvatarNotification::Idle);
    Ok(subtitle_text)
}

#[cfg(test)]
mod strip_thinking_tests {
    use super::strip_thinking_preamble;

    #[test]
    fn user_reported_leak_en_thinking_then_zh_reply() {
        // The exact text shape the user saw: English thinking trace
        // followed by a CJK reply. cross-lang flag on (tts=ja/zh).
        let raw = "The user is asking about today's weather and saying they \
                   feel a bit cold. Let me check the weather for their location. \
                   Based on USER.md, they're in America/Chicago timezone. \
                   Let me get the weather.才51度，难怪你觉得冷。都快三更半夜了。";
        let out = strip_thinking_preamble(raw, true);
        assert!(
            out.starts_with("才51度"),
            "expected leading CJK after strip, got: {out:?}"
        );
        assert!(
            !out.to_lowercase().contains("let me"),
            "thinking markers should be gone, got: {out:?}"
        );
    }

    #[test]
    fn cjk_quoted_in_thinking_then_zh_reply() {
        // 2026-05-14 user-reported leak: thinking sentence STARTS with
        // a CJK term in quotes (`"明天" (tomorrow) likely means...`).
        // The previous heuristic missed this because the first char
        // was CJK and the "leading-latin-char-count" gate never fired.
        // New sentence-level mostly-Latin drop must catch it.
        let raw = "\"明天\" (tomorrow) likely means May 14 during the day \
                   (since it's technically already May 14 but nighttime) or May 15. \
                   I'll check the weather with a 2-day forecast to cover both.\
                   明天也就是5月14号，白天挺舒服的～ sunny，最高14°C，不下雨。\
                   后天（15号）就别想晒太阳了，一整天都在下雨";
        let out = strip_thinking_preamble(raw, true);
        assert!(
            out.starts_with("明天也就是5月14号"),
            "expected clean Chinese reply, got: {out:?}"
        );
        assert!(
            !out.contains("likely means"),
            "English commentary should be gone, got: {out:?}"
        );
        assert!(
            !out.contains("I'll check"),
            "intent statement should be gone, got: {out:?}"
        );
    }

    #[test]
    fn cjk_reply_with_loanwords_preserved() {
        // The user's real reply contained " sunny" and "14°C" — Latin
        // chunks inside a Chinese sentence. The 20% threshold must
        // tolerate these (sentence is still majority CJK).
        let raw = "明天也就是5月14号，白天挺舒服的～ sunny，最高14°C，不下雨。";
        let out = strip_thinking_preamble(raw, true);
        assert_eq!(out, raw, "loanwords should not trigger the Latin drop");
    }

    #[test]
    fn zh_chat_doesnt_break_strip_when_tts_is_ja() {
        // When the user chats in Chinese and TTS is Japanese, the agent
        // can still leak English thinking. prefer_cjk only checks tts,
        // not chat — so the strip must still fire.
        let raw = "Let me think about this. 今日も元気ですか？";
        let out = strip_thinking_preamble(raw, true);
        assert_eq!(out, "今日も元気ですか？");
    }

    #[test]
    fn pure_en_thinking_then_en_reply() {
        // No CJK to fall through to — must catch via marker matching.
        let raw = "Let me check the weather. It is 51 degrees outside.";
        let out = strip_thinking_preamble(raw, false);
        assert_eq!(out, "It is 51 degrees outside.");
    }

    #[test]
    fn no_preamble_passes_through_unchanged() {
        let raw = "Hello there! How are you today?";
        let out = strip_thinking_preamble(raw, false);
        assert_eq!(out, raw);
    }

    #[test]
    fn cjk_only_input_passes_through() {
        let raw = "こんにちは、元気ですか？今日もいい天気ですね。";
        let out = strip_thinking_preamble(raw, true);
        assert_eq!(out, raw);
    }

    #[test]
    fn empty_input_yields_empty() {
        assert_eq!(strip_thinking_preamble("", false), "");
        assert_eq!(strip_thinking_preamble("   \n  ", false), "");
    }

    #[test]
    fn multiple_thinking_sentences_all_stripped() {
        let raw = "Let me check. Looking at the context, this is what I see. \
                   Based on the user's request, I need to respond. \
                   The actual reply starts here.";
        let out = strip_thinking_preamble(raw, false);
        assert_eq!(out, "The actual reply starts here.");
    }

    #[test]
    fn short_latin_prefix_preserved_when_prefer_cjk() {
        // "OK!" + JA reply — the Latin prefix is too short (3 chars) to
        // trigger the CJK fallback heuristic (threshold 16). Should pass
        // through.
        let raw = "OK! 分かりました。";
        let out = strip_thinking_preamble(raw, true);
        assert_eq!(out, raw.trim_start());
    }

    #[test]
    fn webhook_msg_marker_stripped() {
        let raw = "webhook_msg_abc123 was sent. The real reply text.";
        let out = strip_thinking_preamble(raw, false);
        assert_eq!(out, "The real reply text.");
    }

    #[test]
    fn does_not_split_inside_multibyte_codepoint() {
        // Regression guard for byte-vs-char-boundary slicing. A CJK
        // terminator (3-byte 。) at the boundary must not panic.
        let raw = "Let me try。実際の返答です。";
        let out = strip_thinking_preamble(raw, false);
        // The English "Let me try" has no ASCII terminator, so the whole
        // thing is dropped (treated as one runaway thinking sentence).
        // Acceptable degradation: we never panic.
        let _ = out;
    }
}

#[cfg(test)]
mod pop_first_paragraph_tests {
    use super::pop_first_paragraph;

    #[test]
    fn empty_buffer_returns_none() {
        let mut buf = String::new();
        assert!(pop_first_paragraph(&mut buf).is_none());
        assert!(buf.is_empty());
    }

    #[test]
    fn no_double_newline_returns_none() {
        // Pre-2026-05-18: pop_first_paragraph ONLY split on `\n\n`,
        // so this case returned None and the entire turn waited for an
        // explicit paragraph break that NMT never emitted. That bug
        // caused the user to hear only one chunk of a multi-sentence
        // reply (the "leftover" fell out as one chunk at the end of
        // the stream). Updated to test the new semantics: with a
        // sentence terminator present, we pop the sentence, leaving
        // the partial trailing fragment in `buf` for the next delta.
        let mut buf = String::from("Hello there.\nNo paragraph break here yet");
        let p = pop_first_paragraph(&mut buf).expect("sentence present");
        assert_eq!(p, "Hello there.");
        assert_eq!(buf, "No paragraph break here yet");
    }

    #[test]
    fn no_terminator_at_all_returns_none() {
        // No sentence terminator AND no `\n\n` → wait for more delta.
        let mut buf = String::from("a partial fragment of text");
        assert!(pop_first_paragraph(&mut buf).is_none());
        assert_eq!(buf, "a partial fragment of text");
    }

    #[test]
    fn pops_first_paragraph_leaves_rest() {
        let mut buf = String::from("First paragraph.\n\nSecond paragraph still going");
        let p = pop_first_paragraph(&mut buf).expect("paragraph present");
        assert_eq!(p, "First paragraph.");
        assert_eq!(buf, "Second paragraph still going");
    }

    #[test]
    fn pops_when_buffer_ends_with_double_newline() {
        // Translator just wrote the separator; nothing after it yet.
        let mut buf = String::from("Lone paragraph here.\n\n");
        let p = pop_first_paragraph(&mut buf).expect("paragraph present");
        assert_eq!(p, "Lone paragraph here.");
        assert!(buf.is_empty());
    }

    #[test]
    fn consecutive_separators_handled_cleanly() {
        // \n\n\n\n is two stacked separators — both should drain in one
        // call without emitting an empty paragraph.
        let mut buf = String::from("Para A.\n\n\n\nPara B starts");
        let p = pop_first_paragraph(&mut buf).expect("paragraph present");
        assert_eq!(p, "Para A.");
        // After popping, the remaining `\n\n` separator was already
        // consumed (drained by the empty-skip loop) and we're left with
        // the continuation.
        assert_eq!(buf, "Para B starts");
    }

    #[test]
    fn trims_paragraph_whitespace() {
        let mut buf = String::from("   leading spaces.\n\nnext");
        let p = pop_first_paragraph(&mut buf).expect("paragraph present");
        assert_eq!(p, "leading spaces.");
        assert_eq!(buf, "next");
    }

    #[test]
    fn whitespace_only_paragraph_is_skipped() {
        // `\n\n   \n\n` should drain to "" then pop the next real one.
        let mut buf = String::from("\n\n   \n\nreal content here\n\nmore");
        let p = pop_first_paragraph(&mut buf).expect("paragraph present");
        assert_eq!(p, "real content here");
        assert_eq!(buf, "more");
    }

    #[test]
    fn multibyte_paragraph_safe() {
        // CJK content + a `\n\n` boundary must not panic on byte slicing.
        let mut buf = String::from("こんにちは、元気ですか？\n\n今日もいい天気ですね。");
        let p = pop_first_paragraph(&mut buf).expect("paragraph present");
        assert_eq!(p, "こんにちは、元気ですか？");
        assert_eq!(buf, "今日もいい天気ですね。");
    }
}

#[cfg(test)]
mod compute_tts_chunks_tests {
    use super::compute_tts_chunks;

    // The original bug: a 4-sentence single-paragraph JA reply collapsed
    // to one TTS chunk because the splitter looked for \n\n and there
    // were none. The user heard only ~30s of audio, then nothing.
    // With sentence-level splitting via split_sentences(target=80) this
    // single-paragraph input MUST produce multiple chunks.
    #[test]
    fn long_single_paragraph_multi_sentence_japanese_yields_multiple_chunks() {
        let reply = concat!(
            "こんにちは、今日はとても良い天気ですね。",
            "公園を散歩していると、桜の花がきれいに咲いていて、心が癒されました。",
            "近くのカフェで温かいコーヒーを飲みながら、しばらく本を読んでいました。",
            "夕方になると、空がきれいなオレンジ色に染まり、思わず写真を撮りたくなりました。",
            "今夜は家で美味しい料理を作って、ゆっくり過ごす予定です。",
            "明日もきっと素敵な一日になりますように。",
        );
        // No \n\n in the input — the old paragraph splitter would have
        // returned exactly one chunk, the bug we hit on 2026-05-18.
        assert!(!reply.contains("\n\n"));
        let chunks = compute_tts_chunks(reply, Vec::new(), true);
        assert!(
            chunks.len() >= 3,
            "expected >=3 chunks for a 6-sentence input, got {}: {:?}",
            chunks.len(), chunks,
        );
    }

    // Even with paragraph breaks, the sentence-splitter should still
    // pick natural boundaries — never producing fewer chunks than the
    // paragraph splitter would have.
    #[test]
    fn multi_paragraph_english_yields_multiple_chunks() {
        let reply = "Hello, how are you today? The weather is great.\n\n\
                     Let me tell you about Mochi the cat. She lives by the seaside.\n\n\
                     Every day she chases butterflies and falls asleep in the sun.";
        let chunks = compute_tts_chunks(reply, Vec::new(), true);
        assert!(
            chunks.len() >= 3,
            "expected >=3 chunks for a 3-paragraph input, got {}: {:?}",
            chunks.len(), chunks,
        );
    }

    // Streaming OFF must always be one chunk regardless of content.
    #[test]
    fn streaming_off_yields_single_chunk() {
        let reply = "Sentence one. Sentence two. Sentence three. Sentence four.";
        let chunks = compute_tts_chunks(reply, Vec::new(), false);
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0], reply);
    }

    // Pre-chunked input from upstream (LLM streaming sentence-by-sentence)
    // must be honoured verbatim — no re-splitting.
    #[test]
    fn pre_chunked_is_honoured_verbatim() {
        let pre = vec![
            "Sentence A.".to_string(),
            "Sentence B.".to_string(),
            "Sentence C.".to_string(),
        ];
        let chunks = compute_tts_chunks("ignored", pre.clone(), true);
        assert_eq!(chunks, pre);
    }

    // Trivial input must still produce one chunk, not zero.
    #[test]
    fn short_input_yields_at_least_one_chunk() {
        let chunks = compute_tts_chunks("Hi.", Vec::new(), true);
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0], "Hi.");
    }

    // Empty after trim must still return one chunk — caller decides
    // whether to skip the speak path entirely.
    #[test]
    fn empty_after_trim_still_yields_one_chunk() {
        let chunks = compute_tts_chunks("   \n  ", Vec::new(), true);
        // split_sentences returns empty for whitespace-only; we fall back.
        assert_eq!(chunks.len(), 1);
    }
}

#[cfg(test)]
mod pop_first_sentence_tests {
    use super::pop_first_paragraph;

    // The bug we just hit on 2026-05-18: NMT streams the full JA
    // translation as one delta with no `\n\n`. The old pop logic
    // returned None, so the whole text dispatched as one chunk via
    // the leftover branch. Now it should pop sentence by sentence.
    #[test]
    fn drains_multiple_japanese_sentences_without_paragraph_breaks() {
        let mut buf = String::from(
            "こんにちは。今日はとても良い天気ですね。\
             公園を散歩していると、桜の花がきれいに咲いていました。\
             夕方になると、空がきれいなオレンジ色に染まりました。"
        );
        let mut popped = Vec::new();
        while let Some(s) = pop_first_paragraph(&mut buf) {
            popped.push(s);
        }
        assert!(
            popped.len() >= 3,
            "expected >=3 sentences popped, got {}: {:?}",
            popped.len(), popped
        );
        // Buf should be empty (all sentences ended with terminator).
        assert!(buf.trim().is_empty(), "leftover in buf: {:?}", buf);
    }

    #[test]
    fn drains_english_sentences_without_paragraph_breaks() {
        let mut buf = String::from(
            "Hello. How are you? I am fine. Today is sunny."
        );
        let mut popped = Vec::new();
        while let Some(s) = pop_first_paragraph(&mut buf) {
            popped.push(s);
        }
        assert_eq!(popped.len(), 4, "got: {:?}", popped);
    }

    #[test]
    fn defers_on_abbreviation() {
        // `Mr.` must NOT pop as a sentence by itself — wait for the
        // real terminator. Otherwise the streaming dispatcher would
        // synthesize "Hi Mr" alone.
        let mut buf = String::from("Hi Mr. Smith. Welcome.");
        let first = pop_first_paragraph(&mut buf).expect("should pop the full first sentence");
        assert_eq!(first, "Hi Mr. Smith.");
        let second = pop_first_paragraph(&mut buf).expect("welcome");
        assert_eq!(second, "Welcome.");
        assert!(pop_first_paragraph(&mut buf).is_none());
    }

    #[test]
    fn defers_on_incomplete_sentence() {
        // No terminator yet → return None, wait for the next delta.
        let mut buf = String::from("This is not done yet, hold on");
        assert!(pop_first_paragraph(&mut buf).is_none());
        // Buffer untouched.
        assert_eq!(buf, "This is not done yet, hold on");
    }

    #[test]
    fn handles_decimal_points() {
        // `3.14` is not a sentence end. `is enough.` IS.
        let mut buf = String::from("Pi is roughly 3.14 which is enough. Next.");
        let first = pop_first_paragraph(&mut buf).expect("first");
        assert_eq!(first, "Pi is roughly 3.14 which is enough.");
        let second = pop_first_paragraph(&mut buf).expect("second");
        assert_eq!(second, "Next.");
    }

    #[test]
    fn paragraph_break_still_works_when_no_sentence_terminator() {
        // A delta with explicit `\n\n` but no `.`/`。` should still pop
        // (the paragraph-break fallback).
        let mut buf = String::from("Some text here\n\nmore content");
        let first = pop_first_paragraph(&mut buf).expect("para");
        assert_eq!(first, "Some text here");
        assert!(pop_first_paragraph(&mut buf).is_none());
    }
}
