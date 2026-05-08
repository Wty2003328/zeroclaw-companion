//! Pulse collector framework.
//!
//! A collector is anything that periodically returns a list of [`RawItem`]s.
//! The scheduler drives them at their configured cadence; storage handles
//! deduplication by URL.

pub mod hackernews;
pub mod rss;

use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;

use crate::models::RawItem;

#[async_trait]
pub trait Collector: Send + Sync {
    /// Stable identifier (e.g. `"rss"`, `"hackernews"`). Used as the
    /// `collector_id` on stored items and for run logs.
    fn id(&self) -> &str;
    /// Human-readable name for logs / UI.
    fn name(&self) -> &str;
    /// How often the scheduler should call `collect`.
    fn default_interval(&self) -> Duration;
    /// Whether the scheduler should run this collector at all.
    fn enabled(&self) -> bool;
    /// Fetch items.
    async fn collect(&self) -> Result<Vec<RawItem>>;
}

/// Parse intervals like `"30m"`, `"1h"`, `"45s"`. Falls back to 30 minutes.
pub fn parse_interval(s: &str) -> Duration {
    let s = s.trim();
    if let Some(rest) = s.strip_suffix('s') {
        if let Ok(n) = rest.parse::<u64>() {
            return Duration::from_secs(n);
        }
    }
    if let Some(rest) = s.strip_suffix('m') {
        if let Ok(n) = rest.parse::<u64>() {
            return Duration::from_secs(n * 60);
        }
    }
    if let Some(rest) = s.strip_suffix('h') {
        if let Ok(n) = rest.parse::<u64>() {
            return Duration::from_secs(n * 3600);
        }
    }
    Duration::from_secs(30 * 60)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_seconds() {
        assert_eq!(parse_interval("45s"), Duration::from_secs(45));
    }
    #[test]
    fn parse_minutes() {
        assert_eq!(parse_interval("30m"), Duration::from_secs(30 * 60));
    }
    #[test]
    fn parse_hours() {
        assert_eq!(parse_interval("2h"), Duration::from_secs(2 * 3600));
    }
    #[test]
    fn unparseable_falls_back_to_default() {
        assert_eq!(parse_interval("bogus"), Duration::from_secs(30 * 60));
        assert_eq!(parse_interval(""), Duration::from_secs(30 * 60));
    }
    #[test]
    fn whitespace_tolerated() {
        assert_eq!(parse_interval("  10m  "), Duration::from_secs(10 * 60));
    }
}
