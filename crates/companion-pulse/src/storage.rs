//! SQLite-backed storage for Pulse items + collector runs.
//!
//! rusqlite is synchronous, so every operation is wrapped in
//! `tokio::task::spawn_blocking`. Each call opens its own connection to
//! avoid mutex contention between the scheduler and API handlers (SQLite
//! handles concurrent access fine via its own locking).

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use chrono::Utc;
use rusqlite::{Connection, params};
use uuid::Uuid;

use crate::models::{CollectorRun, FeedItem, RawItem};

#[derive(Clone)]
pub struct PulseDatabase {
    path: Arc<String>,
}

impl PulseDatabase {
    /// Open or create the database, run migrations.
    pub async fn new(db_path: &str) -> Result<Self> {
        if let Some(parent) = Path::new(db_path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)
                    .with_context(|| format!("failed to create dir {}", parent.display()))?;
            }
        }

        // One-shot bootstrap: WAL + foreign keys.
        let path = db_path.to_string();
        let p2 = path.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let conn = Connection::open(&p2)?;
            conn.execute_batch(
                "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;",
            )?;
            Ok(())
        })
        .await??;

        let db = Self {
            path: Arc::new(path),
        };
        db.run_migrations().await?;
        tracing::info!("pulse: database ready at {}", db_path);
        Ok(db)
    }

    fn open(&self) -> Result<Connection> {
        let conn = Connection::open(self.path.as_str())?;
        conn.execute_batch("PRAGMA busy_timeout=5000;")?;
        Ok(conn)
    }

    async fn run_migrations(&self) -> Result<()> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let c = Connection::open(path.as_str())?;
            c.execute_batch(
                "CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    collector_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    content TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    published_at TEXT,
                    collected_at TEXT NOT NULL
                 );
                 CREATE INDEX IF NOT EXISTS idx_items_collected_at ON items(collected_at);
                 CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
                 CREATE TABLE IF NOT EXISTS collector_runs (
                    id TEXT PRIMARY KEY,
                    collector_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    items_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    error TEXT
                 );
                 CREATE TABLE IF NOT EXISTS user_feeds (
                    name TEXT NOT NULL,
                    url TEXT NOT NULL PRIMARY KEY
                 );",
            )?;
            Ok(())
        })
        .await??;
        Ok(())
    }

    /// Insert a new item, returning its UUID.
    pub async fn insert_item(&self, raw: &RawItem) -> Result<String> {
        let id = Uuid::new_v4().to_string();
        let now = Utc::now().to_rfc3339();
        let metadata = serde_json::to_string(&raw.metadata)?;
        let published = raw.published_at.map(|d| d.to_rfc3339());
        let item = raw.clone();
        let id2 = id.clone();
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let c = Connection::open(path.as_str())?;
            c.execute(
                "INSERT INTO items
                 (id, source, collector_id, title, url, content, metadata, published_at, collected_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                params![
                    id2, item.source, item.collector_id, item.title,
                    item.url, item.content, metadata, published, now
                ],
            )?;
            Ok(())
        })
        .await??;
        Ok(id)
    }

    /// True when an item with this URL already exists (deduplication helper).
    pub async fn item_exists_by_url(&self, url: &str) -> Result<bool> {
        let path = self.path.clone();
        let url = url.to_string();
        tokio::task::spawn_blocking(move || -> Result<bool> {
            let c = Connection::open(path.as_str())?;
            let count: i64 = c.query_row(
                "SELECT COUNT(*) FROM items WHERE url = ?1",
                params![url],
                |r| r.get(0),
            )?;
            Ok(count > 0)
        })
        .await?
    }

    /// Most-recent items, optionally filtered by source.
    pub async fn get_feed(
        &self,
        limit: u32,
        offset: u32,
        source: Option<&str>,
    ) -> Result<Vec<FeedItem>> {
        let path = self.path.clone();
        let source = source.map(|s| s.to_string());
        tokio::task::spawn_blocking(move || -> Result<Vec<FeedItem>> {
            let c = Connection::open(path.as_str())?;
            let (sql, has_source) = if source.is_some() {
                (
                    "SELECT id, source, collector_id, title, url, content, metadata, published_at, collected_at
                     FROM items
                     WHERE source = ?1 OR source LIKE ?1 || ':%' OR collector_id = ?1
                     ORDER BY collected_at DESC
                     LIMIT ?2 OFFSET ?3",
                    true,
                )
            } else {
                (
                    "SELECT id, source, collector_id, title, url, content, metadata, published_at, collected_at
                     FROM items
                     ORDER BY collected_at DESC
                     LIMIT ?1 OFFSET ?2",
                    false,
                )
            };
            let mut stmt = c.prepare(sql)?;
            let items = if has_source {
                stmt.query_map(
                    params![source.as_deref().unwrap_or(""), limit as i64, offset as i64],
                    row_to_feed_item,
                )?
                .filter_map(|r| r.ok())
                .collect()
            } else {
                stmt.query_map(params![limit as i64, offset as i64], row_to_feed_item)?
                    .filter_map(|r| r.ok())
                    .collect()
            };
            Ok(items)
        })
        .await?
    }

    pub async fn start_collector_run(&self, collector_id: &str) -> Result<String> {
        let id = Uuid::new_v4().to_string();
        let now = Utc::now().to_rfc3339();
        let path = self.path.clone();
        let cid = collector_id.to_string();
        let id2 = id.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            Connection::open(path.as_str())?.execute(
                "INSERT INTO collector_runs (id, collector_id, started_at, status) VALUES (?1,?2,?3,'running')",
                params![id2, cid, now],
            )?;
            Ok(())
        })
        .await??;
        Ok(id)
    }

    pub async fn finish_collector_run(
        &self,
        run_id: &str,
        items_count: u32,
        error: Option<&str>,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        let status = if error.is_some() { "error" } else { "success" };
        let path = self.path.clone();
        let rid = run_id.to_string();
        let err = error.map(|s| s.to_string());
        tokio::task::spawn_blocking(move || -> Result<()> {
            Connection::open(path.as_str())?.execute(
                "UPDATE collector_runs
                 SET finished_at = ?1, items_count = ?2, status = ?3, error = ?4
                 WHERE id = ?5",
                params![now, items_count, status, err, rid],
            )?;
            Ok(())
        })
        .await??;
        Ok(())
    }

    pub async fn get_collector_status(&self) -> Result<Vec<CollectorRun>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || -> Result<Vec<CollectorRun>> {
            let c = Connection::open(path.as_str())?;
            let mut stmt = c.prepare(
                "SELECT id, collector_id, started_at, finished_at, items_count, status, error
                 FROM collector_runs
                 ORDER BY started_at DESC
                 LIMIT 50",
            )?;
            let runs = stmt
                .query_map([], |r| {
                    Ok(CollectorRun {
                        id: r.get(0)?,
                        collector_id: r.get(1)?,
                        started_at: parse_dt(r.get::<_, String>(2)?).unwrap_or_else(Utc::now),
                        finished_at: r.get::<_, Option<String>>(3)?.and_then(parse_dt),
                        items_count: r.get::<_, i64>(4)? as u32,
                        status: r.get(5)?,
                        error: r.get(6)?,
                    })
                })?
                .filter_map(|r| r.ok())
                .collect();
            Ok(runs)
        })
        .await?
    }

    pub async fn get_user_feeds(&self) -> Result<Vec<(String, String)>> {
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || -> Result<Vec<(String, String)>> {
            let c = Connection::open(path.as_str())?;
            let mut stmt = c.prepare("SELECT name, url FROM user_feeds ORDER BY name")?;
            let rows = stmt
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                .filter_map(|r| r.ok())
                .collect();
            Ok(rows)
        })
        .await?
    }

    pub async fn add_user_feed(&self, name: &str, url: &str) -> Result<()> {
        let path = self.path.clone();
        let n = name.to_string();
        let u = url.to_string();
        tokio::task::spawn_blocking(move || -> Result<()> {
            Connection::open(path.as_str())?.execute(
                "INSERT OR REPLACE INTO user_feeds (name, url) VALUES (?1, ?2)",
                params![n, u],
            )?;
            Ok(())
        })
        .await??;
        Ok(())
    }

    pub async fn remove_user_feed(&self, url: &str) -> Result<()> {
        let path = self.path.clone();
        let u = url.to_string();
        tokio::task::spawn_blocking(move || -> Result<()> {
            Connection::open(path.as_str())?
                .execute("DELETE FROM user_feeds WHERE url = ?1", params![u])?;
            Ok(())
        })
        .await??;
        Ok(())
    }

    /// Drop items older than `cutoff` (RFC3339). Returns rows deleted.
    pub async fn purge_older_than(&self, cutoff_rfc3339: &str) -> Result<u64> {
        let path = self.path.clone();
        let cutoff = cutoff_rfc3339.to_string();
        let n = tokio::task::spawn_blocking(move || -> Result<u64> {
            let c = Connection::open(path.as_str())?;
            let n = c.execute(
                "DELETE FROM items WHERE collected_at < ?1",
                params![cutoff],
            )?;
            Ok(n as u64)
        })
        .await??;
        Ok(n)
    }
}

fn parse_dt(s: String) -> Option<chrono::DateTime<Utc>> {
    chrono::DateTime::parse_from_rfc3339(&s)
        .ok()
        .map(|d| d.with_timezone(&Utc))
}

fn row_to_feed_item(r: &rusqlite::Row<'_>) -> rusqlite::Result<FeedItem> {
    Ok(FeedItem {
        id: r.get(0)?,
        source: r.get(1)?,
        collector_id: r.get(2)?,
        title: r.get(3)?,
        url: r.get(4)?,
        content: r.get(5)?,
        metadata: serde_json::from_str(&r.get::<_, String>(6).unwrap_or_default())
            .unwrap_or_default(),
        published_at: r.get::<_, Option<String>>(7)?.and_then(parse_dt),
        collected_at: r
            .get::<_, String>(8)
            .ok()
            .and_then(parse_dt)
            .unwrap_or_else(Utc::now),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    async fn fresh_db() -> (TempDir, PulseDatabase) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("pulse.db");
        let db = PulseDatabase::new(path.to_str().unwrap()).await.unwrap();
        (dir, db)
    }

    fn raw_item(url: Option<&str>) -> RawItem {
        RawItem {
            source: "test".into(),
            collector_id: "test".into(),
            title: "hello".into(),
            url: url.map(String::from),
            content: Some("body".into()),
            metadata: json!({"k": "v"}),
            published_at: None,
        }
    }

    #[tokio::test]
    async fn insert_and_read_feed() {
        let (_d, db) = fresh_db().await;
        let _ = db.insert_item(&raw_item(Some("https://a.example/1"))).await.unwrap();
        let _ = db.insert_item(&raw_item(Some("https://a.example/2"))).await.unwrap();
        let feed = db.get_feed(10, 0, None).await.unwrap();
        assert_eq!(feed.len(), 2);
    }

    #[tokio::test]
    async fn item_exists_by_url_dedup() {
        let (_d, db) = fresh_db().await;
        db.insert_item(&raw_item(Some("https://a.example/dup"))).await.unwrap();
        assert!(db.item_exists_by_url("https://a.example/dup").await.unwrap());
        assert!(!db.item_exists_by_url("https://a.example/none").await.unwrap());
    }

    #[tokio::test]
    async fn collector_run_lifecycle() {
        let (_d, db) = fresh_db().await;
        let run = db.start_collector_run("rss").await.unwrap();
        db.finish_collector_run(&run, 5, None).await.unwrap();
        let runs = db.get_collector_status().await.unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0].items_count, 5);
        assert_eq!(runs[0].status, "success");
    }

    #[tokio::test]
    async fn collector_run_with_error() {
        let (_d, db) = fresh_db().await;
        let run = db.start_collector_run("rss").await.unwrap();
        db.finish_collector_run(&run, 0, Some("boom")).await.unwrap();
        let runs = db.get_collector_status().await.unwrap();
        assert_eq!(runs[0].status, "error");
        assert_eq!(runs[0].error.as_deref(), Some("boom"));
    }

    #[tokio::test]
    async fn user_feed_crud() {
        let (_d, db) = fresh_db().await;
        db.add_user_feed("HN", "https://hnrss.org").await.unwrap();
        db.add_user_feed("Lobsters", "https://lobste.rs/rss").await.unwrap();
        let feeds = db.get_user_feeds().await.unwrap();
        assert_eq!(feeds.len(), 2);
        db.remove_user_feed("https://hnrss.org").await.unwrap();
        let after = db.get_user_feeds().await.unwrap();
        assert_eq!(after.len(), 1);
        assert_eq!(after[0].0, "Lobsters");
    }

    #[tokio::test]
    async fn feed_filters_by_source_and_paginates() {
        let (_d, db) = fresh_db().await;
        for i in 0..5 {
            let mut r = raw_item(Some(&format!("https://hn.example/{i}")));
            r.source = "hackernews".into();
            r.collector_id = "hackernews".into();
            db.insert_item(&r).await.unwrap();
        }
        for i in 0..3 {
            let mut r = raw_item(Some(&format!("https://rss.example/{i}")));
            r.source = "rss:lobsters".into();
            r.collector_id = "rss".into();
            db.insert_item(&r).await.unwrap();
        }
        let only_hn = db.get_feed(10, 0, Some("hackernews")).await.unwrap();
        assert_eq!(only_hn.len(), 5);
        let only_rss = db.get_feed(10, 0, Some("rss")).await.unwrap();
        assert_eq!(only_rss.len(), 3);
        let page1 = db.get_feed(2, 0, None).await.unwrap();
        let page2 = db.get_feed(2, 2, None).await.unwrap();
        assert_eq!(page1.len(), 2);
        assert_eq!(page2.len(), 2);
        assert_ne!(page1[0].id, page2[0].id);
    }

    #[tokio::test]
    async fn purge_older_than_cutoff() {
        let (_d, db) = fresh_db().await;
        db.insert_item(&raw_item(Some("https://a.example/1"))).await.unwrap();
        // Future cutoff → everything is "older" than it
        let future = (Utc::now() + chrono::Duration::days(1)).to_rfc3339();
        let n = db.purge_older_than(&future).await.unwrap();
        assert_eq!(n, 1);
        let feed = db.get_feed(10, 0, None).await.unwrap();
        assert!(feed.is_empty());
    }
}
