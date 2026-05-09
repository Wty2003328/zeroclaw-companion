import { useEffect, useState } from 'react';
import {
  HTTP_BASE,
  getDefaultServerUrl,
  getServerUrl,
  getStoredServerUrl,
  setStoredServerUrl,
} from '../lib/apiBase';
import { cachedJson, useCachedJson } from '../lib/fetchCache';
import CharacterRoster from '../components/CharacterRoster';

interface CompanionStatus {
  ok: boolean;
  zeroclaw_up: boolean;
  avatar_enabled: boolean;
  pulse_enabled?: boolean;
}

/**
 * Home — the main page. Used to be a status dashboard with deep-links
 * to the other pages, but the rest of the UI was just plumbing around
 * the active character anyway, so as of 2026-05 the character roster
 * is the primary content and status / server-connection are tucked
 * into collapsible panels at the top.
 */
export default function Home() {
  return (
    // Outer: scrollable flex item that fills the routes wrapper.
    // Inner: centered content with max-width + horizontal padding.
    <div style={{
      flex: '1 1 0', minHeight: 0, overflow: 'auto',
      // Promote scroll container to its own compositor layer + clip
      // repaints inside it. Without `contain: paint`, scrolling here
      // can invalidate the entire window's paint tree on each frame.
      contain: 'paint',
      overscrollBehavior: 'contain',
    }}>
      <div style={{ padding: 28, maxWidth: 880, margin: '0 auto' }}>
        <h1 style={{ marginTop: 0, marginBottom: 12, fontSize: 24 }}>zeroclaw companion</h1>
        <SystemPanels />
        <div style={{ marginTop: 20 }}>
          <CharacterRoster />
        </div>
      </div>
    </div>
  );
}

/** Combined collapsible bar that surfaces server status + connection
 *  config in one place. Default-collapsed because the user has already
 *  configured this once — the character roster is what they're here
 *  to manage. The panels expand independently, and the title bar shows
 *  a quick health summary so a glance is enough most of the time. */
function SystemPanels() {
  const [statusOpen, setStatusOpen] = useState(false);
  const [serverOpen, setServerOpen] = useState(false);
  // Cached status — instant on revisit (5s TTL). The poll below
  // forces a fresh read every 5s independent of TTL so the badge
  // dots stay live.
  const url = `${HTTP_BASE}/api/status`;
  const { data: status, error } = useCachedJson<CompanionStatus>(url, 5_000);
  useEffect(() => {
    const id = setInterval(() => {
      void cachedJson(url, { force: true }).catch(() => { /* error surfaced via hook */ });
    }, 5_000);
    return () => clearInterval(id);
  }, [url]);

  // Compact summary for the collapsed title row.
  const dot = (ok: boolean) => (
    <span style={{
      width: 7, height: 7, borderRadius: '50%',
      background: ok ? '#10b981' : '#ef4444',
      display: 'inline-block', marginRight: 6, flexShrink: 0,
    }} />
  );
  const summary = !status ? (
    <span style={{ color: '#666' }}>checking…</span>
  ) : (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 12, fontSize: 12, color: '#888' }}>
      <span style={{ display: 'inline-flex', alignItems: 'center' }}>{dot(status.ok)}server</span>
      <span style={{ display: 'inline-flex', alignItems: 'center' }}>{dot(status.zeroclaw_up)}zeroclaw</span>
      <span style={{ display: 'inline-flex', alignItems: 'center' }}>{dot(status.avatar_enabled)}avatar</span>
      {status.pulse_enabled !== undefined &&
        <span style={{ display: 'inline-flex', alignItems: 'center' }}>{dot(status.pulse_enabled)}pulse</span>}
    </span>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <CollapsibleRow
        title="Status"
        rightSummary={summary}
        open={statusOpen}
        onToggle={() => setStatusOpen((v) => !v)}
      >
        {error && <div style={{ color: '#ef4444', fontSize: 13, marginBottom: 6 }}>error: {error}</div>}
        {status && (
          <table style={{ width: '100%', fontSize: 13 }}>
            <tbody>
              <Row label="App service" ok={status.ok} value={status.ok ? 'running' : 'not running'} />
              <Row label="Main agent" ok={status.zeroclaw_up} value={status.zeroclaw_up ? 'connected' : "can't reach"} />
              <Row label="Avatar" ok={status.avatar_enabled} value={status.avatar_enabled ? 'on' : 'off in config'} />
              {status.pulse_enabled !== undefined && (
                <Row label="Pulse" ok={status.pulse_enabled} value={status.pulse_enabled ? 'on' : 'off in config'} />
              )}
            </tbody>
          </table>
        )}
      </CollapsibleRow>

      <CollapsibleRow
        title="Server address"
        rightSummary={<span style={{ fontSize: 12, color: '#666', fontFamily: 'monospace' }}>{getServerUrl()}</span>}
        open={serverOpen}
        onToggle={() => setServerOpen((v) => !v)}
      >
        <ServerConnectionForm />
      </CollapsibleRow>
    </div>
  );
}

function CollapsibleRow({
  title, rightSummary, open, onToggle, children,
}: {
  title: string;
  rightSummary: React.ReactNode;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div style={{ background: '#16181c', border: '1px solid #1f2227', borderRadius: 10 }}>
      <button
        type="button"
        onClick={onToggle}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', gap: 10,
          padding: '10px 14px', background: 'transparent', border: 'none',
          color: '#cbd5e1', fontSize: 13, cursor: 'pointer', textAlign: 'left',
        }}
        aria-expanded={open}
      >
        <span style={{ fontSize: 11, color: '#666', width: 12, textAlign: 'center' }}>
          {open ? '▾' : '▸'}
        </span>
        <span style={{ fontWeight: 500 }}>{title}</span>
        <span style={{ flex: 1 }} />
        {rightSummary}
      </button>
      {open && (
        <div style={{ padding: '4px 14px 14px 38px', borderTop: '1px solid #1f2227' }}>
          {children}
        </div>
      )}
    </div>
  );
}

/** Editor for the companion-server URL stored in localStorage. Same
 *  control as before — just lifted into its own component so the
 *  collapsible can swap it in without bloating the parent. */
function ServerConnectionForm() {
  const [serverInput, setServerInput] = useState<string>(getStoredServerUrl());
  const [savedHint, setSavedHint] = useState<string | null>(null);

  const handleSave = () => {
    const trimmed = serverInput.trim();
    setStoredServerUrl(trimmed);
    setSavedHint(
      trimmed
        ? `Saved. Reload the page for ${trimmed} to take effect.`
        : 'Cleared. Reload the page to use the default.',
    );
    setTimeout(() => setSavedHint(null), 4000);
  };
  const handleClear = () => {
    setStoredServerUrl('');
    setServerInput('');
    setSavedHint('Cleared. Reload the page to use the default.');
    setTimeout(() => setSavedHint(null), 4000);
  };
  const isUsingDefault = !getStoredServerUrl();

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingTop: 8 }}>
      <p style={{ color: '#888', fontSize: 12, margin: 0, lineHeight: 1.5 }}>
        Where this app looks for its background service. Leave blank for
        the default ({getDefaultServerUrl()}). Only change this if you've
        moved the service to a different computer or port.
      </p>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          type="text"
          value={serverInput}
          onChange={(e) => setServerInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSave()}
          placeholder={`${getDefaultServerUrl()}  (default)`}
          style={{
            flex: '1 1 280px', minWidth: 220,
            background: '#0b0d10', color: '#fff', padding: '8px 12px',
            borderRadius: 6, border: '1px solid #2a2d33',
            fontSize: 13, fontFamily: 'monospace', outline: 'none',
          }}
        />
        <button type="button" onClick={handleSave} style={{
          padding: '8px 14px', background: '#3b82f6', color: '#fff',
          border: 'none', borderRadius: 6, fontSize: 13, cursor: 'pointer',
        }}>Save</button>
        <button
          type="button"
          onClick={handleClear}
          disabled={isUsingDefault}
          style={{
            padding: '8px 14px', background: 'transparent', color: '#888',
            border: '1px solid #2a2d33', borderRadius: 6, fontSize: 13,
            cursor: isUsingDefault ? 'not-allowed' : 'pointer',
            opacity: isUsingDefault ? 0.4 : 1,
          }}
        >Reset</button>
      </div>
      {savedHint && <div style={{ fontSize: 11, color: '#10b981' }}>{savedHint}</div>}
    </div>
  );
}

function Row({ label, ok, value }: { label: string; ok: boolean; value: string }) {
  return (
    <tr>
      <td style={{ padding: '4px 0', color: '#aaa' }}>{label}</td>
      <td style={{ padding: '4px 0', textAlign: 'right' }}>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 8,
          color: ok ? '#10b981' : '#ef4444',
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%',
            background: ok ? '#10b981' : '#ef4444',
          }} />
          {value}
        </span>
      </td>
    </tr>
  );
}
