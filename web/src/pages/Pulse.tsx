import { useEffect, useState, useCallback } from 'react';

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
}

interface PulseStatus {
  collectors: Array<{ id: string; name: string; enabled: boolean; interval_secs: number }>;
  runs: Array<{
    id: string;
    collector_id: string;
    started_at: string;
    finished_at: string | null;
    items_count: number;
    status: string;
    error: string | null;
  }>;
}

export default function Pulse() {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [status, setStatus] = useState<PulseStatus | null>(null);
  const [filter, setFilter] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: '50' });
      if (filter) params.set('source', filter);
      const [feedR, statusR] = await Promise.all([
        fetch(`/api/pulse/feed?${params}`),
        fetch(`/api/pulse/status`),
      ]);
      if (feedR.status === 404 || statusR.status === 404) {
        setError('Pulse is disabled in companion.toml. Set [pulse] enabled = true to use this.');
        setItems([]);
        setStatus(null);
        return;
      }
      if (!feedR.ok) throw new Error(`feed ${feedR.status}`);
      if (!statusR.ok) throw new Error(`status ${statusR.status}`);
      const feed = await feedR.json();
      const stat = await statusR.json();
      setItems(feed.items ?? []);
      setStatus(stat);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const trigger = async (cid: string) => {
    await fetch(`/api/pulse/trigger/${cid}`, { method: 'POST' });
    setTimeout(fetchAll, 1000);
  };

  return (
    <div style={{ padding: 24, maxWidth: 1100, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
        <h1 style={{ margin: 0, fontSize: 24 }}>Pulse</h1>
        <span style={{ flex: 1 }} />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{
            background: '#16181c',
            color: '#fff',
            border: '1px solid #2a2d33',
            padding: '6px 10px',
            borderRadius: 6,
            fontSize: 13,
          }}
        >
          <option value="">All sources</option>
          {status?.collectors.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={fetchAll}
          disabled={loading}
          style={{
            padding: '6px 12px',
            background: '#1f2937',
            color: '#fff',
            border: 'none',
            borderRadius: 6,
            fontSize: 13,
            cursor: 'pointer',
          }}
        >
          {loading ? '…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div
          style={{
            padding: 16,
            background: '#1f1316',
            color: '#fca5a5',
            borderRadius: 8,
            marginBottom: 16,
            fontSize: 14,
          }}
        >
          {error}
        </div>
      )}

      {status && (
        <section style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 14, color: '#aaa', marginBottom: 8 }}>Collectors</h2>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {status.collectors.map((c) => {
              const lastRun = status.runs.find((r) => r.collector_id === c.id);
              return (
                <div
                  key={c.id}
                  style={{
                    padding: '8px 12px',
                    background: '#16181c',
                    borderRadius: 8,
                    fontSize: 12,
                    border: '1px solid #2a2d33',
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{c.name}</div>
                  <div style={{ color: '#888', marginTop: 2 }}>
                    every {c.interval_secs}s · {c.enabled ? 'on' : 'off'}
                  </div>
                  <div style={{ color: '#888' }}>
                    last:{' '}
                    {lastRun
                      ? `${lastRun.items_count} items (${lastRun.status})`
                      : '—'}
                  </div>
                  <button
                    type="button"
                    onClick={() => trigger(c.id)}
                    style={{
                      marginTop: 6,
                      padding: '4px 8px',
                      background: '#3b82f6',
                      color: '#fff',
                      border: 'none',
                      borderRadius: 4,
                      fontSize: 11,
                      cursor: 'pointer',
                    }}
                  >
                    Run now
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <section>
        <h2 style={{ fontSize: 14, color: '#aaa', marginBottom: 8 }}>Recent items</h2>
        {items.length === 0 && !loading && (
          <div style={{ color: '#666', fontSize: 13 }}>
            No items yet. Wait for the next collector tick or click "Run now" above.
          </div>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {items.map((item) => (
            <FeedRow key={item.id} item={item} />
          ))}
        </div>
      </section>
    </div>
  );
}

function FeedRow({ item }: { item: FeedItem }) {
  return (
    <article
      style={{
        padding: 14,
        background: '#16181c',
        borderRadius: 8,
        border: '1px solid #1f2227',
      }}
    >
      <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>
        {item.source} · {fmtDate(item.published_at ?? item.collected_at)}
      </div>
      <div style={{ fontSize: 15, fontWeight: 500, marginBottom: 4 }}>
        {item.url ? (
          <a href={item.url} target="_blank" rel="noreferrer" style={{ color: '#fff', textDecoration: 'none' }}>
            {item.title}
          </a>
        ) : (
          item.title
        )}
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

function fmtDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function stripHtml(html: string): string {
  return html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}
