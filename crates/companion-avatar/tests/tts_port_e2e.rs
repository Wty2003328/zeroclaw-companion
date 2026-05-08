//! End-to-end tests for the TTS port wire contract.
//!
//! Spins up a mock TTS server on an ephemeral port and verifies that
//! `AnimeTtsManager::synthesize_with` speaks the documented contract:
//! `POST /tts {text, language, voice?, speed?}` → audio bytes with
//! optional `X-Sample-Rate` / `X-Channels` / `X-Format` headers.

use std::sync::Arc;
use std::time::Duration;

use axum::{
    Router,
    extract::{Json, State},
    http::HeaderMap,
    response::IntoResponse,
    routing::{get, post},
};
use companion_avatar::{AnimeTtsManager, config::AvatarTtsConfig};
use serde::Deserialize;
use tokio::sync::Mutex;

#[derive(Debug, Default, Clone)]
struct MockState {
    /// Captured request bodies, in the order they arrived.
    requests: Arc<Mutex<Vec<MockRequest>>>,
    /// Headers to include on each response (default: WAV at 48kHz mono).
    response_sample_rate: u32,
    response_channels: u16,
    response_format: String,
    /// Bytes to return as the audio payload.
    response_body: Vec<u8>,
    /// HTTP status to return (200 by default).
    response_status: u16,
}

#[derive(Debug, Deserialize, Clone)]
struct MockRequest {
    text: String,
    language: String,
    #[serde(default)]
    voice: Option<String>,
    #[serde(default)]
    speed: Option<f32>,
}

async fn handle_health() -> &'static str {
    "ok"
}

async fn handle_tts(
    State(state): State<MockState>,
    Json(req): Json<MockRequest>,
) -> impl IntoResponse {
    state.requests.lock().await.push(req);
    let mut headers = HeaderMap::new();
    headers.insert(
        "X-Sample-Rate",
        state.response_sample_rate.to_string().parse().unwrap(),
    );
    headers.insert(
        "X-Channels",
        state.response_channels.to_string().parse().unwrap(),
    );
    headers.insert("X-Format", state.response_format.parse().unwrap());
    (
        axum::http::StatusCode::from_u16(state.response_status).unwrap(),
        headers,
        state.response_body.clone(),
    )
}

/// Boot a mock TTS server bound on a random port. Returns (port, captured_requests).
async fn boot_mock(state: MockState) -> (u16, Arc<Mutex<Vec<MockRequest>>>) {
    let captured = Arc::clone(&state.requests);
    let app = Router::new()
        .route("/health", get(handle_health))
        .route("/tts", post(handle_tts))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });
    // Tiny grace so the listener is accepting before the test calls in.
    tokio::time::sleep(Duration::from_millis(20)).await;
    (port, captured)
}

fn config_for(port: u16) -> AvatarTtsConfig {
    AvatarTtsConfig {
        engine: "mock".into(),
        api_url: Some(format!("http://127.0.0.1:{port}")),
        model_path: None,
        reference_audio: None,
        reference_text: None,
        reference_language: None,
        gpu_device: -1,
        port,
        launch_command: None,
        auto_start: false,
        voice: Some("alice".into()),
        language: "en".into(),
        speed: 1.25,
    }
}

#[tokio::test]
async fn synthesize_speaks_wire_contract() {
    let mock = MockState {
        requests: Arc::new(Mutex::new(Vec::new())),
        response_sample_rate: 48_000,
        response_channels: 1,
        response_format: "wav".into(),
        response_body: vec![0xDE, 0xAD, 0xBE, 0xEF],
        response_status: 200,
    };
    let (port, captured) = boot_mock(mock).await;
    let mgr = AnimeTtsManager::new(&config_for(port)).unwrap();

    let out = mgr.synthesize_with("hello", "en").await.unwrap();
    assert_eq!(out.audio_bytes, vec![0xDE, 0xAD, 0xBE, 0xEF]);
    assert_eq!(out.sample_rate, 48_000);
    assert_eq!(out.channels, 1);
    assert_eq!(out.format, "wav");

    let reqs = captured.lock().await;
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].text, "hello");
    assert_eq!(reqs[0].language, "en");
    assert_eq!(reqs[0].voice.as_deref(), Some("alice"));
    assert_eq!(reqs[0].speed, Some(1.25));
}

#[tokio::test]
async fn synthesize_with_overrides_default_language() {
    let mock = MockState {
        requests: Arc::new(Mutex::new(Vec::new())),
        response_sample_rate: 24_000,
        response_channels: 1,
        response_format: "wav".into(),
        response_body: b"audio-bytes".to_vec(),
        response_status: 200,
    };
    let (port, captured) = boot_mock(mock).await;
    // config default is "en" but per-call we ask for "ja"
    let mgr = AnimeTtsManager::new(&config_for(port)).unwrap();
    mgr.synthesize_with("こんにちは", "ja").await.unwrap();

    let reqs = captured.lock().await;
    assert_eq!(reqs[0].language, "ja");
    assert_eq!(reqs[0].text, "こんにちは");
}

#[tokio::test]
async fn synthesize_propagates_server_error() {
    let mock = MockState {
        requests: Arc::new(Mutex::new(Vec::new())),
        response_sample_rate: 24_000,
        response_channels: 1,
        response_format: "wav".into(),
        response_body: b"upstream blew up".to_vec(),
        response_status: 502,
    };
    let (port, _) = boot_mock(mock).await;
    let mgr = AnimeTtsManager::new(&config_for(port)).unwrap();
    let err = mgr.synthesize_with("hi", "en").await.unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("502"), "expected status in error, got: {msg}");
}

#[tokio::test]
async fn synthesize_without_metadata_headers_uses_defaults() {
    // Mock that returns a body but no X-Sample-Rate / X-Channels / X-Format.
    async fn raw_handler() -> Vec<u8> {
        vec![1, 2, 3]
    }
    let app = Router::new()
        .route("/health", get(|| async { "ok" }))
        .route("/tts", post(raw_handler));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    let mgr = AnimeTtsManager::new(&config_for(port)).unwrap();
    let out = mgr.synthesize_with("x", "en").await.unwrap();
    // documented defaults from tts_server.rs
    assert_eq!(out.sample_rate, 24_000);
    assert_eq!(out.channels, 1);
    assert_eq!(out.format, "wav");
}

#[tokio::test]
async fn health_check_against_real_server() {
    let mock = MockState {
        requests: Arc::new(Mutex::new(Vec::new())),
        response_sample_rate: 24_000,
        response_channels: 1,
        response_format: "wav".into(),
        response_body: vec![],
        response_status: 200,
    };
    let (port, _) = boot_mock(mock).await;
    let mgr = AnimeTtsManager::new(&config_for(port)).unwrap();
    assert!(mgr.health_check().await.unwrap());
}

#[tokio::test]
async fn health_check_against_unreachable_server_returns_false() {
    // Pick a port that's almost certainly unbound. Worst case: race; that
    // would return Ok(true) by accident. To make this reliable we bind a
    // throwaway listener and immediately drop it to free the port.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);

    let mut cfg = config_for(port);
    // Shrink the implicit timeout window via a non-existent host bind:
    cfg.api_url = Some(format!("http://127.0.0.1:{port}"));
    let mgr = AnimeTtsManager::new(&cfg).unwrap();
    let healthy = mgr.health_check().await.unwrap();
    assert!(!healthy, "expected unhealthy for unreachable server");
}
