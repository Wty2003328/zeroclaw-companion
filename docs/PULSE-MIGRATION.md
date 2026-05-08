# Pulse — what's done, what's next

The Pulse subsystem is now part of `zeroclaw-companion` proper. The
fork's old `src/pulse/` does not need to exist anywhere else.

## What's in `companion-pulse`

- **Storage** (`storage.rs`) — SQLite-backed `PulseDatabase` with tables
  for items, collector runs, and user-managed feeds. WAL mode, async
  via `spawn_blocking`. 7 unit tests covering insert / dedup / pagination
  / source filter / collector run lifecycle / purge.
- **Models** (`models.rs`) — `RawItem`, `Item`, `FeedItem`, `CollectorRun`.
- **Config** (`config.rs`) — `[pulse]` block in `companion.toml` with
  per-collector knobs. 2 unit tests for parsing.
- **Scheduler** (`scheduler.rs`) — one tokio task per enabled collector,
  configurable cadence, run logs persisted, manual `trigger_collector` API.
- **Collector trait** (`collectors/mod.rs`) — `id`/`name`/`default_interval`/
  `enabled`/`collect`. `parse_interval("30m" / "1h" / "45s")` helper. 5
  unit tests.

## Collectors shipped

- `rss` (`collectors/rss.rs`) — RSS / Atom via `feed-rs`. Tests parse
  canned XML so we don't hit the network in CI.
- `hackernews` (`collectors/hackernews.rs`) — top stories filtered by
  score; `item_to_raw` is public so its conversion logic is unit-tested
  without the API.

## Collectors not yet ported

Each was in the fork; not migrated because it depends on a third-party
API key or a more involved implementation:

| Collector | Why deferred |
|---|---|
| `weather` | Needs an OpenWeatherMap-class API key; ~300 LOC |
| `stocks`  | Needs an Alpha Vantage / Finnhub key; ~170 LOC |
| `github`  | Auth + rate limits; ~210 LOC |
| `reddit`  | Needs Reddit API auth; ~150 LOC |
| `videos`  | Hot-reloads channels from DB and uses YouTube/Bilibili APIs; ~235 LOC |

To port any of them: copy the file from the fork, rewrite imports
(`crate::pulse::config::FooConfig` → `crate::config::FooConfig`,
`crate::pulse::models::RawItem` → `crate::models::RawItem`),
add the `FooConfig` to `companion-pulse/src/config.rs`'s `CollectorsConfig`
struct, register in `PulseSubsystem::start`, write unit tests against the
public `item_to_raw`-style entry point.

## REST API

Mounted at `/api/pulse/*` only when `[pulse] enabled = true`:

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/pulse/feed?limit=&offset=&source=` | Recent items |
| GET    | `/api/pulse/status`                      | Collectors + run history |
| POST   | `/api/pulse/trigger/{id}`                | Manual collector run |
| GET    | `/api/pulse/feeds`                       | User-managed RSS feed list |
| POST   | `/api/pulse/feeds`                       | Add a feed |
| DELETE | `/api/pulse/feeds?url=...`               | Remove a feed |

## Frontend

`web/src/pages/Pulse.tsx` renders:
- Header with source filter dropdown + manual refresh button
- Per-collector cards (interval, last-run status, "Run now" button)
- Recent items list (title, source, timestamp, content excerpt, link)

Polls `/api/pulse/feed` and `/api/pulse/status` every 30s.

## What's deliberately out of scope

The fork had AI-summarization + relevance-scoring layers (`models.rs::Score`,
`Summary`, `Tag`). They're omitted here — if you want LLM-driven curation,
the avatar subagent is already a good template; build a Pulse summarizer
the same way (companion-pulse adds a method, calls `companion_core::LlmClient`).
Saved for a follow-up because shipping the basic feed is more useful first.
