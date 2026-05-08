//! Hacker News collector. Reads `topstories.json` and filters by score.

use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::Deserialize;

use super::{Collector, parse_interval};
use crate::config::HackerNewsConfig;
use crate::models::RawItem;

const HN_API_BASE: &str = "https://hacker-news.firebaseio.com/v0";
/// Hard cap on stories fetched per run — prevents runaway when the API
/// returns thousands of IDs.
const MAX_STORIES_PER_RUN: usize = 30;

#[derive(Debug, Deserialize, Clone)]
pub struct HnItem {
    pub id: u64,
    pub title: Option<String>,
    pub url: Option<String>,
    pub text: Option<String>,
    pub score: Option<u32>,
    pub by: Option<String>,
    pub time: Option<i64>,
    #[serde(rename = "type")]
    pub item_type: Option<String>,
    pub descendants: Option<u32>,
}

pub struct HackerNewsCollector {
    config: HackerNewsConfig,
    client: reqwest::Client,
}

impl HackerNewsCollector {
    pub fn new(config: HackerNewsConfig) -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent("zeroclaw-companion/0.1.0 (+pulse)")
            .build()
            .unwrap_or_default();
        Self { config, client }
    }

    /// Convert a parsed `HnItem` into a `RawItem` if it passes the score
    /// filter. Returns `None` for stories below `min_score`. Public so
    /// tests can verify the conversion shape without network access.
    pub fn item_to_raw(item: &HnItem, min_score: u32) -> Option<RawItem> {
        let score = item.score.unwrap_or(0);
        if score < min_score {
            return None;
        }
        let title = item.title.clone().unwrap_or_else(|| "Untitled".into());
        let published_at = item
            .time
            .map(|t| DateTime::from_timestamp(t, 0).unwrap_or_else(Utc::now));
        let hn_url = format!("https://news.ycombinator.com/item?id={}", item.id);
        let metadata = serde_json::json!({
            "hn_id": item.id,
            "score": score,
            "by": item.by,
            "comments": item.descendants.unwrap_or(0),
            "type": item.item_type,
            "hn_url": hn_url,
        });
        Some(RawItem {
            source: "hackernews".into(),
            collector_id: "hackernews".into(),
            title,
            url: item.url.clone().or(Some(hn_url)),
            content: item.text.clone(),
            metadata,
            published_at,
        })
    }
}

#[async_trait]
impl Collector for HackerNewsCollector {
    fn id(&self) -> &str {
        "hackernews"
    }
    fn name(&self) -> &str {
        "Hacker News"
    }
    fn default_interval(&self) -> Duration {
        parse_interval(&self.config.interval)
    }
    fn enabled(&self) -> bool {
        self.config.enabled
    }

    async fn collect(&self) -> Result<Vec<RawItem>> {
        let url = format!("{}/topstories.json", HN_API_BASE);
        let story_ids: Vec<u64> = self.client.get(&url).send().await?.json().await?;
        let story_ids: Vec<u64> = story_ids.into_iter().take(MAX_STORIES_PER_RUN).collect();

        let mut handles = Vec::with_capacity(story_ids.len());
        for id in story_ids {
            let client = self.client.clone();
            handles.push(tokio::spawn(async move {
                let url = format!("{}/item/{}.json", HN_API_BASE, id);
                client
                    .get(&url)
                    .send()
                    .await
                    .ok()?
                    .json::<Option<HnItem>>()
                    .await
                    .ok()
                    .flatten()
            }));
        }

        let mut items = Vec::new();
        for h in handles {
            if let Ok(Some(it)) = h.await {
                if let Some(raw) = Self::item_to_raw(&it, self.config.min_score) {
                    items.push(raw);
                }
            }
        }
        tracing::info!(
            "hackernews: {} items (min_score={})",
            items.len(),
            self.config.min_score
        );
        Ok(items)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fake(id: u64, score: u32) -> HnItem {
        HnItem {
            id,
            title: Some(format!("Story {id}")),
            url: Some(format!("https://example.com/{id}")),
            text: None,
            score: Some(score),
            by: Some("alice".into()),
            time: Some(1_700_000_000),
            item_type: Some("story".into()),
            descendants: Some(42),
        }
    }

    #[test]
    fn keeps_high_score_items() {
        let raw = HackerNewsCollector::item_to_raw(&fake(1, 100), 50).unwrap();
        assert_eq!(raw.collector_id, "hackernews");
        assert_eq!(raw.title, "Story 1");
    }

    #[test]
    fn filters_low_score_items() {
        assert!(HackerNewsCollector::item_to_raw(&fake(2, 10), 50).is_none());
    }

    #[test]
    fn preserves_score_in_metadata() {
        let raw = HackerNewsCollector::item_to_raw(&fake(3, 88), 50).unwrap();
        assert_eq!(raw.metadata["score"], 88);
        assert_eq!(raw.metadata["comments"], 42);
    }

    #[test]
    fn falls_back_to_hn_url_when_external_missing() {
        let mut item = fake(4, 100);
        item.url = None;
        let raw = HackerNewsCollector::item_to_raw(&item, 50).unwrap();
        assert!(
            raw.url
                .as_deref()
                .unwrap()
                .contains("news.ycombinator.com/item?id=4")
        );
    }
}
