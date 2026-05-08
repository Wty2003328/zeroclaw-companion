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

    let (expression, tts_text_opt) = if let Some(ref subagent) = state.subagent {
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
                let expr = AvatarSubagent::to_expression(&analysis, &keyword_expr);
                (expr, translated)
            }
            None => (keyword_expr, None),
        }
    } else {
        (keyword_expr, None)
    };

    let subtitle_text = expression_mapper.strip_tags(text);
    let tts_text = if chat_lang == tts_lang {
        subtitle_text.clone()
    } else {
        match tts_text_opt {
            Some(t) if !t.trim().is_empty() => expression_mapper.strip_tags(&t),
            _ => {
                tracing::warn!(
                    "avatar: chat_language={chat_lang} != tts_language={tts_lang} \
                     but subagent returned no translation; speaking original text"
                );
                subtitle_text.clone()
            }
        }
    };

    tracing::info!(
        "avatar: process_speak (chat_lang={chat_lang} → tts_lang={tts_lang}, \
         subagent_used={subagent_used}, chat_chars={}, spoken_chars={}, \
         subscribers={})",
        subtitle_text.chars().count(),
        tts_text.chars().count(),
        state.event_tx.receiver_count(),
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

    // ONE TTS call per turn — not one per connected client.
    match state.tts.synthesize_with(&tts_text, &tts_lang).await {
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
            });
        }
        Err(e) => {
            tracing::warn!("avatar: TTS synthesize failed ({e}), skipping audio");
        }
    }

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
