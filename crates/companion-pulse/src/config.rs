//! Pulse configuration. Lives under `[pulse]` in `companion.toml`.

use serde::{Deserialize, Serialize};

/// Top-level Pulse configuration.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PulseConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub database: DatabaseConfig,
    #[serde(default)]
    pub collectors: CollectorsConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DatabaseConfig {
    #[serde(default = "default_db_path")]
    pub path: String,
    #[serde(default = "default_retention_days")]
    pub retention_days: u32,
}

impl Default for DatabaseConfig {
    fn default() -> Self {
        Self {
            path: default_db_path(),
            retention_days: default_retention_days(),
        }
    }
}

fn default_db_path() -> String {
    "./data/pulse.db".into()
}
fn default_retention_days() -> u32 {
    30
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CollectorsConfig {
    #[serde(default)]
    pub rss: Option<RssConfig>,
    #[serde(default)]
    pub hackernews: Option<HackerNewsConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RssConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_30m")]
    pub interval: String,
    #[serde(default)]
    pub feeds: Vec<FeedEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FeedEntry {
    pub name: String,
    pub url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HackerNewsConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_15m")]
    pub interval: String,
    #[serde(default = "default_hn_min_score")]
    pub min_score: u32,
}

fn default_true() -> bool {
    true
}
fn default_30m() -> String {
    "30m".into()
}
fn default_15m() -> String {
    "15m".into()
}
fn default_hn_min_score() -> u32 {
    50
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_pulse_config() {
        let toml = r#"
            enabled = true

            [database]
            path = "/tmp/pulse.db"
        "#;
        let cfg: PulseConfig = toml::from_str(toml).unwrap();
        assert!(cfg.enabled);
        assert_eq!(cfg.database.path, "/tmp/pulse.db");
    }

    #[test]
    fn parses_collector_config() {
        let toml = r#"
            [collectors.rss]
            enabled = true
            interval = "1h"
            feeds = [
                { name = "Lobsters", url = "https://lobste.rs/rss" },
            ]
        "#;
        let cfg: PulseConfig = toml::from_str(toml).unwrap();
        let rss = cfg.collectors.rss.expect("rss config");
        assert_eq!(rss.feeds.len(), 1);
        assert_eq!(rss.feeds[0].name, "Lobsters");
    }
}
