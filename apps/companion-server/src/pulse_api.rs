//! Pulse REST API.
//!
//! Mounted at `/api/pulse/*` only when `[pulse] enabled = true`. Routes:
//! - `GET  /api/pulse/feed`              — recent items (?limit=, ?offset=, ?source=)
//! - `GET  /api/pulse/status`            — collector run history
//! - `POST /api/pulse/trigger/{id}`      — manually run a collector by id
//! - `GET  /api/pulse/feeds`             — user-managed RSS feeds
//! - `POST /api/pulse/feeds`             — add a feed
//! - `DELETE /api/pulse/feeds`           — remove by url

use std::sync::Arc;

use axum::{
    Json, Router,
    extract::{Query, State, Path},
    http::StatusCode,
    routing::{get, post},
};
use serde::Deserialize;

use companion_pulse::{PulseSubsystem, scheduler::trigger_collector};

#[derive(Debug, Deserialize)]
pub struct FeedQuery {
    #[serde(default = "default_limit")]
    limit: u32,
    #[serde(default)]
    offset: u32,
    #[serde(default)]
    source: Option<String>,
}

fn default_limit() -> u32 {
    50
}

#[derive(Debug, Deserialize)]
pub struct AddFeedReq {
    name: String,
    url: String,
}

#[derive(Debug, Deserialize)]
pub struct RemoveFeedQuery {
    url: String,
}

pub fn routes() -> Router<Arc<PulseSubsystem>> {
    Router::new()
        .route("/feed", get(handle_feed))
        .route("/status", get(handle_status))
        .route("/trigger/{id}", post(handle_trigger))
        .route("/feeds", get(handle_list_feeds).post(handle_add_feed).delete(handle_remove_feed))
}

async fn handle_feed(
    State(state): State<Arc<PulseSubsystem>>,
    Query(q): Query<FeedQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let limit = q.limit.min(500);
    let items = state
        .db
        .get_feed(limit, q.offset, q.source.as_deref())
        .await
        .map_err(internal)?;
    Ok(Json(serde_json::json!({ "items": items })))
}

async fn handle_status(
    State(state): State<Arc<PulseSubsystem>>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let runs = state.db.get_collector_status().await.map_err(internal)?;
    let collectors: Vec<_> = state
        .collectors
        .iter()
        .map(|c| {
            serde_json::json!({
                "id": c.id(),
                "name": c.name(),
                "enabled": c.enabled(),
                "interval_secs": c.default_interval().as_secs(),
            })
        })
        .collect();
    Ok(Json(
        serde_json::json!({ "collectors": collectors, "runs": runs }),
    ))
}

async fn handle_trigger(
    State(state): State<Arc<PulseSubsystem>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    trigger_collector(&state.collectors, &state.db, &id)
        .await
        .map_err(|e| (StatusCode::NOT_FOUND, e.to_string()))?;
    Ok(Json(serde_json::json!({ "triggered": id })))
}

async fn handle_list_feeds(
    State(state): State<Arc<PulseSubsystem>>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let feeds = state.db.get_user_feeds().await.map_err(internal)?;
    let shaped: Vec<_> = feeds
        .into_iter()
        .map(|(name, url)| serde_json::json!({ "name": name, "url": url }))
        .collect();
    Ok(Json(serde_json::json!({ "feeds": shaped })))
}

async fn handle_add_feed(
    State(state): State<Arc<PulseSubsystem>>,
    Json(req): Json<AddFeedReq>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if req.name.trim().is_empty() || req.url.trim().is_empty() {
        return Err((StatusCode::BAD_REQUEST, "name and url are required".into()));
    }
    state.db.add_user_feed(&req.name, &req.url).await.map_err(internal)?;
    Ok(Json(serde_json::json!({ "ok": true })))
}

async fn handle_remove_feed(
    State(state): State<Arc<PulseSubsystem>>,
    Query(q): Query<RemoveFeedQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    state.db.remove_user_feed(&q.url).await.map_err(internal)?;
    Ok(Json(serde_json::json!({ "ok": true })))
}

fn internal(e: anyhow::Error) -> (StatusCode, String) {
    tracing::error!("pulse api: {e}");
    (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
}
