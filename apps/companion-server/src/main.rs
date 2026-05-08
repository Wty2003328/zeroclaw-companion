//! companion-server — entry point for the zeroclaw companion.
//!
//! Lifecycle:
//! 1. Load `companion.toml` (or use defaults).
//! 2. Health-check the upstream zeroclaw daemon.
//! 3. Build the avatar subsystem (subagent + TTS port + WS state).
//! 4. Spawn the SSE bridge: subscribe to zeroclaw `/api/events`, forward
//!    `agent.reply` events to the avatar broadcast channel.
//! 5. Auto-start the configured TTS server (e.g. the Asuna v4 wrapper).
//! 6. Serve the companion UI + WS routes on its own HTTP port.
//!
//! The fork's old approach was: edit zeroclaw, rebuild zeroclaw, ship a
//! patched zeroclaw. The new approach is: zeroclaw stays vanilla, the
//! companion runs as a sidecar and consumes zeroclaw's public API.

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use axum::routing::get;
use axum::Router;
use futures_util::StreamExt;
use tokio::sync::broadcast;
use tower_http::cors::{Any, CorsLayer};
use tower_http::services::ServeDir;
use tower_http::trace::TraceLayer;

use companion_avatar::{
    AnimeTtsManager, AvatarConfig, AvatarEvent, AvatarSubagent, AvatarWsState,
    handle_ws_avatar,
};
use companion_core::{AgentEvent, CompanionConfig, ZeroclawClient};
use companion_pulse::{PulseConfig, PulseSubsystem};

mod pulse_api;
mod state;
use state::AppState;

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();
    let config_path = config_path()?;
    tracing::info!("companion: loading config from {}", config_path.display());
    let cfg = CompanionConfig::load(&config_path)?;

    // ── 1. Talk to upstream zeroclaw ──────────────────────────────
    let zc = ZeroclawClient::new(&cfg.zeroclaw)?;
    match zc.health().await {
        Ok(true) => tracing::info!("companion: zeroclaw at {} is up", cfg.zeroclaw.url),
        Ok(false) | Err(_) => tracing::warn!(
            "companion: zeroclaw at {} unreachable — chat features will be limited until it comes up",
            cfg.zeroclaw.url
        ),
    }

    // ── 2. Build the avatar subsystem (if enabled) ───────────────
    let avatar_state = build_avatar(&cfg, zc.clone()).await?;

    // ── 3. SSE bridge: zeroclaw /api/events → avatar broadcast ────
    if let Some(ref state) = avatar_state {
        let event_tx = state.event_tx.clone();
        let zc_clone = zc.clone();
        tokio::spawn(async move {
            run_sse_bridge(zc_clone, event_tx).await;
        });
    }

    // ── 4. Build the Pulse subsystem (if enabled) ────────────────
    let pulse_state = build_pulse(&cfg).await?;

    // ── 5. Build the axum app ─────────────────────────────────────
    let app_state = AppState {
        avatar: avatar_state,
        pulse: pulse_state,
        zeroclaw: Arc::new(zc),
    };

    let mut router = Router::new()
        .route("/health", get(handle_health))
        .route("/api/status", get(handle_status))
        .route("/api/chat", axum::routing::post(handle_chat));

    if app_state.avatar.is_some() {
        let avatar_state = Arc::clone(app_state.avatar.as_ref().unwrap());
        router = router.route(
            "/ws/avatar",
            get(handle_ws_avatar).with_state(avatar_state),
        );
    }

    if let Some(ref pulse) = app_state.pulse {
        let pulse_routes = pulse_api::routes().with_state(Arc::clone(pulse));
        router = router.nest("/api/pulse", pulse_routes);
    }

    // Serve the companion web bundle (Vite build output).
    //
    // The frontend is a React SPA with client-side routing (BrowserRouter
    // — `/avatar`, `/pulse`, etc. are handled by React, not by files on
    // disk). For any path that doesn't match a real asset, fall through
    // to `index.html` so React can take over. Without this, hitting
    // `/avatar` directly in the browser would 404.
    //
    // ServeDir's `not_found_service` does serve index.html bytes but
    // preserves the 404 status, which most browsers refuse to render.
    // Use a custom axum fallback that returns 200 OK with the index body.
    let web_dir = resolve_web_dist(&cfg.server.web_dist_dir);
    if web_dir.exists() {
        tracing::info!("companion: serving web from {}", web_dir.display());
        let index_path = web_dir.join("index.html");
        let serve_dir = ServeDir::new(&web_dir).fallback(spa_fallback(index_path));
        router = router.fallback_service(serve_dir);
    } else {
        tracing::warn!(
            "companion: web bundle not found at {}; UI will 404 until you `npm run build` in web/",
            web_dir.display()
        );
    }

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let app = router
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(app_state);

    // ── 5. Bind ───────────────────────────────────────────────────
    let addr = format!("{}:{}", cfg.server.host, cfg.server.port);
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .with_context(|| format!("failed to bind {addr}"))?;
    tracing::info!("companion: listening on http://{addr}");
    tracing::info!("            • avatar UI:  http://{addr}/avatar");
    tracing::info!("            • WS avatar:  ws://{addr}/ws/avatar");
    tracing::info!("            • health:     http://{addr}/health");

    axum::serve(listener, app).await?;
    Ok(())
}

// ── helpers ───────────────────────────────────────────────────────

fn init_tracing() {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,companion=debug"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .compact()
        .init();
}

fn config_path() -> Result<PathBuf> {
    if let Ok(env) = std::env::var("COMPANION_CONFIG") {
        return Ok(PathBuf::from(env));
    }
    let cwd = std::env::current_dir()?;
    let local = cwd.join("companion.toml");
    if local.exists() {
        return Ok(local);
    }
    if let Some(home) = directories::UserDirs::new() {
        let home_cfg = home.home_dir().join(".zeroclaw-companion").join("companion.toml");
        return Ok(home_cfg);
    }
    Ok(local)
}

fn resolve_web_dist(configured: &Option<String>) -> PathBuf {
    if let Some(p) = configured {
        return PathBuf::from(p);
    }
    // Look for ./web/dist relative to the binary, then to CWD.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let candidate = parent.join("web").join("dist");
            if candidate.exists() {
                return candidate;
            }
        }
    }
    std::env::current_dir()
        .unwrap_or_default()
        .join("web")
        .join("dist")
}

async fn build_avatar(
    cfg: &CompanionConfig,
    zeroclaw_client: ZeroclawClient,
) -> Result<Option<Arc<AvatarWsState>>> {
    // Avatar config lives under [avatar] in companion.toml. We deserialize
    // here (companion-core kept it as a Value to stay decoupled).
    let avatar_cfg: AvatarConfig = serde_json::from_value(cfg.avatar.clone())
        .unwrap_or_default();
    if !avatar_cfg.enabled {
        tracing::info!("companion: avatar disabled in config");
        return Ok(None);
    }

    // Optional subagent (expression analysis + translation). When
    // `use_zeroclaw_webhook = true` we pass the upstream client so the
    // backend can route through zeroclaw and reuse its (already-decrypted)
    // provider key.
    let subagent = if avatar_cfg.subagent.enabled {
        let zc_for_subagent = if avatar_cfg.subagent.use_zeroclaw_webhook {
            Some(zeroclaw_client.clone())
        } else {
            None
        };
        match AvatarSubagent::new(&avatar_cfg.subagent, zc_for_subagent) {
            Ok(s) => {
                tracing::info!(
                    "companion: avatar subagent ready (backend={})",
                    if avatar_cfg.subagent.use_zeroclaw_webhook {
                        "zeroclaw-webhook"
                    } else {
                        "openai-compatible"
                    }
                );
                Some(Arc::new(s))
            }
            Err(e) => {
                tracing::warn!("companion: avatar subagent init failed; using keyword fallback: {e}");
                None
            }
        }
    } else {
        None
    };

    // TTS port. Auto-start the configured server in the background so the
    // avatar UI can still load if the TTS server is down.
    let tts = Arc::new(
        AnimeTtsManager::new(&avatar_cfg.tts).context("companion: avatar TTS init failed")?,
    );
    if avatar_cfg.tts.auto_start {
        let tts_clone = Arc::clone(&tts);
        tokio::spawn(async move {
            if let Err(e) = tts_clone.start_server().await {
                tracing::warn!("companion: TTS auto-start failed: {e}");
            }
        });
    }

    let (event_tx, _event_rx) = broadcast::channel(64);

    tracing::info!(
        "companion: avatar enabled (chat_lang={}, tts_lang={}, engine={})",
        avatar_cfg.chat_language,
        avatar_cfg.tts.language,
        avatar_cfg.tts.engine,
    );

    Ok(Some(Arc::new(AvatarWsState {
        config: avatar_cfg,
        event_tx,
        subagent,
        tts,
    })))
}

async fn build_pulse(cfg: &CompanionConfig) -> Result<Option<Arc<PulseSubsystem>>> {
    let pulse_cfg: PulseConfig = serde_json::from_value(cfg.pulse.clone()).unwrap_or_default();
    if !pulse_cfg.enabled {
        tracing::info!("companion: pulse disabled in config");
        return Ok(None);
    }
    let subsystem = PulseSubsystem::start(&pulse_cfg)
        .await
        .context("companion: pulse init failed")?;
    Ok(Some(Arc::new(subsystem)))
}

/// Subscribe to zeroclaw's SSE event stream for OBSERVABILITY only.
///
/// We deliberately do NOT use SSE to drive the avatar pipeline:
/// (1) zeroclaw v0.7.5's /api/events doesn't broadcast the reply text
///     anyway (only agent_start / llm_request / agent_end metadata),
///     so any avatar-Speak we emitted here would have empty text, and
/// (2) the load-bearing path is /api/chat → process_speak, which runs
///     subagent + TTS exactly once per turn. Re-emitting via SSE would
///     risk doubling that work and producing two simultaneous Asunas.
///
/// Reconnects on failure with exponential backoff capped at 30s.
async fn run_sse_bridge(zc: ZeroclawClient, _avatar_tx: broadcast::Sender<AvatarEvent>) {
    let mut backoff = 1u64;
    loop {
        match zc.events().await {
            Ok(stream) => {
                tracing::info!("companion: SSE bridge connected (observability only)");
                backoff = 1;
                tokio::pin!(stream);
                while let Some(ev) = stream.next().await {
                    // Log unusual events at debug; AgentReply (if a future
                    // zeroclaw ever emits one) is logged but NOT forwarded.
                    match ev {
                        AgentEvent::AgentReply { ref text, .. } => {
                            tracing::debug!(
                                "companion sse: agent.reply ({} chars) — ignored, /api/chat is the speak path",
                                text.len()
                            );
                        }
                        AgentEvent::AgentToken { .. } => {}
                        AgentEvent::Other { ref raw } => {
                            tracing::debug!("companion sse: {}", raw);
                        }
                    }
                }
                tracing::warn!("companion: SSE stream ended; reconnecting");
            }
            Err(e) => {
                tracing::warn!("companion: SSE bridge connect failed: {e}; backoff={backoff}s");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(30);
    }
}

/// Return a tower service that always responds 200 with `index.html`'s
/// bytes. ServeDir uses this as its fallback when no real asset exists
/// at the requested path — exactly the SPA behavior React Router needs.
fn spa_fallback(
    index_path: std::path::PathBuf,
) -> impl tower::Service<
    axum::extract::Request,
    Response = axum::response::Response,
    Error = std::convert::Infallible,
    Future = std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<axum::response::Response, std::convert::Infallible>> + Send>,
    >,
> + Clone
+ Send
+ 'static {
    use axum::response::IntoResponse;
    let index_path = std::sync::Arc::new(index_path);
    tower::service_fn(move |_req: axum::extract::Request| {
        let p = index_path.clone();
        Box::pin(async move {
            let body = tokio::fs::read(p.as_path()).await.unwrap_or_default();
            Ok::<_, std::convert::Infallible>(
                ([(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], body)
                    .into_response(),
            )
        })
            as std::pin::Pin<
                Box<
                    dyn std::future::Future<
                            Output = Result<axum::response::Response, std::convert::Infallible>,
                        > + Send,
                >,
            >
    })
}

async fn handle_health() -> &'static str {
    "ok"
}

async fn handle_status(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let zc_up = state.zeroclaw.health().await.unwrap_or(false);
    axum::Json(serde_json::json!({
        "ok": true,
        "zeroclaw_up": zc_up,
        "avatar_enabled": state.avatar.is_some(),
        "pulse_enabled": state.pulse.is_some(),
    }))
}

#[derive(serde::Deserialize)]
struct ChatRequest {
    message: String,
}

#[derive(serde::Serialize)]
struct ChatResponse {
    reply: String,
}

/// Forward a user message to upstream zeroclaw, return the reply, AND
/// fan it out to any connected avatar viewer so the avatar speaks.
///
/// We learned the hard way during e2e that zeroclaw v0.7.5's reply text
/// only comes back from `POST /webhook` synchronously — it is NOT
/// broadcast on `/api/events` SSE. So this handler is the load-bearing
/// path for driving the avatar pipeline; the SSE bridge is only useful
/// for observability events (tool calls, agent_start/end timing, …).
async fn handle_chat(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<ChatRequest>,
) -> Result<axum::Json<ChatResponse>, (axum::http::StatusCode, String)> {
    if req.message.trim().is_empty() {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "message must not be empty".into(),
        ));
    }
    tracing::info!("companion: /api/chat → zeroclaw ({}c)", req.message.len());
    let started = std::time::Instant::now();
    let reply = state
        .zeroclaw
        .send_chat(&req.message)
        .await
        .map_err(|e| {
            let elapsed = started.elapsed().as_secs();
            // Distinguish timeout from generic errors so the UI can
            // render a useful message instead of "502 Bad Gateway".
            // reqwest's timeout error includes "operation timed out" /
            // "deadline has elapsed" depending on platform; check both.
            let msg = e.to_string();
            let is_timeout = msg.contains("timed out") || msg.contains("deadline");
            tracing::error!(
                "companion: zeroclaw chat failed after {}s ({}): {e}",
                elapsed,
                if is_timeout { "TIMEOUT" } else { "ERROR" }
            );
            if is_timeout {
                (
                    axum::http::StatusCode::GATEWAY_TIMEOUT,
                    format!(
                        "zeroclaw didn't respond within {}s. The agent may be \
                         running a long tool loop (web search etc.). Bump \
                         [zeroclaw] timeout_secs in companion.toml.",
                        elapsed
                    ),
                )
            } else {
                (
                    axum::http::StatusCode::BAD_GATEWAY,
                    format!("zeroclaw error: {e}"),
                )
            }
        })?;
    tracing::info!(
        "companion: /api/chat ← reply ({}c, {}s)",
        reply.len(),
        started.elapsed().as_secs()
    );

    // Run subagent + TTS ONCE here, then fan rendered frames out to
    // every connected /ws/avatar viewer. Doing the work per-client
    // would multiply subagent token cost and TTS load by the number of
    // connected viewers and make all of them play overlapping audio.
    if let Some(ref avatar) = state.avatar {
        let avatar_clone = std::sync::Arc::clone(avatar);
        let reply_clone = reply.clone();
        // Spawn so we don't block the /api/chat response on TTS time.
        tokio::spawn(async move {
            if let Err(e) = companion_avatar::process_speak(&avatar_clone, &reply_clone).await {
                tracing::warn!("companion: process_speak failed: {e}");
            }
        });
    }
    Ok(axum::Json(ChatResponse { reply }))
}
