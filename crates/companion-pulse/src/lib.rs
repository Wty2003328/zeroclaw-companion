//! Pulse — personal intelligence dashboard for the zeroclaw companion.
//!
//! Periodic collectors (RSS, HackerNews, …) push items into a SQLite-backed
//! store. The companion-server exposes the feed at `/api/pulse/*`. The
//! frontend renders a dashboard.
//!
//! Architecture:
//! ```text
//!   PulseConfig (companion.toml [pulse])
//!         │
//!         ▼
//!   Scheduler ── runs each Collector at its interval ──▶ PulseDatabase
//!                                                              │
//!                                                              ▼
//!                                                       /api/pulse/feed
//! ```

pub mod collectors;
pub mod config;
pub mod models;
pub mod scheduler;
pub mod storage;

pub use config::{PulseConfig, RssConfig, FeedEntry, HackerNewsConfig};
pub use models::{CollectorRun, FeedItem, RawItem};
pub use scheduler::{Scheduler, trigger_collector};
pub use storage::PulseDatabase;
pub use collectors::{Collector, parse_interval};

use std::sync::Arc;

/// Shared Pulse subsystem state. Held by the server's AppState when
/// `[pulse] enabled = true`.
#[derive(Clone)]
pub struct PulseSubsystem {
    pub db: PulseDatabase,
    pub collectors: Vec<Arc<dyn Collector>>,
}

impl PulseSubsystem {
    /// Build the subsystem and start the scheduler in the background.
    pub async fn start(cfg: &PulseConfig) -> anyhow::Result<Self> {
        // Resolve DB path. Default ./data/pulse.db relative to CWD.
        let db_path = cfg.database.path.clone();
        let db = PulseDatabase::new(&db_path).await?;

        let mut list: Vec<Arc<dyn Collector>> = Vec::new();
        if let Some(rss) = cfg.collectors.rss.clone() {
            list.push(Arc::new(collectors::rss::RssCollector::new(rss)));
        }
        if let Some(hn) = cfg.collectors.hackernews.clone() {
            list.push(Arc::new(collectors::hackernews::HackerNewsCollector::new(
                hn,
            )));
        }

        tracing::info!("pulse: {} collector(s) registered", list.len());

        let sched = Arc::new(Scheduler::new(list.clone(), db.clone()));
        let sched_handle = Arc::clone(&sched);
        tokio::spawn(async move {
            sched_handle.start().await;
        });

        Ok(Self {
            db,
            collectors: list,
        })
    }
}
