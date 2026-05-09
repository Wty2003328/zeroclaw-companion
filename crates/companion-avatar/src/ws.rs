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
pub struct AvatarWsState {
    pub config: AvatarConfig,
    pub event_tx: broadcast::Sender<AvatarEvent>,
    pub subagent: Option<Arc<AvatarSubagent>>,
    pub tts: Arc<AnimeTtsManager>,
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
    let model_info = AvatarNotification::ModelInfo {
        model_url: state
            .config
            .model
            .model_dir
            .clone()
            .unwrap_or_else(|| "/live2d/models/haru/Haru.model3.json".to_string()),
        scale: state.config.model.scale,
        anchor: state.config.model.anchor.clone(),
        default_expression: state.config.model.default_expression.clone(),
    };
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
    let chat_lang = state.config.chat_language.clone();
    let tts_lang = state.config.tts.language.clone();
    let expression_mapper = ExpressionMapper::new(&state.config.expressions);

    let keyword_expr = expression_mapper.detect(text);
    let mut motion_to_send: Option<AvatarNotification> = None;
    let mut subagent_used = false;

    // Skip subagent when chat == tts language and the user opted into
    // the fast-path: the "translation" would be a no-op and keyword
    // detection picks a sensible expression. Saves ~5–10s per turn.
    let need_translation = chat_lang != tts_lang;
    let should_run_subagent = state.subagent.is_some()
        && (need_translation || !state.config.subagent.only_when_translating);

    // Skip the bulk subagent.analyze call when the input is large —
    // those are exactly the cases where z.ai times out / 429s on the
    // single big request. Per-paragraph translation handles them
    // reliably below. We still want analyze() for SHORT replies
    // because it picks the expression and translates in one call.
    const BULK_ANALYZE_MAX_CHARS: usize = 500;
    let bulk_eligible = text.chars().count() <= BULK_ANALYZE_MAX_CHARS;

    let (expression, tts_text_opt, clean_chat_opt) = if should_run_subagent && bulk_eligible {
        let subagent = state.subagent.as_ref().unwrap();
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
        if state.subagent.is_some() && should_run_subagent && !bulk_eligible {
            tracing::info!(
                "avatar: bulk subagent skipped — input {}c exceeds {} threshold; \
                 routing to per-paragraph translation directly",
                text.chars().count(), BULK_ANALYZE_MAX_CHARS
            );
            // Mark subagent as in-use so the Debug frame reflects that
            // translation was attempted, even though the bulk path was
            // bypassed for sizing reasons.
            subagent_used = true;
        } else if state.subagent.is_some() && !need_translation {
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
    // Build the per-paragraph TTS chunk list.
    //
    // - same-language: chunk the subtitle by sentence (existing path).
    // - cross-language: ALWAYS try translating each paragraph in its own
    //   subagent call. The single-call subagent.analyze() already gave
    //   us a translated_text candidate; we use it for short replies,
    //   but for long replies it tends to time out / rate-limit at z.ai
    //   (1KB+ inputs blow past the 60s connection budget). Per-paragraph
    //   translation keeps each LLM call small and reliable, so the user
    //   actually hears audio for long answers like "5 tips for sleep."
    let tts_chunks: Vec<String> = if chat_lang == tts_lang {
        // No translation needed — the streaming-TTS chunker handles
        // the rest below.
        vec![subtitle_text.clone()]
    } else {
        // Reuse the bulk-call translation IF it looks complete. We've
        // observed z.ai returning translations truncated mid-codepoint
        // when the JSON wrapper exceeds max_tokens — those parse as
        // valid strings but only cover ~half the reply. Heuristic:
        // accept the bulk translation only if it's at least 35% as long
        // as the cleaned chat text. Otherwise fall through to
        // per-paragraph translation so the user hears the full answer.
        let bulk_translated = tts_text_opt
            .as_deref()
            .map(|t| expression_mapper.strip_tags(t))
            .filter(|t| !t.trim().is_empty());
        let bulk_complete = bulk_translated.as_ref().map(|t| {
            let translated_chars = t.chars().count();
            let source_chars = subtitle_text.chars().count().max(1);
            // Japanese typically packs more meaning per char than
            // English (factor ~0.5), so 0.35 is a generous floor.
            (translated_chars as f32) / (source_chars as f32) >= 0.35
        }).unwrap_or(false);
        if bulk_complete {
            let t = bulk_translated.unwrap();
            tracing::info!(
                "avatar: using bulk subagent translation ({} chars)",
                t.chars().count()
            );
            vec![t]
        } else if let Some(ref subagent) = state.subagent {
            // Fall back to per-paragraph translation. Sequential to
            // avoid bursting z.ai's per-key rate limit.
            let paragraphs = split_for_translation(&subtitle_text);
            tracing::info!(
                "avatar: bulk translation missing; per-paragraph translating {} chunks",
                paragraphs.len()
            );
            let mut out = Vec::with_capacity(paragraphs.len());
            for (i, para) in paragraphs.iter().enumerate() {
                if i > 0 {
                    // Stay under z.ai's per-minute rate limit. 800ms
                    // between calls + the call itself keeps us under
                    // ~1 RPS, which the coding-paas endpoint tolerates.
                    tokio::time::sleep(std::time::Duration::from_millis(800)).await;
                }
                if let Some(t) = subagent.translate_chunk(para, &tts_lang).await {
                    let cleaned = expression_mapper.strip_tags(&t);
                    if !cleaned.trim().is_empty() {
                        out.push(cleaned);
                    }
                } else {
                    tracing::warn!(
                        "avatar: per-paragraph translate failed for chunk {}/{}",
                        i + 1, paragraphs.len()
                    );
                }
            }
            out
        } else {
            tracing::warn!(
                "avatar: no subagent configured for cross-language; SKIPPING TTS"
            );
            Vec::new()
        }
    };
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
    } else if state.config.tts.streaming {
        let parts = crate::config::split_sentences(
            &tts_text,
            state.config.tts.streaming_min_chars,
        );
        if parts.is_empty() { vec![tts_text.clone()] } else { parts }
    } else {
        vec![tts_text.clone()]
    };
    let total = chunks.len();
    tracing::info!(
        "avatar: tts streaming={} chunks={} turn_id={}",
        state.config.tts.streaming,
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
        match state.tts.synthesize_with(chunk, &tts_lang).await {
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
