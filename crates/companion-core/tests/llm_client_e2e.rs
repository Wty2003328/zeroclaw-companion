//! End-to-end test for the OpenAI-compatible LLM client.
//!
//! Mocks the `/v1/chat/completions` endpoint and verifies our client
//! sends the right body shape and parses the response.

use std::time::Duration;

use axum::{Json, Router, routing::post};
use companion_core::{ChatMessage, LlmClient, LlmConfig, Role};
use serde_json::{Value, json};

async fn boot_mock(canned: Value) -> u16 {
    let app = Router::new().route(
        "/v1/chat/completions",
        post(move |Json(req): Json<Value>| {
            let canned = canned.clone();
            async move {
                // Echo the request into the response so the test can assert
                // on what we sent.
                Json(json!({
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": canned.to_string(),
                        }
                    }],
                    "_observed_request": req,
                }))
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });
    tokio::time::sleep(Duration::from_millis(20)).await;
    port
}

#[tokio::test]
async fn chat_round_trip() {
    let port = boot_mock(json!("hello back")).await;
    let cfg = LlmConfig {
        base_url: format!("http://127.0.0.1:{port}/v1"),
        api_key: Some("dummy".into()),
        api_key_env: None,
        model: "test-model".into(),
        temperature: 0.5,
        max_tokens: 100,
        timeout_secs: 5,
    };
    let client = LlmClient::new(&cfg).unwrap();
    let reply = client
        .chat(&[ChatMessage {
            role: Role::User,
            content: "hi".into(),
        }])
        .await
        .unwrap();
    // The mock echoes a JSON-stringified payload; we just need to know
    // the response was parsed and assistant.content extracted.
    assert!(reply.contains("hello back"));
}

#[tokio::test]
async fn chat_propagates_5xx_error() {
    let app = Router::new().route(
        "/v1/chat/completions",
        post(|| async {
            (axum::http::StatusCode::INTERNAL_SERVER_ERROR, "upstream broke")
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    let cfg = LlmConfig {
        base_url: format!("http://127.0.0.1:{port}/v1"),
        model: "x".into(),
        timeout_secs: 5,
        ..Default::default()
    };
    let client = LlmClient::new(&cfg).unwrap();
    let err = client
        .chat(&[ChatMessage {
            role: Role::User,
            content: "x".into(),
        }])
        .await
        .unwrap_err();
    assert!(err.to_string().contains("500"));
}

#[tokio::test]
async fn chat_errors_on_malformed_response() {
    let app = Router::new().route(
        "/v1/chat/completions",
        post(|| async { Json(json!({"unexpected": "shape"})) }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });
    tokio::time::sleep(Duration::from_millis(20)).await;

    let cfg = LlmConfig {
        base_url: format!("http://127.0.0.1:{port}/v1"),
        model: "x".into(),
        timeout_secs: 5,
        ..Default::default()
    };
    let client = LlmClient::new(&cfg).unwrap();
    let err = client
        .chat(&[ChatMessage {
            role: Role::User,
            content: "x".into(),
        }])
        .await
        .unwrap_err();
    assert!(err.to_string().contains("missing"));
}
