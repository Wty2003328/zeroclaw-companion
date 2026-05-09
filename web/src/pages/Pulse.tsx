import { useEffect, useState, useCallback } from 'react';
import { HTTP_BASE } from '../lib/apiBase';
import { openExternal } from '../lib/tauriShell';
import { cachedJson, invalidateCache } from '../lib/fetchCache';

// ── Types mirroring /api/pulse responses ─────────────────────────

interface FeedItem {
  id: string;
  source: string;
  collector_id: string;
  title: string;
  url: string | null;
  content: string | null;
  metadata: Record<string, unknown>;
  published_at: string | null;
  collected_at: string;
  /** ISO timestamp; null = unread. */
  read_at: string | null;
  /** Cached LLM summary, populated by POST /items/{id}/summarize. */
  summary?: string | null;
}

/** One-click suggestions in the Sources tab. Each entry maps to an
 *  RSS feed URL. Curated to cover common categories (programming,
 *  research, microblog) without committing to any particular site. */
const SUGGESTED_FEEDS: { name: string; url: string; tag: string }[] = [
  { name: 'Hacker News (front page)', url: 'https://hnrss.org/frontpage', tag: 'tech' },
  { name: 'Lobsters', url: 'https://lobste.rs/rss', tag: 'tech' },
  { name: 'r/rust', url: 'https://www.reddit.com/r/rust/.rss', tag: 'reddit' },
  { name: 'r/programming', url: 'https://www.reddit.com/r/programming/.rss', tag: 'reddit' },
  { name: 'r/MachineLearning', url: 'https://www.reddit.com/r/MachineLearning/.rss', tag: 'reddit' },
  { name: 'arXiv cs.AI new', url: 'https://export.arxiv.org/rss/cs.AI', tag: 'research' },
  { name: 'arXiv cs.LG new', url: 'https://export.arxiv.org/rss/cs.LG', tag: 'research' },
  { name: 'Mastodon · @anthropic@x.com (example)', url: 'https://mastodon.social/@anthropic.rss', tag: 'mastodon' },
];
interface CollectorInfo { id: string; name: string; enabled: boolean; interval_secs: number; }
interface CollectorRun {
  id: string;
  collector_id: string;
  started_at: string;
  finished_at: string | null;
  items_count: number;
  status: string;
  error: string | null;
}
interface PulseStatus { collectors: CollectorInfo[]; runs: CollectorRun[]; }
interface RssFeed { name: string; url: string; }
interface VideoChannel { platform: string; channel_id: string; display_name: string; }

type Tab = 'feed' | 'sources';

// ── Page ─────────────────────────────────────────────────────────

export default function Pulse() {
  const [tab, setTab] = useState<Tab>('feed');
  const [error, setError] = useState<string | null>(null);

  return (
    <div style={{
      flex: '1 1 0', minHeight: 0, overflow: 'auto',
      contain: 'paint',
      overscrollBehavior: 'contain',
    }}>
      <div style={{ padding: 24, maxWidth: 1100, margin: '0 auto' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
          <h1 style={{ margin: 0, fontSize: 24 }}>Pulse</h1>
          <span style={{ flex: 1 }} />
          <Tabs current={tab} onChange={setTab} />
        </div>

        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

        {tab === 'feed' && <FeedTab onError={setError} />}
        {tab === 'sources' && <SourcesTab onError={setError} />}
      </div>
    </div>
  );
}

function Tabs({ current, onChange }: { current: Tab; onChange: (t: Tab) => void }) {
  const items: { id: Tab; label: string }[] = [
    { id: 'feed', label: 'Feed' },
    { id: 'sources', label: 'Sources' },
  ];
  return (
    <div style={{ display: 'flex', gap: 4, background: '#16181c', padding: 4, borderRadius: 8, border: '1px solid #2a2d33' }}>
      {items.map((it) => (
        <button
          key={it.id}
          type="button"
          onClick={() => onChange(it.id)}
          style={{
            padding: '6px 14px',
            borderRadius: 6,
            border: 'none',
            background: current === it.id ? '#3b82f6' : 'transparent',
            color: current === it.id ? '#fff' : '#aaa',
            fontSize: 13,
            cursor: 'pointer',
          }}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

// ── Feed tab ─────────────────────────────────────────────────────

function FeedTab({ onError }: { onError: (m: string) => void }) {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [status, setStatus] = useState<PulseStatus | null>(null);
  const [filter, setFilter] = useState('');
  const [search, setSearch] = useState('');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [unreadCount, setUnreadCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [openItemId, setOpenItemId] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: '100' });
      if (filter) params.set('source', filter);
      // Server-side search keeps results consistent when the working
      // set exceeds the 100-row client window. Empty string is
      // treated as no filter by the server.
      if (search.trim()) params.set('search', search.trim());
      if (unreadOnly) params.set('unread', '1');

      // Route reads through the SWR cache: the first call after boot
      // hits the prewarm entry instantly; subsequent calls dedupe
      // in-flight requests and respect the 5s TTL. Pulse-disabled
      // detection used to rely on the raw Response's content-type;
      // with cached JSON we catch the parse error from the SPA's
      // index.html fallback instead.
      const feedUrl = `${HTTP_BASE}/api/pulse/feed?${params}`;
      const statusUrl = `${HTTP_BASE}/api/pulse/status`;
      const unreadUrl = `${HTTP_BASE}/api/pulse/unread_count`;
      const [feed, stat, unread] = await Promise.all([
        cachedJson<{ items?: FeedItem[] }>(feedUrl, { ttlMs: 5_000 }),
        cachedJson<PulseStatus>(statusUrl, { ttlMs: 5_000 }),
        cachedJson<{ unread?: number }>(unreadUrl, { ttlMs: 5_000 }).catch(() => ({ unread: undefined })),
      ]);
      setItems(feed.items ?? []);
      setStatus(stat);
      if (unread && typeof unread.unread === 'number') setUnreadCount(unread.unread);
    } catch (e) {
      const msg = (e as Error).message;
      // Pulse-disabled fallback: the SPA serves index.html on /api/*
      // when the subsystem is off, which fails JSON parsing.
      if (/unexpected token|JSON|Unexpected end/i.test(msg)) {
        onError('Pulse is turned off. Open companion.toml and set [pulse] enabled = true to use this page.');
        setItems([]); setStatus(null);
      } else {
        onError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [filter, search, unreadOnly, onError]);

  useEffect(() => {
    void fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const trigger = async (cid: string) => {
    try {
      await fetch(`${HTTP_BASE}/api/pulse/trigger/${cid}`, { method: 'POST' });
      // Server-side collector run finishes async; wait a beat then
      // invalidate so the next fetchAll picks up new items.
      setTimeout(() => {
        invalidateCache(`${HTTP_BASE}/api/pulse/feed`);
        invalidateCache(`${HTTP_BASE}/api/pulse/status`);
        void fetchAll();
      }, 1000);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const setRead = async (id: string, read: boolean) => {
    try {
      await fetch(`${HTTP_BASE}/api/pulse/items/${encodeURIComponent(id)}/read`, {
        method: read ? 'POST' : 'DELETE',
      });
      // Optimistic update so the UI doesn't flicker.
      setItems((cur) =>
        cur.map((it) =>
          it.id === id ? { ...it, read_at: read ? new Date().toISOString() : null } : it,
        ),
      );
      setUnreadCount((c) => (c == null ? c : Math.max(0, c + (read ? -1 : 1))));
      // Mark cached feed/unread stale so background revalidation
      // mirrors the optimistic patch above.
      invalidateCache(`${HTTP_BASE}/api/pulse/feed`);
      invalidateCache(`${HTTP_BASE}/api/pulse/unread_count`);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  /** Generate (or fetch cached) LLM summary for an item.
   *  Returns the summary text or throws. The drawer drives loading
   *  state locally so multiple drawer opens don't stomp each other. */
  const summarize = async (id: string, force = false): Promise<string> => {
    const url = `${HTTP_BASE}/api/pulse/items/${encodeURIComponent(id)}/summarize${force ? '?force=1' : ''}`;
    const r = await fetch(url, { method: 'POST' });
    if (!r.ok) {
      // 503 = no summarizer wired up (no LLM key + no zeroclaw webhook).
      // Surface a hint instead of the raw status.
      if (r.status === 503) {
        throw new Error(
          "AI summary isn't configured yet. Open Settings → Translation & " +
          "expressions and either add an API key (Direct AI), or switch to " +
          '"Through main agent" if you have the main agent running.',
        );
      }
      const txt = await r.text().catch(() => '');
      throw new Error(`summarize ${r.status}: ${txt || r.statusText}`);
    }
    const j = await r.json();
    const summary: string = j.summary ?? '';
    setItems((cur) => cur.map((it) => (it.id === id ? { ...it, summary } : it)));
    return summary;
  };

  const markAllRead = async () => {
    try {
      const r = await fetch(`${HTTP_BASE}/api/pulse/items/read_all`, { method: 'POST' });
      if (!r.ok) throw new Error(`read_all ${r.status}`);
      invalidateCache(`${HTTP_BASE}/api/pulse/feed`);
      invalidateCache(`${HTTP_BASE}/api/pulse/unread_count`);
      void fetchAll();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const openItem = items.find((it) => it.id === openItemId) ?? null;

  return (
    <>
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          type="search"
          placeholder="Search title / body…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            flex: '1 1 240px',
            background: '#0b0d10',
            color: '#fff',
            padding: '8px 12px',
            borderRadius: 6,
            border: '1px solid #2a2d33',
            fontSize: 13,
            outline: 'none',
          }}
        />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{
            background: '#16181c',
            color: '#fff',
            border: '1px solid #2a2d33',
            padding: '7px 10px',
            borderRadius: 6,
            fontSize: 13,
          }}
        >
          <option value="">All sources</option>
          {status?.collectors.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, color: '#cbd5e1', cursor: 'pointer', padding: '0 6px' }}>
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          Unread only
          {unreadCount != null && (
            <span style={{ color: unreadCount > 0 ? '#3b82f6' : '#666', fontWeight: 600 }}>({unreadCount})</span>
          )}
        </label>
        {unreadCount != null && unreadCount > 0 && (
          <button type="button" onClick={markAllRead} style={refreshBtn(false)}>
            Mark all read
          </button>
        )}
        <button
          type="button"
          onClick={fetchAll}
          disabled={loading}
          style={refreshBtn(loading)}
        >
          {loading ? '…' : 'Refresh'}
        </button>
      </div>

      {status && (
        <section style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 12, color: '#888', marginBottom: 8, fontWeight: 500, textTransform: 'uppercase', letterSpacing: 0.5 }}>
            Collectors
          </h2>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {status.collectors.map((c) => (
              <CollectorChip
                key={c.id}
                collector={c}
                lastRun={status.runs.find((r) => r.collector_id === c.id)}
                onTrigger={() => trigger(c.id)}
              />
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 style={{ fontSize: 12, color: '#888', marginBottom: 8, fontWeight: 500, textTransform: 'uppercase', letterSpacing: 0.5 }}>
          {search || unreadOnly
            ? `Filtered (${items.length})`
            : `Recent items (${items.length})`}
        </h2>
        {items.length === 0 && !loading && (
          <div style={{ color: '#666', fontSize: 13 }}>
            {search
              ? `No items match "${search}".`
              : unreadOnly
                ? "All caught up — nothing unread."
                : 'No items yet. Click "Run now" on a source above to fetch some, or wait a few minutes.'}
          </div>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {items.map((item) => (
            <FeedRow
              key={item.id}
              item={item}
              onClick={() => {
                setOpenItemId(item.id);
                if (!item.read_at) void setRead(item.id, true);
              }}
              onToggleRead={() => setRead(item.id, !item.read_at)}
            />
          ))}
        </div>
      </section>

      {openItem && (
        <ItemDetailDrawer
          item={openItem}
          onClose={() => setOpenItemId(null)}
          onToggleRead={() => setRead(openItem.id, !openItem.read_at)}
          onSummarize={(force) => summarize(openItem.id, force)}
        />
      )}
    </>
  );
}

function CollectorChip({
  collector, lastRun, onTrigger,
}: {
  collector: CollectorInfo;
  lastRun: CollectorRun | undefined;
  onTrigger: () => void;
}) {
  const tone = lastRun?.status === 'error' ? '#fca5a5' : lastRun?.status === 'ok' ? '#10b981' : '#888';
  return (
    <div style={{ padding: '10px 14px', background: '#16181c', borderRadius: 8, fontSize: 12, border: '1px solid #2a2d33', minWidth: 180 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 2 }}>{collector.name}</div>
      <div style={{ color: '#888' }}>every {fmtInterval(collector.interval_secs)} · {collector.enabled ? 'on' : 'off'}</div>
      <div style={{ color: tone, marginTop: 2 }}>
        last: {lastRun ? `${lastRun.items_count} items · ${lastRun.status}` : '—'}
      </div>
      <button type="button" onClick={onTrigger} style={runBtn}>Run now</button>
    </div>
  );
}

// ── Sources tab — RSS feeds + Video channels ─────────────────────

function SourcesTab({ onError }: { onError: (m: string) => void }) {
  const [feeds, setFeeds] = useState<RssFeed[]>([]);
  const [videos, setVideos] = useState<VideoChannel[]>([]);

  const reload = useCallback(async () => {
    try {
      const [fr, vr] = await Promise.all([
        fetch(`${HTTP_BASE}/api/pulse/feeds`),
        fetch(`${HTTP_BASE}/api/pulse/videos`),
      ]);
      if (!fr.ok || !vr.ok) throw new Error('list failed');
      const fj = await fr.json();
      const vj = await vr.json();
      setFeeds(fj.feeds ?? []);
      setVideos(vj.videos ?? []);
    } catch (e) {
      onError((e as Error).message);
    }
  }, [onError]);
  useEffect(() => { void reload(); }, [reload]);

  return (
    <div style={{ display: 'grid', gap: 24, gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))' }}>
      <RssFeedsPanel feeds={feeds} reload={reload} onError={onError} />
      <VideoChannelsPanel videos={videos} reload={reload} onError={onError} />
    </div>
  );
}

function RssFeedsPanel({ feeds, reload, onError }: { feeds: RssFeed[]; reload: () => void; onError: (m: string) => void }) {
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [showSuggestions, setShowSuggestions] = useState(false);
  const subscribed = new Set(feeds.map((f) => f.url));

  const post = async (payload: { name: string; url: string }) => {
    const r = await fetch(`${HTTP_BASE}/api/pulse/feeds`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
  };

  const add = async () => {
    if (!name.trim() || !url.trim()) return;
    try {
      await post({ name: name.trim(), url: url.trim() });
      setName(''); setUrl('');
      reload();
    } catch (e) { onError((e as Error).message); }
  };

  const addSuggestion = async (s: typeof SUGGESTED_FEEDS[number]) => {
    try {
      await post({ name: s.name, url: s.url });
      reload();
    } catch (e) { onError((e as Error).message); }
  };

  const remove = async (u: string) => {
    try {
      const r = await fetch(`${HTTP_BASE}/api/pulse/feeds?url=${encodeURIComponent(u)}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(await r.text());
      reload();
    } catch (e) { onError((e as Error).message); }
  };

  return (
    <Panel title="RSS feeds">
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Display name" style={inputStyle} />
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/feed.xml" style={inputStyle} />
        <button type="button" onClick={add} style={primaryBtn}>Add</button>
      </div>

      <button
        type="button"
        onClick={() => setShowSuggestions((s) => !s)}
        style={{
          fontSize: 11, background: 'transparent', color: '#7aa9ff',
          border: 'none', cursor: 'pointer', padding: '0 0 8px 0',
          textAlign: 'left',
        }}
      >
        {showSuggestions ? '▾' : '▸'} Suggested feeds
      </button>

      {showSuggestions && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 12 }}>
          {SUGGESTED_FEEDS.map((s) => {
            const already = subscribed.has(s.url);
            return (
              <button
                key={s.url}
                type="button"
                onClick={() => addSuggestion(s)}
                disabled={already}
                title={s.url}
                style={{
                  fontSize: 11,
                  padding: '4px 8px',
                  background: already ? '#0b1322' : 'transparent',
                  color: already ? '#475569' : '#cbd5e1',
                  border: '1px solid #2a2d33',
                  borderRadius: 4,
                  cursor: already ? 'default' : 'pointer',
                }}
              >
                {already ? '✓ ' : '+ '}{s.name}
                <span style={{ color: '#475569', marginLeft: 6 }}>{s.tag}</span>
              </button>
            );
          })}
        </div>
      )}

      {feeds.length === 0 ? (
        <div style={{ fontSize: 12, color: '#666' }}>No user-managed feeds. The collector also runs the static list in companion.toml.</div>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {feeds.map((f) => (
            <li key={f.url} style={rowStyle}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>{f.name}</div>
                <div style={{ fontSize: 11, color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {f.url}
                </div>
              </div>
              <button type="button" onClick={() => remove(f.url)} style={dangerBtn}>Remove</button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function VideoChannelsPanel({ videos, reload, onError }: { videos: VideoChannel[]; reload: () => void; onError: (m: string) => void }) {
  const [platform, setPlatform] = useState<'youtube' | 'bilibili'>('youtube');
  const [channelId, setChannelId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const add = async () => {
    if (!channelId.trim() || !displayName.trim()) return;
    try {
      const r = await fetch(`${HTTP_BASE}/api/pulse/videos`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform, channel_id: channelId.trim(), display_name: displayName.trim() }),
      });
      if (!r.ok) throw new Error(await r.text());
      setChannelId(''); setDisplayName('');
      reload();
    } catch (e) { onError((e as Error).message); }
  };
  const remove = async (p: string, id: string) => {
    try {
      const u = new URLSearchParams({ platform: p, channel_id: id });
      const r = await fetch(`${HTTP_BASE}/api/pulse/videos?${u}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(await r.text());
      reload();
    } catch (e) { onError((e as Error).message); }
  };
  return (
    <Panel title="Video subscriptions">
      <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
        <select value={platform} onChange={(e) => setPlatform(e.target.value as 'youtube' | 'bilibili')} style={{ ...inputStyle, flex: '0 0 100px' }}>
          <option value="youtube">YouTube</option>
          <option value="bilibili">Bilibili</option>
        </select>
        <input value={channelId} onChange={(e) => setChannelId(e.target.value)} placeholder={platform === 'youtube' ? 'UC… channel ID' : 'Bilibili UID'} style={{ ...inputStyle, flex: 1 }} />
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Display name" style={inputStyle} />
        <button type="button" onClick={add} style={primaryBtn}>Add</button>
      </div>
      {videos.length === 0 ? (
        <div style={{ fontSize: 12, color: '#666' }}>No subscribed channels.</div>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {videos.map((v) => (
            <li key={`${v.platform}:${v.channel_id}`} style={rowStyle}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>{v.display_name}</div>
                <div style={{ fontSize: 11, color: '#666' }}>
                  <span style={{ color: '#888', marginRight: 6 }}>{v.platform}</span>{v.channel_id}
                </div>
              </div>
              <button type="button" onClick={() => remove(v.platform, v.channel_id)} style={dangerBtn}>Remove</button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// (Pulse Options tab removed — the only knob it exposed was a custom
// RSSHub URL for Bilibili videos, which most users don't need. The
// underlying server endpoint at /api/pulse/settings/rsshub_url still
// works for power users who want to set it via curl.)

// ── Shared bits ──────────────────────────────────────────────────

function FeedRow({
  item, onClick, onToggleRead,
}: {
  item: FeedItem;
  onClick: () => void;
  onToggleRead: () => void;
}) {
  const isRead = !!item.read_at;
  return (
    <article
      onClick={onClick}
      style={{
        padding: 14,
        background: isRead ? '#101216' : '#16181c',
        borderRadius: 8,
        border: '1px solid #1f2227',
        cursor: 'pointer',
        opacity: isRead ? 0.65 : 1,
        transition: 'opacity 120ms ease, background 120ms ease',
        // `contain: content` isolates this row's layout/style/paint
        // from its siblings — scrolling 100 items doesn't recompute
        // the layout of off-screen rows.
        contain: 'content',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
        <div style={{ fontSize: 11, color: '#888', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {item.source} · {fmtDate(item.published_at ?? item.collected_at)}
        </div>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onToggleRead(); }}
          title={isRead ? 'Click to mark unread' : 'Click to mark read'}
          style={{
            fontSize: 11,
            background: isRead ? 'transparent' : '#1e3a8a',
            color: isRead ? '#666' : '#cbd5e1',
            border: isRead ? '1px solid #2a2d33' : '1px solid #3b82f6',
            borderRadius: 4,
            padding: '2px 8px', cursor: 'pointer', flexShrink: 0,
          }}
        >
          {/* Label always reflects CURRENT state, not the toggle action.
              Tooltip carries the action ("Click to mark X"). */}
          {isRead ? '✓ read' : '● unread'}
        </button>
      </div>
      <div style={{ fontSize: 15, fontWeight: isRead ? 400 : 500, marginBottom: 4 }}>
        {item.title}
      </div>
      {item.content && (
        <div style={{ fontSize: 13, color: '#aaa', lineHeight: 1.5 }}>
          {stripHtml(item.content).slice(0, 280)}
          {item.content.length > 280 ? '…' : ''}
        </div>
      )}
    </article>
  );
}

function ItemDetailDrawer({
  item, onClose, onToggleRead, onSummarize,
}: {
  item: FeedItem;
  onClose: () => void;
  onToggleRead: () => void;
  /** Calls the agent and persists the summary. `force=true` re-runs
   *  even if a cached summary already exists. Returns the summary text
   *  for the caller to surface errors against. */
  onSummarize: (force: boolean) => Promise<string>;
}) {
  // Esc closes; click outside closes (the dimmed overlay does it).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const [summarizing, setSummarizing] = useState(false);
  const [summarizeErr, setSummarizeErr] = useState<string | null>(null);
  const runSummarize = async (force: boolean) => {
    if (summarizing) return;
    setSummarizing(true);
    setSummarizeErr(null);
    try {
      await onSummarize(force);
    } catch (e) {
      setSummarizeErr((e as Error).message);
    } finally {
      setSummarizing(false);
    }
  };

  const meta = item.metadata as Record<string, unknown>;
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
        display: 'flex', justifyContent: 'flex-end', zIndex: 50,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(640px, 92vw)', height: '100%',
          background: '#0e1014', borderLeft: '1px solid #2a2d33',
          padding: 24, overflowY: 'auto',
          display: 'flex', flexDirection: 'column', gap: 12,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>
              {item.source} · {fmtDate(item.published_at ?? item.collected_at)}
            </div>
            <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600, lineHeight: 1.3 }}>
              {item.title}
            </h2>
          </div>
          <button type="button" onClick={onClose} style={{
            background: 'transparent', color: '#888', border: 'none',
            fontSize: 20, cursor: 'pointer', padding: 0, marginLeft: 8,
          }} aria-label="Close">×</button>
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {item.url && (
            <button
              type="button"
              onClick={() => void openExternal(item.url!)}
              style={{
                padding: '6px 12px', background: '#3b82f6', color: '#fff',
                border: 'none', borderRadius: 6, fontSize: 12, cursor: 'pointer',
              }}
            >
              Open ↗
            </button>
          )}
          <button type="button" onClick={onToggleRead} style={{
            padding: '6px 12px', background: 'transparent', color: '#aaa',
            border: '1px solid #2a2d33', borderRadius: 6, fontSize: 12,
            cursor: 'pointer',
          }}>
            {item.read_at ? 'Mark unread' : 'Mark read'}
          </button>
          <button
            type="button"
            disabled={summarizing}
            onClick={() => void runSummarize(!!item.summary)}
            title={item.summary ? 'Regenerate summary (force=1)' : 'Summarize via agent'}
            style={{
              padding: '6px 12px',
              background: item.summary ? 'transparent' : '#7c3aed',
              color: item.summary ? '#a78bfa' : '#fff',
              border: item.summary ? '1px solid #4c2a91' : 'none',
              borderRadius: 6, fontSize: 12,
              cursor: summarizing ? 'wait' : 'pointer',
              opacity: summarizing ? 0.6 : 1,
            }}
          >
            {summarizing
              ? 'Summarizing…'
              : item.summary ? '↻ Re-summarize' : '✨ Summarize'}
          </button>
        </div>

        {(item.summary || summarizeErr) && (
          <div
            data-testid="summary-block"
            style={{
              background: '#1a1330', border: '1px solid #4c2a91',
              borderRadius: 8, padding: 14, fontSize: 13, lineHeight: 1.6,
              color: '#e9d5ff', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}
          >
            <div style={{ fontSize: 11, color: '#a78bfa', marginBottom: 6, fontWeight: 600 }}>
              AGENT SUMMARY
            </div>
            {summarizeErr ? (
              <div style={{ color: '#fca5a5' }}>{summarizeErr}</div>
            ) : (
              item.summary
            )}
          </div>
        )}

        {item.content && (
          <div style={{
            background: '#16181c', border: '1px solid #1f2227',
            borderRadius: 8, padding: 14, fontSize: 13, lineHeight: 1.6,
            color: '#cbd5e1', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>
            {stripHtml(item.content)}
          </div>
        )}

        <details style={{ fontSize: 11, color: '#888' }}>
          <summary style={{ cursor: 'pointer', userSelect: 'none' }}>metadata</summary>
          <pre style={{
            background: '#0b0d10', border: '1px solid #1f2227',
            borderRadius: 6, padding: 10, marginTop: 6,
            maxHeight: 240, overflow: 'auto',
            fontSize: 11, color: '#94a3b8',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>{JSON.stringify(meta, null, 2)}</pre>
        </details>
      </div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ background: '#16181c', borderRadius: 10, padding: 16, border: '1px solid #1f2227' }}>
      <h2 style={{ margin: '0 0 12px 0', fontSize: 14, fontWeight: 600 }}>{title}</h2>
      {children}
    </section>
  );
}

function ErrorBanner({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  return (
    <div style={{ padding: 12, background: '#1f1316', color: '#fca5a5', borderRadius: 8, marginBottom: 16, fontSize: 13, display: 'flex', alignItems: 'center', gap: 12 }}>
      <span style={{ flex: 1 }}>{message}</span>
      <button type="button" onClick={onDismiss} style={{ background: 'transparent', color: '#fca5a5', border: '1px solid #5a2a2a', borderRadius: 4, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }}>
        dismiss
      </button>
    </div>
  );
}

function fmtDate(iso: string): string {
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}
function fmtInterval(secs: number): string {
  if (secs >= 3600) return `${Math.round(secs / 3600)}h`;
  if (secs >= 60) return `${Math.round(secs / 60)}m`;
  return `${secs}s`;
}
function stripHtml(html: string): string {
  return html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

const inputStyle: React.CSSProperties = {
  background: '#0b0d10', color: '#fff', padding: '6px 10px', borderRadius: 6,
  border: '1px solid #2a2d33', fontSize: 12, outline: 'none', flex: 1, minWidth: 0,
};
const primaryBtn: React.CSSProperties = {
  padding: '6px 14px', background: '#3b82f6', color: '#fff', border: 'none',
  borderRadius: 6, fontSize: 12, cursor: 'pointer', flexShrink: 0,
};
const dangerBtn: React.CSSProperties = {
  padding: '4px 10px', background: 'transparent', color: '#fca5a5', border: '1px solid #4b2a2a',
  borderRadius: 4, fontSize: 11, cursor: 'pointer', flexShrink: 0,
};
const runBtn: React.CSSProperties = {
  marginTop: 8, padding: '4px 10px', background: '#3b82f6', color: '#fff', border: 'none',
  borderRadius: 4, fontSize: 11, cursor: 'pointer',
};
const refreshBtn = (loading: boolean): React.CSSProperties => ({
  padding: '7px 14px', background: '#1f2937', color: '#fff', border: 'none',
  borderRadius: 6, fontSize: 13, cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.5 : 1,
});
const rowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px',
  background: '#0e1014', borderRadius: 4, border: '1px solid #1f2227',
};
