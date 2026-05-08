//! End-to-end tests for the zeroclaw HTTP+SSE client.
//!
//! Mock zeroclaw is an axum server that:
//! - Returns 200 on `/health`
//! - Echoes a reply on `POST /webhook` (matches real zeroclaw v0.7.5)
//! - Streams `text/event-stream` on `GET /api/events`

use std::time::Duration;

use axum::{
    Json, Router,
    response::sse::{Event, KeepAlive, Sse},
    routing::{get, post},
};
use companion_core::{AgentEvent, ZeroclawClient, config::ZeroclawConfig};
use futures_util::StreamExt;
use serde_json::json;
use tokio_stream::wrappers::ReceiverStream;

async fn boot_mock(events: Vec<&'static str>) -> u16 {
    let app = Router::new()
        .route("/health", get(|| async { "ok" }))
        .route(
            "/webhook",
            post(|Json(p): Json<serde_json::Value>| async move {
                let msg = p
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("(no message)");
                // Real zeroclaw v0.7.5 returns {"model":"...","response":"..."}
                Json(json!({"model": "test-model", "response": format!("echo: {msg}")}))
            }),
        )
        .route(
            "/api/events",
            get(move || {
                let evs = events.clone();
                async move {
                    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, std::io::Error>>(8);
                    tokio::spawn(async move {
                        for body in evs {
                            let ev = Event::default().data(body);
                            let _ = tx.send(Ok(ev)).await;
                            tokio::time::sleep(Duration::from_millis(5)).await;
                        }
                    });
                    let stream = ReceiverStream::new(rx);
                    Sse::new(stream).keep_alive(KeepAlive::default())
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

fn config_for(port: u16) -> ZeroclawConfig {
    ZeroclawConfig {
        url: format!("http://127.0.0.1:{port}"),
        pair_token: None,
        timeout_secs: 10,
    }
}

#[tokio::test]
async fn health_endpoint_round_trip() {
    let port = boot_mock(vec![]).await;
    let client = ZeroclawClient::new(&config_for(port)).unwrap();
    assert!(client.health().await.unwrap());
}

#[tokio::test]
async fn send_chat_returns_reply_body() {
    let port = boot_mock(vec![]).await;
    let client = ZeroclawClient::new(&config_for(port)).unwrap();
    let reply = client.send_chat("ping").await.unwrap();
    assert_eq!(reply, "echo: ping");
}

#[tokio::test]
async fn sse_stream_yields_classified_reply() {
    let payloads = vec![
        r#"{"event":"agent.reply","text":"hello world","session_id":"s1"}"#,
        r#"{"event":"tool.call","name":"shell"}"#,
    ];
    let port = boot_mock(payloads).await;
    let client = ZeroclawClient::new(&config_for(port)).unwrap();
    let mut stream = Box::pin(client.events().await.unwrap());

    // First event: classified as AgentReply
    let ev = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap();
    match ev {
        AgentEvent::AgentReply { text, session_id } => {
            assert_eq!(text, "hello world");
            assert_eq!(session_id.as_deref(), Some("s1"));
        }
        other => panic!("expected AgentReply, got {other:?}"),
    }

    // Second event: tool.call → Other
    let ev = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap();
    matches!(ev, AgentEvent::Other { .. });
}

#[tokio::test]
async fn sse_classifies_streaming_tokens() {
    let port = boot_mock(vec![
        r#"{"event":"agent.token","text":"he"}"#,
        r#"{"event":"agent.token","text":"llo"}"#,
    ])
    .await;
    let client = ZeroclawClient::new(&config_for(port)).unwrap();
    let mut stream = Box::pin(client.events().await.unwrap());
    let ev = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap();
    match ev {
        AgentEvent::AgentToken { text, .. } => assert_eq!(text, "he"),
        _ => panic!("expected AgentToken"),
    }
}

#[tokio::test]
async fn sse_handles_role_assistant_final_shape() {
    // Some zeroclaw versions emit role/content/final instead of event/text.
    let port = boot_mock(vec![
        r#"{"role":"assistant","content":"the answer is 42","final":true}"#,
    ])
    .await;
    let client = ZeroclawClient::new(&config_for(port)).unwrap();
    let mut stream = Box::pin(client.events().await.unwrap());
    let ev = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap();
    match ev {
        AgentEvent::AgentReply { text, .. } => {
            assert_eq!(text, "the answer is 42");
        }
        other => panic!("expected AgentReply, got {other:?}"),
    }
}
