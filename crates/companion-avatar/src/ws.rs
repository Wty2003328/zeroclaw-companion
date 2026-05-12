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

/// Split a long agent reply into translation-sized chunks.
///
/// Used by process_speak when the bulk subagent.analyze() call didn't
/// produce a translation (long inputs blow past z.ai's 60s connection
/// budget). Per-paragraph translation keeps each LLM call small enough
/// to land reliably, and we sequence them to stay under the per-key
/// rate limit.
///
/// Strategy:
/// - Split on blank lines (\n\n) first — most agent replies use them
///   between bullets / numbered items / paragraphs.
/// - For paragraphs longer than `MAX`, split further by sentence
///   terminator so no single LLM call exceeds the safe input size.
fn split_for_translation(text: &str) -> Vec<String> {
    const MAX_CHARS_PER_CHUNK: usize = 280;
    let mut out: Vec<String> = Vec::new();
    for paragraph in text.split("\n\n") {
        let p = paragraph.trim();
        if p.is_empty() {
            continue;
        }
        if p.chars().count() <= MAX_CHARS_PER_CHUNK {
            out.push(p.to_string());
            continue;
        }
        // Long paragraph: subdivide on sentence terminators.
        let parts = crate::config::split_sentences(p, 16);
        if parts.is_empty() {
            // Hard split if sentence splitter found nothing.
            let mut buf = String::new();
            for ch in p.chars() {
                buf.push(ch);
                if buf.chars().count() >= MAX_CHARS_PER_CHUNK {
                    out.push(std::mem::take(&mut buf));
                }
            }
            if !buf.trim().is_empty() {
                out.push(buf);
            }
        } else {
            out.extend(parts);
        }
    }
    out
}
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

    let keyword_expr = expression_mapper.detect(text);
    let mut motion_to_send: Option<AvatarNotification> = None;
    let mut subagent_used = false;

    // Skip subagent when chat == tts language and the user opted into
    // the fast-path: the "translation" would be a no-op and keyword
    // detection picks a sensible expression. Saves ~5–10s per turn.
    let need_translation = chat_lang != tts_lang;
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

    bcast(AvatarNotification::Expression {
        name: expression.name.clone(),
        intensity: expression.intensity,
        duration_ms: expression.duration_ms,
    });

    if let Some(motion) = motion_to_send {
        bcast(motion);
    }

    bcast(AvatarNotification::Text {
        content: subtitle_text.clone(),
    });

    bcast(AvatarNotification::Debug {
        chat_text: subtitle_text.clone(),
        spoken_text: tts_text.clone(),
        expression: expression.name,
        subagent_used,
    });

    // Empty tts_text means we deliberately skipped speech for this turn
    // (cross-language without a translation). Emit Idle and bail out so
    // the frontend doesn't receive an Audio frame containing zero bytes
    // synthesized by the TTS server.
    if tts_text.trim().is_empty() {
        tracing::info!("avatar: process_speak skipping TTS (no spoken text)");
        bcast(AvatarNotification::Idle);
        return Ok(subtitle_text);
    }

    // Sentence-chunked synthesis when streaming is enabled. All chunks
    // of one turn share `turn_id` so the frontend can queue them
    // sequentially without confusing them for stale audio. The first
    // chunk arrives ~1–2s after the agent reply lands instead of
    // waiting for the full reply to render — the perceived latency
    // win for long replies.
    //
    // If the per-paragraph translator produced multiple chunks already,
    // we feed those to TTS directly (each paragraph is its own
    // synthesis unit). For a single bulk translation we still apply
    // sentence-level chunking so streaming kicks in on long replies.
    let turn_id = uuid::Uuid::new_v4().to_string();
    let chunks: Vec<String> = if tts_chunks.len() > 1 {
        tts_chunks
    } else if cfg.tts.streaming {
        let parts = crate::config::split_sentences(
            &tts_text,
            cfg.tts.streaming_min_chars,
        );
        if parts.is_empty() { vec![tts_text.clone()] } else { parts }
    } else {
        vec![tts_text.clone()]
    };
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
        match tts.synthesize_with(chunk, &tts_lang).await {
            Ok(audio) => {
                use base64::Engine;
                let audio_b64 = base64::engine::general_purpose::STANDARD.encode(&audio.audio_bytes);
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
                // wait forever for audio that won't arrive.
                if is_last {
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

    bcast(AvatarNotification::Idle);
    Ok(subtitle_text)
}

#[cfg(test)]
mod split_for_translation_tests {
    use super::split_for_translation;

    #[test]
    fn paragraphs_under_max_pass_through() {
        let v = split_for_translation("First.\n\nSecond.\n\nThird.");
        assert_eq!(v, vec!["First.", "Second.", "Third."]);
    }

    #[test]
    fn long_paragraphs_subdivide_by_sentence() {
        let p = "Sentence one is here. ".repeat(20); // ~440c, no \n\n
        let v = split_for_translation(&p);
        assert!(v.len() > 1, "expected multiple chunks for 440c paragraph");
        for chunk in &v {
            assert!(chunk.chars().count() <= 320, "chunk too long: {chunk:?}");
        }
    }

    #[test]
    fn empty_input_yields_empty() {
        assert!(split_for_translation("").is_empty());
        assert!(split_for_translation("   \n\n   ").is_empty());
    }

    #[test]
    fn mixed_short_and_long_paragraphs() {
        let long = "X is the case. ".repeat(30);
        let input = format!("Short.\n\n{long}\n\nAlso short.");
        let v = split_for_translation(&input);
        assert_eq!(v.first().map(String::as_str), Some("Short."));
        assert_eq!(v.last().map(String::as_str), Some("Also short."));
        assert!(v.len() > 2, "long para should be subdivided");
    }
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

/// Pop the first complete sentence(s) from `buf` once at least
/// `target` chars have accumulated and a *real* sentence terminator
/// appears. "Real" excludes a `.` that's a decimal (`3.14`) or part
/// of an ellipsis (`...`) — those would otherwise cut a number or a
/// trailing-off phrase mid-stride and hand the TTS a fragment with
/// falling sentence-final intonation. CJK `。！？` and ASCII `!?` are
/// always real ends. Returns the drained text (trimmed) on success.
fn pop_first_sentence(buf: &mut String, target: usize) -> Option<String> {
    let chars: Vec<char> = buf.chars().collect();
    // Map char index → byte offset of the char *after* that char, so
    // we can slice `buf` cleanly.
    let mut byte_after: Vec<usize> = Vec::with_capacity(chars.len());
    {
        let mut acc = 0usize;
        for c in &chars {
            acc += c.len_utf8();
            byte_after.push(acc);
        }
    }
    for i in 0..chars.len() {
        let ch = chars[i];
        if i + 1 < target {
            // Not enough text behind this position yet.
            continue;
        }
        let is_real_end = match ch {
            '。' | '！' | '？' | '!' | '?' => true,
            '\n' => true,
            '.' => {
                let prev = if i > 0 { Some(chars[i - 1]) } else { None };
                let next = chars.get(i + 1).copied();
                let decimal = prev.is_some_and(|c| c.is_ascii_digit())
                    && next.is_some_and(|c| c.is_ascii_digit());
                let ellipsis = prev == Some('.') || next == Some('.');
                !(decimal || ellipsis)
            }
            _ => false,
        };
        if !is_real_end {
            continue;
        }
        // Pull in trailing close-quotes/parens so they ride along.
        let mut end_char = i + 1;
        while let Some(&c) = chars.get(end_char) {
            if matches!(c, '"' | '\'' | ')' | ']' | '}' | '」' | '』' | '）') {
                end_char += 1;
            } else {
                break;
            }
        }
        let end_byte = byte_after[end_char - 1];
        let sentence = buf[..end_byte].to_string();
        let rest = buf[end_byte..].to_string();
        *buf = rest;
        return Some(sentence.trim().to_string()).filter(|s| !s.is_empty());
    }
    None
}

/// Streaming pipeline. Bcasts initial Expression/Text/Debug, then
/// runs `subagent.translate_stream` while a sidecar task drains
/// completed sentences and dispatches them to TTS in order. Final
/// chunk gets `last=true`; Idle bcasts when the dispatcher exits.
///
/// Order is preserved because (a) the dispatcher consumes from the
/// channel sequentially and (b) `synthesize_with` is awaited inline
/// before the next sentence is taken — so each Audio frame's `seq`
/// is broadcast in monotonic order.
async fn run_streaming_speak(
    state: &Arc<AvatarWsState>,
    text: &str,
    chat_lang: &str,
    tts_lang: &str,
    expression_mapper: &ExpressionMapper,
    keyword_expr: crate::expression::Live2DExpression,
) -> Result<String> {
    let raw_subtitle = expression_mapper.strip_tags(text);
    let subtitle_text = raw_subtitle.clone();
    let turn_id = uuid::Uuid::new_v4().to_string();
    let min_len = state.config.load().tts.streaming_min_chars;

    let bcast = |frame: AvatarNotification| {
        let _ = state.event_tx.send(AvatarEvent::Frame(frame));
    };

    // NOTE: the "intro" frames (Expression + Text + Debug) are NOT sent
    // up front. They're emitted by the dispatcher task immediately before
    // the *first* Audio frame leaves — so the chat-bubble / subtitle land
    // *with* the sound instead of ~10-20s ahead of it (the agent reply
    // returns long before TTS produces audio). The frontend's HTTP
    // fallback (handleSendChat) still backstops a dropped WS connection.

    // Channel: (sentence_text, is_final). is_final marks the last
    // sentence so the dispatcher knows to stamp the Audio frame's
    // last=true. A None sentence with is_final=true is allowed for
    // the empty-trailer case.
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<(Option<String>, bool)>();
    let dispatcher_state = state.clone();
    let dispatcher_lang = tts_lang.to_string();
    let dispatcher_turn = turn_id.clone();
    let dispatcher_subtitle = subtitle_text.clone();
    let dispatcher_expr_name = keyword_expr.name.clone();
    let dispatcher_expr_intensity = keyword_expr.intensity;
    let dispatcher = tokio::spawn(async move {
        let mut seq: u32 = 0;
        let mut intro_sent = false;
        // Expression + chat-bubble text + (empty) debug, exactly once,
        // just before the first Audio frame — keeps text synced to sound.
        macro_rules! emit_intro_once {
            () => {{
                if !intro_sent {
                    intro_sent = true;
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
            let tts_snap = dispatcher_state.tts.load_full();
            match tts_snap.synthesize_with(&cleaned, &dispatcher_lang).await {
                Ok(audio) => {
                    use base64::Engine;
                    let audio_b64 = base64::engine::general_purpose::STANDARD
                        .encode(&audio.audio_bytes);
                    emit_intro_once!(); // text rides just ahead of the first chunk
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
                            seq,
                            last: is_final,
                        },
                    ));
                }
                Err(e) => {
                    tracing::warn!(
                        "avatar: streaming TTS failed for chunk seq={seq}: {e}"
                    );
                    if is_final {
                        emit_intro_once!();
                        send_empty_terminator!();
                    }
                }
            }
            seq += 1;
            if is_final {
                break;
            }
        }
        // Safety net: if the channel closed without ever delivering a
        // usable sentence (shouldn't happen), still surface the reply and
        // close the turn so the frontend doesn't hang in "speaking".
        if !intro_sent {
            emit_intro_once!();
            send_empty_terminator!();
        }
    });

    // Drive the streaming translation. Each delta append triggers a
    // sentence-pop loop; complete sentences flow to the dispatcher.
    let subagent_snap = state.subagent.load_full();
    let subagent = subagent_snap
        .as_ref()
        .expect("run_streaming_speak called without a subagent — process_speak gates this");
    let translation_buf = std::sync::Arc::new(std::sync::Mutex::new(String::new()));
    let buf_clone = translation_buf.clone();
    let tx_clone = tx.clone();
    let full = subagent
        .translate_stream(&subtitle_text, tts_lang, move |delta| {
            let mut buf = buf_clone.lock().unwrap();
            buf.push_str(delta);
            while let Some(sentence) = pop_first_sentence(&mut buf, min_len) {
                let _ = tx_clone.send((Some(sentence), false));
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
        });
    }

    bcast(AvatarNotification::Idle);
    Ok(subtitle_text)
}
