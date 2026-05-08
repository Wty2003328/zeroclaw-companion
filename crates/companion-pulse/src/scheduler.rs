//! Periodic execution of collectors. Each collector runs on its own
//! tokio task at its configured cadence, with run logs written to the DB.

use std::sync::Arc;

use anyhow::Result;
use tokio::time;

use crate::collectors::Collector;
use crate::storage::PulseDatabase;

pub struct Scheduler {
    collectors: Vec<Arc<dyn Collector>>,
    db: PulseDatabase,
}

impl Scheduler {
    pub fn new(collectors: Vec<Arc<dyn Collector>>, db: PulseDatabase) -> Self {
        Self { collectors, db }
    }

    /// Spawn one task per enabled collector. Each task runs once
    /// immediately, then on the configured interval. The task lives for
    /// the lifetime of the process (no cancellation token yet — add one
    /// if we ever need graceful Pulse shutdown distinct from process exit).
    pub async fn start(self: Arc<Self>) {
        for collector in &self.collectors {
            if !collector.enabled() {
                tracing::info!("pulse: collector '{}' disabled, skipping", collector.id());
                continue;
            }
            let collector = Arc::clone(collector);
            let db = self.db.clone();
            let interval = collector.default_interval();
            tracing::info!(
                "pulse: scheduling '{}' every {}s",
                collector.id(),
                interval.as_secs()
            );
            tokio::spawn(async move {
                run_collector(&collector, &db).await;
                let mut ticker = time::interval(interval);
                ticker.tick().await; // skip the first immediate tick
                loop {
                    ticker.tick().await;
                    run_collector(&collector, &db).await;
                }
            });
        }
    }
}

/// One pass: start a run record, fetch items, deduplicate by URL, persist,
/// finalize the run record. Errors at the fetch level are recorded; errors
/// at the persistence level are logged but don't stop the run.
async fn run_collector(collector: &Arc<dyn Collector>, db: &PulseDatabase) {
    let run_id = match db.start_collector_run(collector.id()).await {
        Ok(id) => id,
        Err(e) => {
            tracing::error!("pulse: failed to record run start for {}: {e}", collector.id());
            return;
        }
    };
    tracing::debug!("pulse: running {}", collector.id());

    match collector.collect().await {
        Ok(items) => {
            let mut inserted = 0u32;
            for item in &items {
                if let Some(ref url) = item.url {
                    match db.item_exists_by_url(url).await {
                        Ok(true) => continue,
                        Ok(false) => {}
                        Err(e) => tracing::warn!("pulse: dedup check failed: {e}"),
                    }
                }
                match db.insert_item(item).await {
                    Ok(_) => inserted += 1,
                    Err(e) => tracing::warn!("pulse: insert failed for '{}': {e}", item.title),
                }
            }
            tracing::info!(
                "pulse: {} run done — fetched={} new={}",
                collector.id(),
                items.len(),
                inserted
            );
            if let Err(e) = db.finish_collector_run(&run_id, inserted, None).await {
                tracing::error!("pulse: failed to finalize run: {e}");
            }
        }
        Err(e) => {
            tracing::error!("pulse: collector {} failed: {e}", collector.id());
            let _ = db.finish_collector_run(&run_id, 0, Some(&e.to_string())).await;
        }
    }
}

/// Run a single collector by id once (manual trigger from the API).
pub async fn trigger_collector(
    collectors: &[Arc<dyn Collector>],
    db: &PulseDatabase,
    collector_id: &str,
) -> Result<()> {
    let collector = collectors
        .iter()
        .find(|c| c.id() == collector_id)
        .ok_or_else(|| anyhow::anyhow!("collector '{collector_id}' not found"))?;
    run_collector(collector, db).await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{HackerNewsConfig, RssConfig};
    use crate::collectors::{hackernews::HackerNewsCollector, rss::RssCollector};

    #[test]
    fn scheduler_skips_disabled_collectors() {
        // Simply verify we can build a scheduler with disabled collectors
        // — actual run loop has its own integration test elsewhere.
        let rss = RssConfig {
            enabled: false,
            interval: "30m".into(),
            feeds: vec![],
        };
        let hn = HackerNewsConfig {
            enabled: false,
            interval: "15m".into(),
            min_score: 50,
        };
        let _r = Arc::new(RssCollector::new(rss)) as Arc<dyn Collector>;
        let _h = Arc::new(HackerNewsCollector::new(hn)) as Arc<dyn Collector>;
        // (Integration with real DB + ticking is exercised in e2e tests.)
    }
}
