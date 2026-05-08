//! RSS / Atom feed collector. Wraps `feed-rs`.

use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;
use chrono::{DateTime, Utc};

use super::{Collector, parse_interval};
use crate::config::RssConfig;
use crate::models::RawItem;

pub struct RssCollector {
    config: RssConfig,
    client: reqwest::Client,
}

impl RssCollector {
    pub fn new(config: RssConfig) -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent("zeroclaw-companion/0.1.0 (+pulse)")
            .build()
            .unwrap_or_default();
        Self { config, client }
    }

    /// Parse an arbitrary feed body (RSS or Atom) into [`RawItem`]s.
    /// Public so tests can inject canned XML without needing the network.
    pub fn parse_body(name: &str, body: &[u8]) -> Result<Vec<RawItem>> {
        let feed = feed_rs::parser::parse(body)?;
        let items: Vec<RawItem> = feed
            .entries
            .into_iter()
            .map(|entry| {
                let title = entry
                    .title
                    .map(|t| t.content)
                    .unwrap_or_else(|| "Untitled".into());
                let url = entry.links.first().map(|l| l.href.clone());
                let content = entry
                    .summary
                    .map(|s| s.content)
                    .or_else(|| entry.content.and_then(|c| c.body));
                let published_at: Option<DateTime<Utc>> = entry
                    .published
                    .or(entry.updated)
                    .map(|d| d.with_timezone(&Utc));
                let metadata = serde_json::json!({
                    "feed_name": name,
                    "feed_url": url,
                    "authors": entry.authors.iter().map(|a| &a.name).collect::<Vec<_>>(),
                    "categories": entry.categories.iter().map(|c| &c.term).collect::<Vec<_>>(),
                });
                RawItem {
                    source: format!("rss:{}", name.to_lowercase().replace(' ', "-")),
                    collector_id: "rss".to_string(),
                    title,
                    url,
                    content,
                    metadata,
                    published_at,
                }
            })
            .collect();
        Ok(items)
    }

    async fn fetch_feed(&self, name: &str, url: &str) -> Result<Vec<RawItem>> {
        tracing::debug!("rss: fetching {} ({})", name, url);
        let body = self.client.get(url).send().await?.bytes().await?;
        Self::parse_body(name, &body)
    }
}

#[async_trait]
impl Collector for RssCollector {
    fn id(&self) -> &str {
        "rss"
    }
    fn name(&self) -> &str {
        "RSS Feeds"
    }
    fn default_interval(&self) -> Duration {
        parse_interval(&self.config.interval)
    }
    fn enabled(&self) -> bool {
        self.config.enabled
    }

    async fn collect(&self) -> Result<Vec<RawItem>> {
        let mut all = Vec::new();
        for feed in &self.config.feeds {
            match self.fetch_feed(&feed.name, &feed.url).await {
                Ok(items) => {
                    tracing::info!("rss: {} → {} items", feed.name, items.len());
                    all.extend(items);
                }
                Err(e) => tracing::warn!("rss: {} failed: {e}", feed.name),
            }
        }
        Ok(all)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RSS_SAMPLE: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample Feed</title>
    <link>https://example.com/</link>
    <item>
      <title>First Post</title>
      <link>https://example.com/1</link>
      <description>Body of post one.</description>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.com/2</link>
      <description>Body of post two.</description>
    </item>
  </channel>
</rss>"#;

    #[test]
    fn parse_rss_two_items() {
        let items = RssCollector::parse_body("Sample", RSS_SAMPLE.as_bytes()).unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].title, "First Post");
        assert_eq!(items[0].url.as_deref(), Some("https://example.com/1"));
        assert_eq!(items[0].source, "rss:sample");
        assert_eq!(items[0].collector_id, "rss");
        assert!(items[0].published_at.is_some());
    }

    #[test]
    fn parse_rss_lowercases_source_and_replaces_spaces() {
        let items = RssCollector::parse_body("My Cool Feed", RSS_SAMPLE.as_bytes()).unwrap();
        assert_eq!(items[0].source, "rss:my-cool-feed");
    }

    #[test]
    fn parse_rss_missing_pubdate() {
        let items = RssCollector::parse_body("Sample", RSS_SAMPLE.as_bytes()).unwrap();
        assert!(items[1].published_at.is_none());
    }

    #[test]
    fn parse_rss_invalid_body_errors() {
        let result = RssCollector::parse_body("Sample", b"not xml");
        assert!(result.is_err());
    }
}
