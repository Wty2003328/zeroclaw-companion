import { useEffect, useState } from 'react';
import {
  HTTP_BASE,
  getDefaultServerUrl,
  getServerUrl,
  getStoredServerUrl,
  setStoredServerUrl,
} from '../lib/apiBase';
import {
  fetchInstalledModels,
  getUserModelChoice,
  setUserModelChoice,
  type InstalledModel,
} from '../lib/models';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tauriInvoke(): ((cmd: string, args?: Record<string, unknown>) => Promise<any>) | null {
  if (typeof window === 'undefined') return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const w = window as any;
  const inv = w.__TAURI_INTERNALS__?.invoke ?? w.__TAURI__?.invoke ?? null;
  return typeof inv === 'function' ? inv : null;
}

interface ServerConfig {
  avatar: {
    enabled: boolean;
    chat_language: string;
    tts: {
      engine: string;
      language: string;
      voice: string | null;
      api_url: string | null;
      speed: number;
    };
    subagent: {
      enabled: boolean;
      only_when_translating: boolean;
      use_zeroclaw_webhook: boolean;
      llm_model: string;
      llm_base_url: string;
      llm_api_key_set: boolean;
      timeout_secs: number;
    };
    model: {
      model_dir: string | null;
      default_expression: string;
      scale: number;
      anchor: string;
    };
  } | null;
}

const TOML_HINT_KEY = 'companion.tomlHint.dismissed.v1';

export default function Settings() {
  const [cfg, setCfg] = useState<ServerConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Companion URL section state
  const [serverInput, setServerInput] = useState<string>(getStoredServerUrl());
  const [savedHint, setSavedHint] = useState<string | null>(null);

  const [tomlHintDismissed, setTomlHintDismissed] = useState<boolean>(
    () => localStorage.getItem(TOML_HINT_KEY) === '1'
  );

  useEffect(() => {
    let cancelled = false;
    fetch(`${HTTP_BASE}/api/config`)
      .then((r) => {
        if (!r.ok) throw new Error(`config ${r.status}`);
        return r.json();
      })
      .then((data: ServerConfig) => {
        if (!cancelled) setCfg(data);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSaveUrl = () => {
    const trimmed = serverInput.trim();
    setStoredServerUrl(trimmed);
    setSavedHint(
      trimmed
        ? `Saved. Reload to use ${trimmed}.`
        : 'Cleared. Reload to use the default.'
    );
    setTimeout(() => setSavedHint(null), 4000);
  };

  const handleClearUrl = () => {
    setStoredServerUrl('');
    setServerInput('');
    setSavedHint('Cleared. Reload to use the default.');
    setTimeout(() => setSavedHint(null), 4000);
  };

  const dismissTomlHint = () => {
    setTomlHintDismissed(true);
    localStorage.setItem(TOML_HINT_KEY, '1');
  };

  const isUsingDefaultUrl = !getStoredServerUrl();

  return (
    <div style={{ padding: 32, maxWidth: 880, margin: '0 auto', overflow: 'auto', height: '100%' }}>
      <h1 style={{ marginTop: 0, fontSize: 24 }}>Settings</h1>
      <p style={{ color: '#888', fontSize: 13, marginTop: -4 }}>
        UI-only settings persist in this browser/window. Server-side knobs
        (subagent backend, TTS engine, etc.) live in <code style={{ color: '#aaa' }}>companion.toml</code>;
        this page shows what's loaded.
      </p>

      {error && <ErrorBox message={error} />}

      {/* ── Companion server connection ─────────────────────────── */}
      <Section title="Server connection">
        <div style={{ color: '#888', fontSize: 12, marginBottom: 8, lineHeight: 1.5 }}>
          Where this UI talks to companion-server. Default is{' '}
          <code style={{ color: '#aaa' }}>{getDefaultServerUrl()}</code>. Useful for
          remote companion-server, custom port, or multiple instances.
        </div>
        <Row>
          <input
            type="text"
            value={serverInput}
            onChange={(e) => setServerInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSaveUrl()}
            placeholder={`${getDefaultServerUrl()}  (default)`}
            style={inputStyle}
          />
          <Button onClick={handleSaveUrl} primary>Save</Button>
          <Button onClick={handleClearUrl} disabled={isUsingDefaultUrl}>Reset</Button>
        </Row>
        <Hint tone={savedHint ? 'good' : 'muted'}>
          {savedHint ?? `Currently using: ${getServerUrl()}${isUsingDefaultUrl ? ' (default)' : ' (custom)'}`}
        </Hint>
      </Section>

      {/* ── Avatar / TTS ───────────────────────────────────────── */}
      <Section title="Avatar">
        {!cfg && !error && <Hint tone="muted">loading…</Hint>}
        {cfg && !cfg.avatar && (
          <Hint tone="warn">Avatar disabled in companion.toml. Set <code>[avatar] enabled = true</code>.</Hint>
        )}
        {cfg?.avatar && (
          <>
            <ReadonlyRow label="enabled" value={String(cfg.avatar.enabled)} />
            <ReadonlyRow label="chat language" value={cfg.avatar.chat_language} />
            <ReadonlyRow label="TTS language" value={cfg.avatar.tts.language} />
            <ReadonlyRow label="TTS engine" value={cfg.avatar.tts.engine} />
            <ReadonlyRow label="TTS voice" value={cfg.avatar.tts.voice ?? '—'} />
            <ReadonlyRow label="TTS speed" value={cfg.avatar.tts.speed.toFixed(2)} />
            <ReadonlyRow label="model dir" value={cfg.avatar.model.model_dir ?? '—'} />
            <ReadonlyRow label="default expression" value={cfg.avatar.model.default_expression} />
          </>
        )}
      </Section>

      {/* ── Live2D model picker ────────────────────────────────── */}
      <Section title="Live2D model">
        <ModelPicker />
      </Section>

      {/* ── Subagent (translation + expression LLM) ────────────── */}
      <Section title="Avatar subagent">
        {cfg?.avatar?.subagent && (
          <SubagentEditor
            current={cfg.avatar.subagent}
            tomlHintDismissed={tomlHintDismissed}
            onDismissHint={dismissTomlHint}
          />
        )}
      </Section>
    </div>
  );
}

// ── Editable subagent backend section ─────────────────────────────
//
// Routes "save" through `POST /api/config/subagent`. The companion-server
// writes the choice to `companion.runtime.json` (sibling of the loaded
// companion.toml). It takes effect on next process restart — the live
// subagent is built once at startup, not hot-swappable. After save we
// surface a "Restart Tauri" button (which triggers `app.restart()` via
// a Tauri command) so the user doesn't have to manually close the
// window.
type Backend = 'direct' | 'webhook';

function SubagentEditor({
  current,
  tomlHintDismissed,
  onDismissHint,
}: {
  current: NonNullable<ServerConfig['avatar']>['subagent'];
  tomlHintDismissed: boolean;
  onDismissHint: () => void;
}) {
  const [backend, setBackend] = useState<Backend>(
    current.use_zeroclaw_webhook ? 'webhook' : 'direct'
  );
  const [apiKey, setApiKey] = useState<string>('');
  const [model, setModel] = useState<string>(current.llm_model || '');
  const [baseUrl, setBaseUrl] = useState<string>(current.llm_base_url || '');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dirty =
    backend !== (current.use_zeroclaw_webhook ? 'webhook' : 'direct') ||
    apiKey.length > 0 ||
    model.trim() !== (current.llm_model || '') ||
    baseUrl.trim() !== (current.llm_base_url || '');

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    const body: Record<string, unknown> = {
      use_zeroclaw_webhook: backend === 'webhook',
    };
    // Only send fields that the user actually changed so we don't trample
    // unrelated overrides server-side.
    if (apiKey.length > 0) body.api_key = apiKey;
    if (model.trim() !== (current.llm_model || '')) body.model = model.trim();
    if (baseUrl.trim() !== (current.llm_base_url || '')) body.base_url = baseUrl.trim();
    try {
      const r = await fetch(`${HTTP_BASE}/api/config/subagent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`save failed: ${r.status} ${await r.text()}`);
      setSavedAt(Date.now());
      // Clear the api-key input once it's saved — server stores it,
      // /api/config redacts it on the next read; the form should not
      // claim the key is still pending.
      setApiKey('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleRestart = async () => {
    const inv = tauriInvoke();
    if (!inv) {
      // Browser path — we can only suggest manual reload of the server.
      window.alert('Restart the companion-server process to apply.');
      return;
    }
    try {
      await inv('restart_app');
    } catch (e) {
      setError(`restart failed: ${(e as Error).message}`);
    }
  };

  return (
    <>
      <ReadonlyRow label="enabled" value={String(current.enabled)} />
      <ReadonlyRow
        label="only when translating"
        value={
          current.only_when_translating
            ? 'yes (skip same-language)'
            : 'no (always run)'
        }
      />

      {/* Backend selector */}
      <div
        style={{
          display: 'flex',
          gap: 12,
          padding: '10px 0',
          borderBottom: '1px solid #1f2227',
          fontSize: 13,
          alignItems: 'center',
          flexWrap: 'wrap',
        }}
      >
        <span style={{ minWidth: 160, color: '#888' }}>backend</span>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center', cursor: 'pointer' }}>
          <input
            type="radio"
            name="backend"
            checked={backend === 'direct'}
            onChange={() => setBackend('direct')}
          />
          <span style={{ color: backend === 'direct' ? '#10b981' : '#cbd5e1' }}>
            direct LLM <span style={{ color: '#666' }}>(fast ~1–3s)</span>
          </span>
        </label>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center', cursor: 'pointer' }}>
          <input
            type="radio"
            name="backend"
            checked={backend === 'webhook'}
            onChange={() => setBackend('webhook')}
          />
          <span style={{ color: backend === 'webhook' ? '#f59e0b' : '#cbd5e1' }}>
            zeroclaw webhook{' '}
            <span style={{ color: '#666' }}>(slow ~5–10s, reuses zeroclaw key)</span>
          </span>
        </label>
      </div>

      {/* Direct-LLM connection fields (only meaningful when backend=direct) */}
      {backend === 'direct' && (
        <div
          style={{
            padding: '10px 0 4px',
            borderBottom: '1px solid #1f2227',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
          <FieldRow label="base URL">
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.z.ai/api/coding/paas/v4"
              style={inputStyle}
            />
          </FieldRow>
          <FieldRow label="model">
            <input
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="glm-4.5-flash"
              style={inputStyle}
            />
          </FieldRow>
          <FieldRow label="API key">
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={
                current.llm_api_key_set
                  ? '••• already set (paste to replace)'
                  : 'paste your z.ai / OpenAI / etc. key'
              }
              style={inputStyle}
              autoComplete="off"
            />
          </FieldRow>
          <div style={{ fontSize: 11, color: '#666', marginLeft: 168 }}>
            Stored in <code style={{ color: '#888' }}>companion.runtime.json</code>{' '}
            next to <code style={{ color: '#888' }}>companion.toml</code>. Don't commit
            that file to git.
          </div>
        </div>
      )}

      {/* Save / restart buttons */}
      <Row>
        <div style={{ flex: 1, minWidth: 0 }}>
          {error && <Hint tone="warn">{error}</Hint>}
          {savedAt && !error && (
            <Hint tone="good">
              Saved. Click <strong>Restart</strong> to apply.
            </Hint>
          )}
          {!savedAt && !error && dirty && (
            <Hint tone="muted">unsaved changes</Hint>
          )}
        </div>
        <Button onClick={handleSave} primary disabled={!dirty || saving}>
          {saving ? 'saving…' : 'Save'}
        </Button>
        <Button onClick={handleRestart}>
          Restart
        </Button>
      </Row>

      <ReadonlyRow label="timeout" value={`${current.timeout_secs}s`} />

      {backend === 'webhook' && !tomlHintDismissed && (
        <SubagentSpeedupHint onDismiss={onDismissHint} />
      )}
    </>
  );
}

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
      <span style={{ minWidth: 160, color: '#888', fontSize: 12 }}>{label}</span>
      <div style={{ flex: '1 1 280px', minWidth: 220 }}>{children}</div>
    </div>
  );
}

// ── Model picker ─────────────────────────────────────────────────
//
// Lists models installed under web/public/live2d/models/ via the
// /api/models endpoint and lets the user pick one. Selection is
// persisted to localStorage; Avatar.tsx listens for the
// `companion:userModel` custom event and live-updates the canvas.
function ModelPicker() {
  const [models, setModels] = useState<InstalledModel[]>([]);
  const [picked, setPicked] = useState<string | null>(() => getUserModelChoice());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchInstalledModels()
      .then((m) => {
        if (cancelled) return;
        if (m.length === 0) {
          setError('No models found under web/public/live2d/models/');
        }
        setModels(m);
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  const choose = (id: string | null) => {
    setPicked(id);
    setUserModelChoice(id);
    // Notify same-window listeners (storage event only fires
    // cross-tab, not within the same window where the write happened).
    window.dispatchEvent(new Event('companion:userModel'));
  };

  if (error) return <Hint tone="warn">{error}</Hint>;
  if (models.length === 0) return <Hint tone="muted">loading installed models…</Hint>;

  return (
    <>
      <div style={{ color: '#888', fontSize: 12, marginBottom: 8, lineHeight: 1.5 }}>
        Pick which Live2D model the avatar canvas renders. Drop new
        models into <code style={{ color: '#aaa' }}>web/public/live2d/models/&lt;name&gt;/</code>
        — they'll appear here after a refresh.
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <ModelChoice
          label="Server default"
          format=""
          checked={picked === null}
          onChange={() => choose(null)}
        />
        {models.map((m) => (
          <ModelChoice
            key={m.id}
            label={m.name}
            format={m.format}
            checked={picked === m.id}
            onChange={() => choose(m.id)}
          />
        ))}
      </div>
      <Hint tone="muted">
        {picked
          ? `Active: ${picked} (override). Clears on "Server default".`
          : `Active: server-default (companion.toml \`[avatar.model]\`).`}
      </Hint>
    </>
  );
}

function ModelChoice({
  label,
  format,
  checked,
  onChange,
}: {
  label: string;
  format: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
      <input type="radio" name="live2d-model" checked={checked} onChange={onChange} />
      <span style={{ color: checked ? '#10b981' : '#cbd5e1' }}>{label}</span>
      {format && (
        <span style={{ color: '#666', fontSize: 11 }}>({format})</span>
      )}
    </label>
  );
}

function SubagentSpeedupHint({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div
      style={{
        marginTop: 12,
        padding: 14,
        background: '#1e2433',
        border: '1px solid #2d3a55',
        borderRadius: 8,
        fontSize: 12,
        color: '#cbd5e1',
        lineHeight: 1.55,
        position: 'relative',
      }}
    >
      <button
        type="button"
        onClick={onDismiss}
        title="Dismiss"
        style={{
          position: 'absolute',
          top: 8,
          right: 8,
          background: 'transparent',
          border: 'none',
          color: '#888',
          cursor: 'pointer',
          fontSize: 14,
        }}
      >
        ✕
      </button>
      <div style={{ fontWeight: 600, color: '#fff', marginBottom: 6 }}>
        💡 Subagent speed-up
      </div>
      You're routing every subagent call through zeroclaw's full agent
      loop (memory, tools, system prompts) — that adds 5–10s per turn.
      The subagent only needs a tiny LLM to emit JSON, so a direct call
      to a small model is much faster.
      <pre
        style={{
          marginTop: 10,
          padding: 10,
          background: '#0b0d10',
          border: '1px solid #2a2d33',
          borderRadius: 6,
          fontSize: 11,
          color: '#a5b4fc',
          overflow: 'auto',
        }}
      >
{`# in companion.toml
[avatar.subagent]
enabled              = true
use_zeroclaw_webhook = false       # ← key change
only_when_translating = true       # skip when chat_lang == tts_lang

[avatar.subagent.llm]
base_url     = "https://api.openai.com/v1"
model        = "gpt-4o-mini"       # ~1s, cheap
api_key_env  = "OPENAI_API_KEY"    # set the env var
timeout_secs = 10`}
      </pre>
      <div style={{ marginTop: 6, color: '#94a3b8' }}>
        Other fast options: Groq Llama-3.3-70B (~0.5s),
        Ollama on localhost:11434/v1 (free, local), DeepSeek, Z.ai GLM-4-Flash.
        Restart companion-server after editing companion.toml.
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section
      style={{
        background: '#16181c',
        borderRadius: 10,
        padding: 20,
        marginTop: 16,
      }}
    >
      <h2 style={{ margin: '0 0 12px 0', fontSize: 14, fontWeight: 600 }}>{title}</h2>
      {children}
    </section>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>{children}</div>
  );
}

function ReadonlyRow({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'good' | 'warn' | 'muted';
}) {
  const color = tone === 'good' ? '#10b981' : tone === 'warn' ? '#f59e0b' : '#cbd5e1';
  return (
    <div
      style={{
        display: 'flex',
        gap: 12,
        padding: '6px 0',
        borderBottom: '1px solid #1f2227',
        fontSize: 13,
      }}
    >
      <span style={{ minWidth: 160, color: '#888' }}>{label}</span>
      <span style={{ color, fontFamily: 'ui-monospace, monospace', fontSize: 12, wordBreak: 'break-all' }}>
        {value}
      </span>
    </div>
  );
}

function Hint({ tone, children }: { tone: 'muted' | 'good' | 'warn'; children: React.ReactNode }) {
  const color = tone === 'good' ? '#10b981' : tone === 'warn' ? '#f59e0b' : '#666';
  return <div style={{ marginTop: 8, fontSize: 11, color }}>{children}</div>;
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div
      style={{
        background: '#1f1316',
        color: '#fca5a5',
        padding: 12,
        borderRadius: 8,
        marginTop: 16,
        fontSize: 13,
      }}
    >
      Failed to load config: {message}
    </div>
  );
}

function Button({
  children,
  onClick,
  primary,
  disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  primary?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: '8px 14px',
        background: primary && !disabled ? '#3b82f6' : 'transparent',
        color: primary && !disabled ? '#fff' : '#888',
        border: primary && !disabled ? 'none' : '1px solid #2a2d33',
        borderRadius: 6,
        fontSize: 13,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.4 : 1,
      }}
    >
      {children}
    </button>
  );
}

const inputStyle: React.CSSProperties = {
  flex: '1 1 280px',
  minWidth: 220,
  background: '#0b0d10',
  color: '#fff',
  padding: '8px 12px',
  borderRadius: 6,
  border: '1px solid #2a2d33',
  fontSize: 13,
  fontFamily: 'monospace',
  outline: 'none',
};
