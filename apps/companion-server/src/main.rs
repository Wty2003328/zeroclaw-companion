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

mod characters;
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
    // Pulse summarize reuses whichever backend the user already
    // configured for the avatar subagent — direct LLM call or via
    // zeroclaw's webhook — so they don't have to set up two paths.
    let pulse_summarizer = build_pulse_summarizer(&cfg, zc.clone());
    let pulse_state = build_pulse(&cfg, pulse_summarizer).await?;

    // ── 5. Build the axum app ─────────────────────────────────────
    let app_state = AppState {
        avatar: avatar_state,
        pulse: pulse_state,
        zeroclaw: Arc::new(zc),
        config_path: config_path.clone(),
    };

    // Shutdown channel: GET /api/shutdown sends () through this so
    // the main loop knows to wind down (graceful TTS stop, then exit).
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    let shutdown_tx = std::sync::Arc::new(tokio::sync::Mutex::new(Some(shutdown_tx)));

    let mut router = Router::new()
        .route("/health", get(handle_health))
        .route("/api/status", get(handle_status))
        .route("/api/chat", axum::routing::post(handle_chat))
        .route("/api/config", get(handle_get_config))
        .route(
            "/api/config/subagent",
            axum::routing::post(handle_post_subagent_override),
        )
        .route(
            "/api/config/avatar",
            axum::routing::post(handle_post_avatar_override),
        )
        .route("/api/models", get(handle_list_models))
        .route(
            "/api/characters",
            get(handle_list_characters).post(handle_upsert_character),
        )
        .route(
            "/api/characters/active",
            axum::routing::post(handle_set_active_character),
        )
        .route(
            "/api/characters/{id}",
            axum::routing::delete(handle_delete_character),
        )
        .route(
            "/api/characters/{id}/attachments",
            get(handle_list_character_attachments),
        )
        .route(
            "/api/characters/{id}/attachments/{file}",
            get(handle_get_character_attachment)
                .put(handle_put_character_attachment)
                .delete(handle_delete_character_attachment),
        )
        .route(
            "/api/shutdown",
            axum::routing::post({
                let shutdown_tx = shutdown_tx.clone();
                move || async move {
                    tracing::info!("companion: /api/shutdown requested");
                    if let Some(tx) = shutdown_tx.lock().await.take() {
                        let _ = tx.send(());
                    }
                    axum::http::StatusCode::ACCEPTED
                }
            }),
        );

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

    // Clone the avatar handle for the shutdown path BEFORE moving
    // app_state into the router — the router takes ownership of
    // app_state via .with_state.
    let avatar_for_shutdown = app_state.avatar.clone();

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

    let server = axum::serve(listener, app);
    tokio::select! {
        // The HTTP server itself exits — shouldn't happen under normal
        // operation, but if it does we still want to stop TTS.
        result = server => {
            tracing::info!("companion: HTTP server exited: {:?}", result.as_ref().map(|_| "ok"));
        }
        // Tauri (or external user) hit POST /api/shutdown — graceful
        // shutdown path. We've moved the tx out, so this completes.
        _ = shutdown_rx => {
            tracing::info!("companion: shutdown signal received via /api/shutdown");
        }
        // Ctrl+C in a console run.
        _ = tokio::signal::ctrl_c() => {
            tracing::info!("companion: Ctrl+C received");
        }
    }

    // Graceful TTS shutdown: POST /shutdown to the Python wrapper, wait
    // up to 8s for clean exit (which runs torch.cuda.empty_cache() +
    // sync), fall back to kill. Without this, leaving the model running
    // leaks fragmented VRAM into whatever graphics workload runs next.
    if let Some(avatar) = avatar_for_shutdown {
        tracing::info!("companion: stopping TTS server before exit");
        if let Err(e) = avatar.tts.stop_server().await {
            tracing::warn!("companion: TTS stop_server returned {e}");
        }
    }
    tracing::info!("companion: bye");
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

async fn build_pulse(
    cfg: &CompanionConfig,
    summarizer: Option<Arc<companion_pulse::Summarizer>>,
) -> Result<Option<Arc<PulseSubsystem>>> {
    let pulse_cfg: PulseConfig = serde_json::from_value(cfg.pulse.clone()).unwrap_or_default();
    if !pulse_cfg.enabled {
        tracing::info!("companion: pulse disabled in config");
        return Ok(None);
    }
    let subsystem = PulseSubsystem::start(&pulse_cfg, summarizer)
        .await
        .context("companion: pulse init failed")?;
    Ok(Some(Arc::new(subsystem)))
}

/// Build the Summarizer used by Pulse's `POST /items/{id}/summarize`.
///
/// We mirror the avatar subagent's backend choice so the user only
/// configures one path:
///
/// * `subagent.use_zeroclaw_webhook = true` → tunnel through zeroclaw's
///   `/webhook` (no API key needed on this machine).
/// * otherwise → direct OpenAI-compatible call using
///   `[avatar.subagent.llm]`.
///
/// Returns `None` if the avatar config can't be deserialized or the
/// chosen backend fails to construct. In that case `/items/{id}/summarize`
/// reports 503; the rest of Pulse keeps working.
fn build_pulse_summarizer(
    cfg: &CompanionConfig,
    zc: companion_core::zeroclaw::ZeroclawClient,
) -> Option<Arc<companion_pulse::Summarizer>> {
    let avatar_cfg: AvatarConfig = serde_json::from_value(cfg.avatar.clone()).ok()?;
    if avatar_cfg.subagent.use_zeroclaw_webhook {
        tracing::info!("companion: pulse summarize ready (backend=zeroclaw-webhook)");
        Some(Arc::new(companion_pulse::Summarizer::Zeroclaw(zc)))
    } else {
        match companion_core::llm::LlmClient::new(&avatar_cfg.subagent.llm) {
            Ok(c) => {
                tracing::info!(
                    "companion: pulse summarize ready (backend=openai-compatible, model={})",
                    avatar_cfg.subagent.llm.model,
                );
                Some(Arc::new(companion_pulse::Summarizer::Llm(c)))
            }
            Err(e) => {
                tracing::warn!(
                    "companion: pulse summarize unavailable (LLM init failed: {e})"
                );
                None
            }
        }
    }
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

/// Read-only snapshot of the loaded companion configuration so the
/// Settings page can render what's actually running. Sensitive fields
/// (api keys) are redacted.
async fn handle_get_config(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let avatar = state.avatar.as_ref().map(|a| {
        let cfg = &a.config;
        serde_json::json!({
            "enabled": cfg.enabled,
            "chat_language": cfg.chat_language,
            "tts": {
                "engine": cfg.tts.engine,
                "language": cfg.tts.language,
                "voice": cfg.tts.voice,
                "api_url": cfg.tts.api_url,
                "speed": cfg.tts.speed,
                "launch_command": cfg.tts.launch_command,
                "reference_audio": cfg.tts.reference_audio,
                "reference_text": cfg.tts.reference_text,
                "reference_language": cfg.tts.reference_language,
                "model_path": cfg.tts.model_path,
                "gpu_device": cfg.tts.gpu_device,
            },
            "subagent": {
                "enabled": cfg.subagent.enabled,
                "only_when_translating": cfg.subagent.only_when_translating,
                "use_zeroclaw_webhook": cfg.subagent.use_zeroclaw_webhook,
                "streaming": cfg.subagent.streaming,
                "llm_model": cfg.subagent.llm.model,
                "llm_base_url": cfg.subagent.llm.base_url,
                "timeout_secs": cfg.subagent.timeout_secs,
                // api_key intentionally redacted
                "llm_api_key_set": cfg.subagent.llm.api_key.is_some()
                    || cfg.subagent.llm.api_key_env.is_some(),
            },
            "model": {
                "model_dir": cfg.model.model_dir,
                "default_expression": cfg.model.default_expression,
                "scale": cfg.model.scale,
                "anchor": cfg.model.anchor,
            },
        })
    });
    axum::Json(serde_json::json!({
        "avatar": avatar,
        "zeroclaw_url": state.zeroclaw.health().await.ok().map(|_| "ok"),
    }))
}

/// List Live2D models installed under `<web_dist_dir>/live2d/models/`.
/// Each subdirectory is a model; we look for an entry-point JSON
/// (Cubism 4 `*.model3.json` first, then Cubism 2 `*.model.json` or
/// `model*.json`) to construct the URL the frontend can load.
async fn handle_list_models(
    _state: axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    // Look in the same directory the static-file server uses. When
    // launched from the workspace root via the wrapper, that's
    // `./web/dist/live2d/models/`. We don't store the resolved path
    // in AppState yet, so we re-derive it from cwd here — safe because
    // companion-server (sidecar or standalone) is always launched
    // from a known-cwd ancestor.
    let dist = std::env::current_dir()
        .map(|cwd| cwd.join("web").join("dist"))
        .unwrap_or_default();
    let models_dir = dist.join("live2d").join("models");

    let mut out: Vec<serde_json::Value> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&models_dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if !p.is_dir() {
                continue;
            }
            let dir_name = p.file_name().and_then(|s| s.to_str()).unwrap_or("").to_string();
            if dir_name.is_empty() {
                continue;
            }
            // Prefer Cubism 4 entry, then Cubism 2 conventions.
            let mut entry_file: Option<String> = None;
            let mut format = "cubism2";
            if let Ok(files) = std::fs::read_dir(&p) {
                let mut all: Vec<String> = files
                    .flatten()
                    .filter_map(|f| f.file_name().to_str().map(|s| s.to_string()))
                    .collect();
                all.sort();
                if let Some(f) = all.iter().find(|s| s.ends_with(".model3.json")) {
                    entry_file = Some(f.clone());
                    format = "cubism4";
                } else if let Some(f) = all
                    .iter()
                    .find(|s| s.ends_with(".model.json") || s.starts_with("model"))
                {
                    entry_file = Some(f.clone());
                }
            }
            if let Some(f) = entry_file {
                let url = format!("/live2d/models/{dir_name}/{f}");
                out.push(serde_json::json!({
                    "id": dir_name,
                    "name": dir_name,
                    "modelUrl": url,
                    "format": format,
                }));
            }
        }
    }
    axum::Json(serde_json::json!({ "models": out }))
}

// ── Character management ────────────────────────────────────────

async fn handle_list_characters(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::response::Result<axum::Json<characters::CharactersFile>, (axum::http::StatusCode, String)> {
    let path = characters::characters_path(&state.config_path);
    characters::load(&path)
        .map(axum::Json)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

async fn handle_upsert_character(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<characters::Character>,
) -> axum::response::Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    if req.id.trim().is_empty() {
        return Err((axum::http::StatusCode::BAD_REQUEST, "id required".into()));
    }
    let path = characters::characters_path(&state.config_path);
    let mut file = characters::load(&path)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    if let Some(existing) = file.characters.iter_mut().find(|c| c.id == req.id) {
        *existing = req;
    } else {
        file.characters.push(req);
    }
    characters::save(&path, &file)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(axum::http::StatusCode::OK)
}

#[derive(serde::Deserialize)]
struct ActivateCharacterReq {
    id: String,
}

async fn handle_set_active_character(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<ActivateCharacterReq>,
) -> axum::response::Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    let path = characters::characters_path(&state.config_path);
    let mut file = characters::load(&path)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    // Empty id allowed — clears active.
    if !req.id.is_empty() && !file.characters.iter().any(|c| c.id == req.id) {
        return Err((axum::http::StatusCode::NOT_FOUND, format!("no character with id {}", req.id)));
    }
    file.active_id = req.id;
    characters::save(&path, &file)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(axum::http::StatusCode::OK)
}

async fn handle_delete_character(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> axum::response::Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    let path = characters::characters_path(&state.config_path);
    let mut file = characters::load(&path)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let before = file.characters.len();
    file.characters.retain(|c| c.id != id);
    if file.characters.len() == before {
        return Err((axum::http::StatusCode::NOT_FOUND, format!("no character with id {id}")));
    }
    if file.active_id == id {
        file.active_id.clear();
    }
    characters::save(&path, &file)
        .map_err(|e| (axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(axum::http::StatusCode::OK)
}

// ── Character attachments ────────────────────────────────────────
//
// On-disk markdown bundle per character. Lives at
// `<config-dir>/characters/<id>/*.md` and is loaded on every chat
// turn. The user can edit either through the Characters page UI
// (these endpoints) or directly with their own editor — both produce
// the same file on disk so changes round-trip cleanly.

async fn handle_list_character_attachments(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    // We don't validate `id` against the roster — listing a non-existent
    // character's dir is harmless (returns []), and lets the UI render
    // the section before save.
    let attachments = characters::read_attachments(&state.config_path, &id);
    let shaped: Vec<_> = attachments
        .into_iter()
        .map(|(name, body)| {
            serde_json::json!({
                "name": name,
                "size": body.len(),
            })
        })
        .collect();
    Ok(axum::Json(serde_json::json!({ "attachments": shaped })))
}

async fn handle_get_character_attachment(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((id, file)): axum::extract::Path<(String, String)>,
) -> Result<axum::Json<serde_json::Value>, (axum::http::StatusCode, String)> {
    if !attachment_filename_ok(&file) {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "attachment name must be a single .md filename, no slashes / dots".into(),
        ));
    }
    let path = characters::character_dir(&state.config_path, &id).join(&file);
    let body = std::fs::read_to_string(&path)
        .map_err(|e| (axum::http::StatusCode::NOT_FOUND, e.to_string()))?;
    Ok(axum::Json(
        serde_json::json!({ "name": file, "body": body }),
    ))
}

#[derive(serde::Deserialize)]
struct PutAttachmentReq {
    body: String,
}

async fn handle_put_character_attachment(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((id, file)): axum::extract::Path<(String, String)>,
    axum::Json(req): axum::Json<PutAttachmentReq>,
) -> Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    if !attachment_filename_ok(&file) {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "attachment name must be a single .md filename, no slashes / dots".into(),
        ));
    }
    let dir = characters::character_dir(&state.config_path, &id);
    std::fs::create_dir_all(&dir).map_err(|e| {
        (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            format!("create dir: {e}"),
        )
    })?;
    let path = dir.join(&file);
    std::fs::write(&path, req.body).map_err(|e| {
        (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            format!("write {}: {e}", path.display()),
        )
    })?;
    Ok(axum::http::StatusCode::OK)
}

async fn handle_delete_character_attachment(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((id, file)): axum::extract::Path<(String, String)>,
) -> Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    if !attachment_filename_ok(&file) {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            "attachment name must be a single .md filename, no slashes / dots".into(),
        ));
    }
    let path = characters::character_dir(&state.config_path, &id).join(&file);
    match std::fs::remove_file(&path) {
        Ok(_) => Ok(axum::http::StatusCode::OK),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Ok(axum::http::StatusCode::OK)
        }
        Err(e) => Err((axum::http::StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

/// Reject anything that isn't a single safe `*.md` filename. We refuse
/// path separators and `..` so a malicious file name can't escape the
/// per-character directory.
fn attachment_filename_ok(name: &str) -> bool {
    if name.is_empty() || name.len() > 128 {
        return false;
    }
    if name.contains('/') || name.contains('\\') || name.contains("..") {
        return false;
    }
    name.to_ascii_lowercase().ends_with(".md")
}

// ─────────────────────────────────────────────────────────────────

#[derive(serde::Deserialize)]
struct SubagentOverrideRequest {
    /// `true` → route through zeroclaw's webhook (slow, no key needed).
    /// `false` → direct LLM call (fast, needs api_key).
    use_zeroclaw_webhook: Option<bool>,
    /// Direct-LLM API key. If empty string, treated as "clear the override".
    api_key: Option<String>,
    model: Option<String>,
    base_url: Option<String>,
    timeout_secs: Option<u64>,
}

/// Persist the user's subagent settings choice to
/// `companion.runtime.json` (sibling of companion.toml). The change
/// takes effect on the next process restart — this handler never tries
/// to hot-swap the live subagent because the wire path through
/// `AvatarWsState` holds an `Option<Arc<AvatarSubagent>>` directly,
/// not a lock. UI shows a "restart required" hint after success.
async fn handle_post_subagent_override(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<SubagentOverrideRequest>,
) -> axum::response::Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    use companion_core::{RuntimeOverride, runtime_override_path};

    let path = runtime_override_path(&state.config_path);

    // Load the existing override (if any) so we don't trample unrelated keys.
    let mut over = if path.exists() {
        match std::fs::read_to_string(&path)
            .ok()
            .and_then(|b| serde_json::from_str::<RuntimeOverride>(&b).ok())
        {
            Some(v) => v,
            None => RuntimeOverride::default(),
        }
    } else {
        RuntimeOverride::default()
    };

    let mut sub = over.subagent.unwrap_or_default();

    if let Some(v) = req.use_zeroclaw_webhook {
        sub.use_zeroclaw_webhook = Some(v);
    }
    if let Some(v) = req.api_key {
        // Empty string → treat as "clear the override".
        sub.api_key = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.model {
        sub.model = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.base_url {
        sub.base_url = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.timeout_secs {
        sub.timeout_secs = Some(v);
    }

    over.subagent = Some(sub);

    let body = serde_json::to_string_pretty(&over).map_err(|e| {
        (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            format!("serialize override: {e}"),
        )
    })?;
    std::fs::write(&path, body).map_err(|e| {
        (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            format!("write {}: {e}", path.display()),
        )
    })?;

    tracing::info!(
        "companion: wrote subagent override to {} (restart required to apply)",
        path.display()
    );
    // 202 Accepted — saved, but takes effect after restart.
    Ok(axum::http::StatusCode::ACCEPTED)
}

#[derive(serde::Deserialize)]
struct AvatarOverrideRequest {
    /// Master toggle for the avatar subsystem.
    enabled: Option<bool>,
    /// Chat language code (e.g. "en", "ja").
    chat_language: Option<String>,
    /// TTS speech language code.
    tts_language: Option<String>,
    /// TTS speed multiplier.
    tts_speed: Option<f64>,
    /// TTS engine identifier (e.g. "gpt-sovits-v4", "edge-tts").
    tts_engine: Option<String>,
    /// Full launch command for the TTS server process.
    tts_launch_command: Option<String>,
    /// Path to the reference audio clip used for voice cloning.
    tts_reference_audio: Option<String>,
    /// Transcript of the reference clip.
    tts_reference_text: Option<String>,
    /// Reference clip's language code.
    tts_reference_language: Option<String>,
    /// Path to the GPT-SoVITS install root.
    tts_model_path: Option<String>,
    /// CUDA device index (0+, or -1 for CPU).
    tts_gpu_device: Option<i32>,
    /// Voice id for preset-voice engines (edge-tts, melotts).
    tts_voice: Option<String>,
    /// Subagent enabled toggle.
    subagent_enabled: Option<bool>,
    /// Skip subagent when chat_lang == tts_lang.
    subagent_only_when_translating: Option<bool>,
    /// Stream the translation token-by-token (TTS per sentence).
    subagent_streaming: Option<bool>,
}

/// Persist user-flippable avatar settings to companion.runtime.json.
/// Same restart-required semantics as the subagent endpoint — the
/// avatar config is built once at startup and we don't currently
/// support hot-swapping (TTS launches a child process keyed off a
/// snapshot of the config). Settings UI shows a "Restart" button.
async fn handle_post_avatar_override(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<AvatarOverrideRequest>,
) -> axum::response::Result<axum::http::StatusCode, (axum::http::StatusCode, String)> {
    use companion_core::{RuntimeOverride, runtime_override_path};

    let path = runtime_override_path(&state.config_path);

    let mut over = if path.exists() {
        match std::fs::read_to_string(&path)
            .ok()
            .and_then(|b| serde_json::from_str::<RuntimeOverride>(&b).ok())
        {
            Some(v) => v,
            None => RuntimeOverride::default(),
        }
    } else {
        RuntimeOverride::default()
    };

    let mut av = over.avatar.unwrap_or_default();
    if let Some(v) = req.enabled { av.enabled = Some(v); }
    if let Some(v) = req.chat_language {
        av.chat_language = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_language {
        av.tts_language = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_speed {
        // Clamp into a sane band so a typo can't ship `speed = 99` to TTS.
        let clamped = v.clamp(0.25, 3.0);
        av.tts_speed = Some(clamped);
    }
    if let Some(v) = req.tts_engine {
        av.tts_engine = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_launch_command {
        av.tts_launch_command = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_reference_audio {
        av.tts_reference_audio = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_reference_text {
        av.tts_reference_text = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_reference_language {
        av.tts_reference_language = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_model_path {
        av.tts_model_path = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.tts_gpu_device {
        // Clamp to a sane range. -1 for CPU; 0..=15 for typical multi-GPU.
        let clamped = v.clamp(-1, 15);
        av.tts_gpu_device = Some(clamped);
    }
    if let Some(v) = req.tts_voice {
        av.tts_voice = if v.is_empty() { None } else { Some(v) };
    }
    if let Some(v) = req.subagent_enabled { av.subagent_enabled = Some(v); }
    if let Some(v) = req.subagent_only_when_translating { av.subagent_only_when_translating = Some(v); }
    if let Some(v) = req.subagent_streaming { av.subagent_streaming = Some(v); }

    over.avatar = Some(av);

    let body = serde_json::to_string_pretty(&over).map_err(|e| {
        (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            format!("serialize override: {e}"),
        )
    })?;
    std::fs::write(&path, body).map_err(|e| {
        (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            format!("write {}: {e}", path.display()),
        )
    })?;
    tracing::info!(
        "companion: wrote avatar override to {} (restart required to apply)",
        path.display()
    );
    Ok(axum::http::StatusCode::ACCEPTED)
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

    // Echo the user's message on the avatar broadcast channel so all
    // connected windows (main + overlay) record the same user turn in
    // their chat panel. Without this, a message typed in the overlay
    // would never reach the main window's history because only the
    // main window appendTurn user turns (overlay isn't authoritative).
    if let Some(ref avatar) = state.avatar {
        let frame = companion_avatar::AvatarEvent::Frame(
            companion_avatar::AvatarNotification::UserMessage {
                content: req.message.clone(),
            },
        );
        let _ = avatar.event_tx.send(frame);
    }

    // Prepend the active character's system prompt (if any) before
    // sending to zeroclaw. This is the load-bearing way to switch
    // personas without modifying zeroclaw's config — we just frame
    // each user message with "[Character] ... User: <msg>". Failure
    // to load the characters file is non-fatal: we just send the raw
    // message.
    let outbound = match characters::load(&characters::characters_path(&state.config_path)) {
        Ok(file) => match characters::active(&file) {
            Some(c) => {
                let prefix = characters::compose_persona_prefix(&state.config_path, c);
                if prefix.is_empty() {
                    req.message.clone()
                } else {
                    tracing::info!(
                        "companion: persona prefix for '{}' ({} chars, prompt + notes + on-disk md)",
                        c.name,
                        prefix.len(),
                    );
                    format!("{}\n\nUser message: {}", prefix, req.message)
                }
            }
            _ => req.message.clone(),
        },
        Err(e) => {
            tracing::warn!("companion: characters load failed (continuing): {e}");
            req.message.clone()
        }
    };

    let started = std::time::Instant::now();
    let reply = state
        .zeroclaw
        .send_chat(&outbound)
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
