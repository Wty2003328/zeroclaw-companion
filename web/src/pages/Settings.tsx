import { useState, useEffect } from 'react';
import {
  HTTP_BASE,
  getDefaultServerUrl,
  getServerUrl,
  getStoredServerUrl,
  setStoredServerUrl,
} from '../lib/apiBase';
import { invalidateCache, useCachedJson } from '../lib/fetchCache';
import { pickFile, pickFolder, listGpus, type DetectedGpu } from '../lib/tauriShell';
import { tokens, inputStyle, monoInputStyle } from '../lib/theme';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tauriInvoke(): ((cmd: string, args?: Record<string, unknown>) => Promise<any>) | null {
  if (typeof window === 'undefined') return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const w = window as any;
  const inv = w.__TAURI_INTERNALS__?.invoke ?? w.__TAURI__?.invoke ?? null;
  return typeof inv === 'function' ? inv : null;
}

interface AvatarConfigView {
  enabled: boolean;
  chat_language: string;
  tts: {
    engine: string;
    language: string;
    voice: string | null;
    api_url: string | null;
    speed: number;
    launch_command: string | null;
    reference_audio: string | null;
    reference_text: string | null;
    reference_language: string | null;
    model_path: string | null;
    gpu_device: number;
  };
  subagent: {
    enabled: boolean;
    only_when_translating: boolean;
    use_zeroclaw_webhook: boolean;
    streaming: boolean;
    llm_model: string;
    llm_base_url: string;
    llm_disable_thinking: boolean;
    llm_api_key_set: boolean;
    timeout_secs: number;
  };
  model: {
    model_dir: string | null;
    default_expression: string;
    scale: number;
    anchor: string;
  };
}

interface ZeroclawConfigView {
  /// "zeroclaw" | "openclaw" | "hermes" | "custom". Drives the chat
  /// HTTP shape (webhook vs OpenAI-compat) and prefilled default port.
  kind: string;
  url: string;
  timeout_secs: number;
  pair_token_set: boolean;
  reachable: boolean;
}

/// Known agent kinds and the metadata the UI needs to present them.
/// Keep this in sync with `AgentKind` in companion-core/src/config.rs.
const AGENT_KINDS: Array<{
  id: 'zeroclaw' | 'openclaw' | 'hermes' | 'custom';
  label: string;
  port: number;
  blurb: string;
}> = [
  {
    id: 'zeroclaw',
    label: 'zeroclaw (Rust, /webhook)',
    port: 42617,
    blurb: 'Talks to a zeroclaw gateway. POSTs {message} to /webhook.',
  },
  {
    id: 'openclaw',
    label: 'openclaw (Node, /v1/chat/completions)',
    port: 18790,
    blurb:
      'Talks to an openclaw gateway via its OpenAI-compatible /v1/chat/completions endpoint. ' +
      'A pairing token is required when openclaw is bound to LAN.',
  },
  {
    id: 'hermes',
    label: 'hermes-agent (via bridge, /webhook)',
    port: 18791,
    blurb:
      'Talks to the hermes-bridge.py shim (POST /webhook). The shim shells out to ' +
      '`hermes -z "<message>"` since hermes-agent has no built-in synchronous HTTP chat. ' +
      'See README → "Running hermes" for the bridge.',
  },
  {
    id: 'custom',
    label: 'custom (/webhook)',
    port: 42617,
    blurb:
      'Anything else that speaks the zeroclaw /webhook shape (`{"message"}` → `{"response"}`). ' +
      'Point this at any compatible URL.',
  },
];

interface ServerConfig {
  avatar: AvatarConfigView | null;
  zeroclaw?: ZeroclawConfigView;
}

const TOML_HINT_KEY = 'companion.tomlHint.dismissed.v1';

export default function Settings() {
  // Cached read of server config — instant on Settings revisit, the
  // hook auto-revalidates after `invalidateCache` calls fired by
  // editor save handlers.
  const cfgUrl = `${HTTP_BASE}/api/config`;
  const { data: cfg, error: fetchError } = useCachedJson<ServerConfig>(cfgUrl, 60_000);
  const reloadCfg = () => { invalidateCache(cfgUrl); };

  // Companion URL section state
  const [serverInput, setServerInput] = useState<string>(getStoredServerUrl());
  const [savedHint, setSavedHint] = useState<string | null>(null);

  const [tomlHintDismissed, setTomlHintDismissed] = useState<boolean>(
    () => localStorage.getItem(TOML_HINT_KEY) === '1',
  );
  const error = fetchError;

  const handleSaveUrl = () => {
    const trimmed = serverInput.trim();
    setStoredServerUrl(trimmed);
    setSavedHint(trimmed
      ? `Saved. Reload to use ${trimmed}.`
      : 'Cleared. Reload to use the default.');
    setTimeout(() => setSavedHint(null), 4000);
  };

  const handleClearUrl = () => {
    setStoredServerUrl('');
    setServerInput('');
    setSavedHint('Cleared. Reload to use the default.');
    setTimeout(() => setSavedHint(null), 4000);
  };

  const isUsingDefaultUrl = !getStoredServerUrl();

  return (
    <div
      style={{
        flex: '1 1 0', minHeight: 0, overflow: 'auto',
        contain: 'paint',
        overscrollBehavior: 'contain',
      }}
    >
      <div style={{ padding: '40px 32px', maxWidth: 880, margin: '0 auto' }}>
      <header style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 28, fontWeight: 700, letterSpacing: '-0.01em', color: tokens.text }}>
          Settings
        </h1>
        <p style={{ color: tokens.textMuted, fontSize: 13, margin: '6px 0 0 0', lineHeight: 1.55 }}>
          Changes apply immediately. Voice-engine swaps and a few other
          process-level options take effect on the next app start —
          they'll say so explicitly.
        </p>
      </header>

      {error && <ErrorBox message={error} />}

      <Section title="Main agent">
        {!cfg && !error && <Hint tone="muted">loading…</Hint>}
        {cfg?.zeroclaw && (
          <ZeroclawEditor current={cfg.zeroclaw} onSaved={reloadCfg} />
        )}
      </Section>

      <Section title="Avatar & voice">
        {!cfg && !error && <Hint tone="muted">loading…</Hint>}
        {cfg && !cfg.avatar && (
          <Hint tone="warn">
            Avatar is turned off in the config file. Set{' '}
            <code>[avatar] enabled = true</code> in companion.toml to use it.
          </Hint>
        )}
        {cfg?.avatar && (
          <AvatarEditor current={cfg.avatar} onSaved={reloadCfg} />
        )}
      </Section>

      <Section title="Translation & expressions">
        {cfg?.avatar?.subagent && (
          <SubagentEditor
            current={cfg.avatar.subagent}
            tomlHintDismissed={tomlHintDismissed}
            onDismissHint={() => {
              setTomlHintDismissed(true);
              localStorage.setItem(TOML_HINT_KEY, '1');
            }}
          />
        )}
      </Section>

      {/* Companion service URL — most users never touch this. It's the
          address the React UI uses to reach its own background service
          (the companion-server sidecar). Distinct from the agent URL
          above; the description spells that out so it doesn't get
          confused with it. */}
      <Section
        title="Companion service"
        description="Where this UI reaches its local background service (the companion-server sidecar). Leave blank for the default — this is not the agent address; set that in Main agent above."
      >
        <FieldRow
          label="Service URL"
          hint={`Now using: ${getServerUrl()}${isUsingDefaultUrl ? ' (default)' : ''}`}
        >
          <input
            type="text"
            value={serverInput}
            onChange={(e) => setServerInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSaveUrl()}
            placeholder={`${getDefaultServerUrl()}  (default)`}
            style={monoInputStyle}
          />
          <Button onClick={handleSaveUrl} primary>Save</Button>
          <Button onClick={handleClearUrl} disabled={isUsingDefaultUrl}>Reset</Button>
        </FieldRow>
        {savedHint && (
          <div style={{ marginTop: 8 }}>
            <Hint tone="good">{savedHint}</Hint>
          </div>
        )}
      </Section>
      </div>
    </div>
  );
}

// ── Avatar editor ────────────────────────────────────────────────
//
// Knobs that flip frequently and don't need a TTS engine restart get
// editable controls here. The TTS engine, voice, and reference audio
// stay read-only because changing them implies a different launch
// pipeline (different engine binary, different model weights).

const LANGUAGE_CHOICES: { code: string; label: string }[] = [
  { code: 'en', label: 'English (en)' },
  { code: 'ja', label: 'Japanese (ja)' },
  { code: 'zh', label: 'Chinese (zh)' },
  { code: 'ko', label: 'Korean (ko)' },
  { code: 'es', label: 'Spanish (es)' },
  { code: 'fr', label: 'French (fr)' },
  { code: 'de', label: 'German (de)' },
];

/** Per-engine spec: which fields the form should expose, plus a
 *  one-liner describing what the engine is. Custom engine names get
 *  the "show everything" fallback below — power users typically know
 *  what they need.
 *
 *  Removed: legacy `gpt-sovits` (v1-v3). It still works if a user has
 *  it set in companion.toml — they'll just see the Custom path and
 *  can edit by hand. v4 is the supported zero-shot rig. */
interface EngineSpec {
  value: string;
  label: string;
  description: string;
  needsLauncher: boolean;
  needsModelRoot: boolean;
  modelRootLabel?: string;
  modelRootHint?: string;
  needsGpu: boolean;
  needsVoiceSample: boolean;
  needsPresetVoice: boolean;
  presetVoices?: { value: string; label: string }[];
}

const ENGINE_SPECS: EngineSpec[] = [
  {
    value: 'gpt-sovits-v4',
    label: 'GPT-SoVITS v4',
    description: 'High-quality zero-shot voice cloning. Needs GPU + a 3-10s voice sample.',
    needsLauncher: true,
    needsModelRoot: true,
    modelRootLabel: 'GPT-SoVITS install folder',
    modelRootHint: 'Path to your GPT-SoVITS git checkout (the folder with `tools/`, `GPT_SoVITS/`, etc.)',
    needsGpu: true,
    needsVoiceSample: true,
    needsPresetVoice: false,
  },
  {
    value: 'fish-speech',
    label: 'fish-speech',
    description: 'Zero-shot voice cloning. Needs GPU + a voice sample.',
    needsLauncher: true,
    needsModelRoot: true,
    modelRootLabel: 'fish-speech model folder',
    modelRootHint: 'Path to the fish-speech checkpoint directory',
    needsGpu: true,
    needsVoiceSample: true,
    needsPresetVoice: false,
  },
  {
    value: 'xtts',
    label: 'XTTS (Coqui)',
    description: 'Zero-shot multilingual cloning. Needs GPU + a voice sample.',
    needsLauncher: true,
    needsModelRoot: true,
    modelRootLabel: 'XTTS model folder',
    modelRootHint: 'Path to the Coqui XTTS model directory',
    needsGpu: true,
    needsVoiceSample: true,
    needsPresetVoice: false,
  },
  {
    value: 'f5-tts',
    label: 'F5-TTS',
    description: 'Fast zero-shot synthesis. Needs GPU + a voice sample.',
    needsLauncher: true,
    needsModelRoot: true,
    modelRootLabel: 'F5-TTS install folder',
    modelRootHint: 'Path to the F5-TTS checkout',
    needsGpu: true,
    needsVoiceSample: true,
    needsPresetVoice: false,
  },
  {
    value: 'edge-tts',
    label: 'edge-tts (Microsoft, free, no GPU)',
    description: 'Cloud-based preset voices from Microsoft Edge. Free, fast, no GPU. Pick from a fixed voice list.',
    needsLauncher: true,
    needsModelRoot: false,
    needsGpu: false,
    needsVoiceSample: false,
    needsPresetVoice: true,
    presetVoices: [
      { value: 'ja-JP-NanamiNeural', label: 'ja-JP / Nanami (female)' },
      { value: 'ja-JP-KeitaNeural',  label: 'ja-JP / Keita (male)' },
      { value: 'en-US-AriaNeural',   label: 'en-US / Aria (female)' },
      { value: 'en-US-GuyNeural',    label: 'en-US / Guy (male)' },
      { value: 'en-US-JennyNeural',  label: 'en-US / Jenny (female)' },
      { value: 'zh-CN-XiaoxiaoNeural', label: 'zh-CN / Xiaoxiao (female)' },
      { value: 'zh-CN-YunxiNeural',  label: 'zh-CN / Yunxi (male)' },
      { value: 'ko-KR-SunHiNeural',  label: 'ko-KR / SunHi (female)' },
    ],
  },
  {
    value: 'melotts',
    label: 'MeloTTS',
    description: 'Lightweight multilingual TTS with preset voices. Runs on CPU or GPU.',
    needsLauncher: true,
    needsModelRoot: false,
    needsGpu: true,
    needsVoiceSample: false,
    needsPresetVoice: true,
    presetVoices: [
      { value: 'JP',     label: 'Japanese (default)' },
      { value: 'EN-US',  label: 'English US' },
      { value: 'EN-BR',  label: 'English UK' },
      { value: 'ZH',     label: 'Chinese' },
      { value: 'KR',     label: 'Korean' },
      { value: 'FR',     label: 'French' },
      { value: 'ES',     label: 'Spanish' },
    ],
  },
];

/** "Show everything" spec for custom/unknown engines. */
const CUSTOM_ENGINE_SPEC: EngineSpec = {
  value: '__custom',
  label: 'Custom engine',
  description: "You're bringing your own. We expose every field — pick what your wrapper needs.",
  needsLauncher: true,
  needsModelRoot: true,
  modelRootLabel: 'Engine root folder',
  modelRootHint: 'Whatever your wrapper expects as TTS_MODEL_PATH',
  needsGpu: true,
  needsVoiceSample: true,
  needsPresetVoice: false,
};

function engineSpec(engine: string): EngineSpec {
  return ENGINE_SPECS.find((e) => e.value === engine) ?? CUSTOM_ENGINE_SPEC;
}

/** Split a launch_command into (python interpreter, server script).
 *  The combined form looks like `C:/path/python.exe tools/x.py`.
 *  Heuristic:
 *    1. Match on `.exe`/`python`/`python3` followed by whitespace —
 *       this handles paths-with-no-spaces cleanly.
 *    2. Otherwise split on the first whitespace.
 *    3. If neither, treat the whole thing as the interpreter.
 *  Doesn't handle Windows paths with embedded spaces — for those the
 *  user can paste the combined string into either field; we re-join
 *  on save with a single space. */
function splitLaunch(combined: string): { python: string; script: string } {
  const trimmed = combined.trim();
  if (!trimmed) return { python: '', script: '' };
  const m = trimmed.match(/^(.*?(?:\.exe|python\d?))\s+(.+)$/i);
  if (m) return { python: m[1].trim(), script: m[2].trim() };
  const ws = trimmed.indexOf(' ');
  if (ws < 0) return { python: trimmed, script: '' };
  return { python: trimmed.slice(0, ws), script: trimmed.slice(ws + 1).trim() };
}

function joinLaunch(python: string, script: string): string {
  const p = python.trim();
  const s = script.trim();
  if (p && s) return `${p} ${s}`;
  return p || s;
}

/** Edits the connection to the (possibly remote) zeroclaw daemon.
 *  Lets the user point the companion at a zeroclaw running on a home
 *  server, a Raspberry Pi, or another laptop on the LAN — no
 *  companion.toml editing. The companion never gives zeroclaw access
 *  to the machine it runs on; it just POSTs chat to zeroclaw's
 *  `/webhook` and renders the reply. Changes need a companion-server
 *  restart (the client is built once at startup). */
function ZeroclawEditor({
  current, onSaved,
}: {
  current: ZeroclawConfigView;
  onSaved: () => void;
}) {
  const initialKind = (current.kind || 'zeroclaw') as typeof AGENT_KINDS[number]['id'];
  const [kind, setKind] = useState<typeof AGENT_KINDS[number]['id']>(initialKind);
  const [url, setUrl] = useState<string>(current.url);
  const [token, setToken] = useState<string>(''); // never pre-filled; redacted server-side
  const [timeout, setTimeout_] = useState<number>(current.timeout_secs);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle');

  const spec = AGENT_KINDS.find((k) => k.id === kind) ?? AGENT_KINDS[0];

  /// Prefill the URL when the user picks a new kind — but only if the
  /// current URL still matches the OLD kind's default port. If they
  /// typed something custom, leave it alone.
  const handleKindChange = (next: typeof AGENT_KINDS[number]['id']) => {
    const prev = AGENT_KINDS.find((k) => k.id === kind) ?? AGENT_KINDS[0];
    const newSpec = AGENT_KINDS.find((k) => k.id === next) ?? AGENT_KINDS[0];
    setKind(next);
    // Replace `:<oldPort>` with `:<newPort>` if the URL looks like the
    // default for the previous kind. Otherwise don't touch the URL.
    const oldUrl = url.trim();
    const wasPrevDefault =
      oldUrl === `http://127.0.0.1:${prev.port}` ||
      oldUrl === `http://localhost:${prev.port}` ||
      oldUrl.endsWith(`:${prev.port}`);
    if (wasPrevDefault) {
      setUrl(oldUrl.replace(`:${prev.port}`, `:${newSpec.port}`));
    }
  };

  const dirty =
    kind !== (current.kind || 'zeroclaw') ||
    url.trim() !== current.url ||
    token.length > 0 ||
    timeout !== current.timeout_secs;

  const save = async () => {
    setSaving(true); setError(null);
    const body: Record<string, unknown> = {};
    if (kind !== (current.kind || 'zeroclaw')) body.kind = kind;
    if (url.trim() !== current.url) body.url = url.trim();
    if (token.length > 0) body.pair_token = token;
    if (timeout !== current.timeout_secs) body.timeout_secs = timeout;
    if (Object.keys(body).length === 0) { setSaving(false); return; }
    try {
      const r = await fetch(`${HTTP_BASE}/api/config/zeroclaw`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`save: ${r.status} ${await r.text()}`);
      setSavedAt(Date.now());
      setToken(''); // clear once persisted; server redacts on read
      onSaved();
      // The server hot-swapped the agent client; tell the health
      // banner to re-poll right now so the red bar clears instead of
      // sitting stale until the next 30s tick.
      window.dispatchEvent(new CustomEvent('companion:agent-changed'));
      // Fade the "Applied" hint after 4s so it doesn't linger.
      setTimeout(() => setSavedAt(null), 4000);
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };

  const testConnection = async () => {
    const inv = tauriInvoke();
    const target = url.trim() || current.url;
    setTestResult('testing');
    if (inv) {
      // Reuse the Tauri health-probe command, but against the URL the
      // user typed (not the running config) so they can verify before
      // saving + restarting.
      try {
        const ok = await inv('check_zeroclaw_health', { url: target });
        setTestResult(ok ? 'ok' : 'fail');
      } catch { setTestResult('fail'); }
    } else {
      // Browser fallback: ask companion-server. This only checks the
      // CURRENTLY configured zeroclaw, not the typed URL — note that
      // to the user.
      try {
        const r = await fetch(`${HTTP_BASE}/api/config`);
        const j = await r.json();
        setTestResult(j?.zeroclaw?.reachable ? 'ok' : 'fail');
      } catch { setTestResult('fail'); }
    }
    setTimeout(() => setTestResult('idle'), 5000);
  };

  return (
    <>
      <p style={{
        margin: '0 0 14px 0', fontSize: 12.5, color: tokens.textMuted, lineHeight: 1.55,
      }}>
        Where the companion finds your main agent. Pick the flavor and
        point it at the host running it (this machine, a home server, a
        Raspberry Pi, another laptop on your LAN). The companion only
        sends chat messages and shows replies; the agent never gets to
        touch the computer the companion runs on.
      </p>
      <FieldRow label="Agent" hint={spec.blurb}>
        <select
          value={kind}
          onChange={(e) => handleKindChange(e.target.value as typeof AGENT_KINDS[number]['id'])}
          style={{ ...inputStyle, maxWidth: 360 }}
        >
          {AGENT_KINDS.map((k) => (
            <option key={k.id} value={k.id}>{k.label}</option>
          ))}
        </select>
      </FieldRow>
      <FieldRow label="Gateway URL">
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={`http://192.168.1.50:${spec.port}  (or http://127.0.0.1:${spec.port} for local)`}
          style={monoInputStyle}
        />
      </FieldRow>
      <FieldRow label="Pairing token">
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={current.pair_token_set ? '••• set (paste to replace)' : 'optional — only if your agent requires one'}
          style={monoInputStyle}
          autoComplete="off"
        />
      </FieldRow>
      <FieldRow
        label="Request timeout (s)"
        hint={
          <>
            Long enough for the agent's full tool-use loop (web searches,
            browser, shell). 300s is a safe default; bump it if you see
            "timed out" on complex requests.
            <br />
            For a LAN agent, make sure its gateway binds to{' '}
            <code>0.0.0.0</code> (not <code>127.0.0.1</code>) so it's
            reachable from this machine.
          </>
        }
      >
        <input
          type="number" min={5} max={1800}
          value={timeout}
          onChange={(e) => setTimeout_(Math.max(5, Math.min(1800, parseInt(e.target.value, 10) || 300)))}
          style={{ ...inputStyle, maxWidth: 110 }}
        />
      </FieldRow>
      <EditorFooter
        status={
          <>
            {error && <Hint tone="warn">{error}</Hint>}
            {!error && dirty && <Hint tone="muted">unsaved changes</Hint>}
            {!error && !dirty && savedAt && <Hint tone="good">✓ Applied — agent switched live.</Hint>}
            {!error && !dirty && !savedAt && (
              <Hint tone={current.reachable ? 'good' : 'warn'}>
                {current.reachable
                  ? '● connected'
                  : `● not reachable — check the URL or start ${spec.id}`}
              </Hint>
            )}
            {testResult === 'testing' && <Hint tone="muted">testing…</Hint>}
            {testResult === 'ok' && <Hint tone="good">✓ reachable</Hint>}
            {testResult === 'fail' && <Hint tone="warn">✗ no response</Hint>}
          </>
        }
      >
        <Button onClick={testConnection} disabled={testResult === 'testing'}>Test connection</Button>
        <Button onClick={save} primary disabled={!dirty || saving}>
          {saving ? 'Applying…' : 'Apply'}
        </Button>
      </EditorFooter>
    </>
  );
}

function AvatarEditor({
  current, onSaved,
}: {
  current: AvatarConfigView;
  onSaved: () => void;
}) {
  const [enabled, setEnabled] = useState<boolean>(current.enabled);
  const [chatLang, setChatLang] = useState<string>(current.chat_language);
  const [ttsLang, setTtsLang] = useState<string>(current.tts.language);
  const [ttsSpeed, setTtsSpeed] = useState<number>(current.tts.speed);
  const [ttsEngine, setTtsEngine] = useState<string>(current.tts.engine);
  // TTS path / reference settings — used to require editing
  // companion.toml. Now editable here so a fresh install can be set
  // up without leaving the app.
  // Split the combined launch_command (`python.exe tools/x.py`) into
  // two fields so the user gets a clean "interpreter + script" UI
  // instead of one merged string that couldn't be properly browsed.
  const initialLaunch = splitLaunch(current.tts.launch_command ?? '');
  const [ttsPython, setTtsPython] = useState<string>(initialLaunch.python);
  const [ttsScript, setTtsScript] = useState<string>(initialLaunch.script);
  const ttsLaunchCmd = joinLaunch(ttsPython, ttsScript);
  const [ttsRefAudio, setTtsRefAudio] = useState<string>(current.tts.reference_audio ?? '');
  const [ttsRefText, setTtsRefText] = useState<string>(current.tts.reference_text ?? '');
  const [ttsRefLang, setTtsRefLang] = useState<string>(current.tts.reference_language ?? '');
  const [ttsModelPath, setTtsModelPath] = useState<string>(current.tts.model_path ?? '');
  const [ttsGpu, setTtsGpu] = useState<number>(current.tts.gpu_device);
  const [ttsVoice, setTtsVoice] = useState<string>(current.tts.voice ?? '');
  // Detected GPUs from the host (nvidia-smi → WMI fallback).
  // Empty until the Tauri command resolves; we render a sane fallback
  // (CPU + "GPU 0") until then.
  const [detectedGpus, setDetectedGpus] = useState<DetectedGpu[]>([]);
  useEffect(() => { void listGpus().then(setDetectedGpus); }, []);
  const spec = engineSpec(ttsEngine);
  const isCustomEngine = !ENGINE_SPECS.find((e) => e.value === ttsEngine);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dirty =
    enabled !== current.enabled ||
    chatLang !== current.chat_language ||
    ttsLang !== current.tts.language ||
    Math.abs(ttsSpeed - current.tts.speed) > 0.001 ||
    ttsEngine.trim() !== current.tts.engine ||
    ttsLaunchCmd.trim() !== (current.tts.launch_command ?? '') ||
    ttsRefAudio.trim() !== (current.tts.reference_audio ?? '') ||
    ttsRefText.trim() !== (current.tts.reference_text ?? '') ||
    ttsRefLang.trim() !== (current.tts.reference_language ?? '') ||
    ttsModelPath.trim() !== (current.tts.model_path ?? '') ||
    ttsGpu !== current.tts.gpu_device ||
    ttsVoice.trim() !== (current.tts.voice ?? '');

  const save = async () => {
    setSaving(true); setError(null);
    const body: Record<string, unknown> = {};
    if (enabled !== current.enabled) body.enabled = enabled;
    if (chatLang !== current.chat_language) body.chat_language = chatLang;
    if (ttsLang !== current.tts.language) body.tts_language = ttsLang;
    if (Math.abs(ttsSpeed - current.tts.speed) > 0.001) body.tts_speed = ttsSpeed;
    if (ttsEngine.trim() !== current.tts.engine) body.tts_engine = ttsEngine.trim();
    if (ttsLaunchCmd.trim() !== (current.tts.launch_command ?? '')) body.tts_launch_command = ttsLaunchCmd.trim();
    if (ttsRefAudio.trim() !== (current.tts.reference_audio ?? '')) body.tts_reference_audio = ttsRefAudio.trim();
    if (ttsRefText.trim() !== (current.tts.reference_text ?? '')) body.tts_reference_text = ttsRefText.trim();
    if (ttsRefLang.trim() !== (current.tts.reference_language ?? '')) body.tts_reference_language = ttsRefLang.trim();
    if (ttsModelPath.trim() !== (current.tts.model_path ?? '')) body.tts_model_path = ttsModelPath.trim();
    if (ttsGpu !== current.tts.gpu_device) body.tts_gpu_device = ttsGpu;
    if (ttsVoice.trim() !== (current.tts.voice ?? '')) body.tts_voice = ttsVoice.trim();
    try {
      const r = await fetch(`${HTTP_BASE}/api/config/avatar`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`save failed: ${r.status} ${await r.text()}`);
      // Server returns a JSON body describing what got applied live
      // and whether a TTS child-process restart is pending. The
      // restart itself runs on a background task — the watchdog
      // updates /api/status when it finishes (success or fail).
      const result = await r.json().catch(() => ({}));
      if (result?.tts_error) {
        // Synchronous build error — bad path or similar. Surface now.
        setError(`Apply: ${result.tts_error}`);
      } else {
        setSavedAt(Date.now());
        setTimeout(() => setSavedAt(null), 4000);
      }
      onSaved();
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };

  return (
    <>
      <FieldRow label="Show avatar">
        <Toggle checked={enabled} onChange={setEnabled} />
      </FieldRow>
      <FieldRow label="Chat language">
        <select value={chatLang} onChange={(e) => setChatLang(e.target.value)} style={inputStyle}>
          {LANGUAGE_CHOICES.find((l) => l.code === chatLang) === undefined && (
            <option value={chatLang}>{chatLang} (custom)</option>
          )}
          {LANGUAGE_CHOICES.map((l) => (
            <option key={l.code} value={l.code}>{l.label}</option>
          ))}
        </select>
      </FieldRow>
      <FieldRow label="Voice language">
        <select value={ttsLang} onChange={(e) => setTtsLang(e.target.value)} style={inputStyle}>
          {LANGUAGE_CHOICES.find((l) => l.code === ttsLang) === undefined && (
            <option value={ttsLang}>{ttsLang} (custom)</option>
          )}
          {LANGUAGE_CHOICES.map((l) => (
            <option key={l.code} value={l.code}>{l.label}</option>
          ))}
        </select>
      </FieldRow>
      <FieldRow label="Voice speed">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flex: 1 }}>
          <input
            type="range" min={0.5} max={2.0} step={0.05}
            value={ttsSpeed}
            onChange={(e) => setTtsSpeed(Number(e.target.value))}
            style={{ flex: 1 }}
          />
          <span style={{ fontFamily: 'monospace', color: '#cbd5e1', minWidth: 48, textAlign: 'right' }}>
            {ttsSpeed.toFixed(2)}×
          </span>
        </div>
      </FieldRow>

      <Subsection label="Voice engine">
        <FieldRow label="Voice engine">
          <select
            value={isCustomEngine ? '__custom' : ttsEngine}
            onChange={(e) => {
              if (e.target.value === '__custom') return;
              setTtsEngine(e.target.value);
            }}
            style={inputStyle}
          >
            {ENGINE_SPECS.map((e) => (
              <option key={e.value} value={e.value}>{e.label}</option>
            ))}
            <option value="__custom">Other…</option>
          </select>
        </FieldRow>
        <div style={{ fontSize: 11, color: '#666', marginLeft: 168, marginTop: -4, marginBottom: 10, lineHeight: 1.5 }}>
          {spec.description}
        </div>
        {isCustomEngine && (
          <FieldRow label="Custom engine name">
            <input
              type="text"
              value={ttsEngine}
              onChange={(e) => setTtsEngine(e.target.value)}
              placeholder="my-engine"
              style={inputStyle}
            />
          </FieldRow>
        )}

        {spec.needsLauncher && (
          <>
            <FieldRow label="Python interpreter">
              <PathPicker
                value={ttsPython}
                onChange={setTtsPython}
                placeholder="C:/Users/.../envs/<env>/python.exe"
                pick={async () => {
                  const path = await pickFile({
                    title: 'Pick the Python interpreter (python.exe)',
                    filters: [
                      { label: 'Python executable', extensions: ['exe'] },
                      { label: 'All files', extensions: ['*'] },
                    ],
                  });
                  if (path) setTtsPython(path);
                }}
              />
            </FieldRow>
            <FieldRow label="Server script">
              <PathPicker
                value={ttsScript}
                onChange={setTtsScript}
                placeholder="tools/avatar/gptsovits_tts_server.py"
                pick={async () => {
                  const path = await pickFile({
                    title: 'Pick the TTS server script',
                    filters: [
                      { label: 'Python script', extensions: ['py'] },
                      { label: 'All files', extensions: ['*'] },
                    ],
                  });
                  if (path) setTtsScript(path);
                }}
              />
            </FieldRow>
            <div style={{ fontSize: 11, color: '#666', marginLeft: 168, marginTop: -4, marginBottom: 8, lineHeight: 1.5 }}>
              The Python the engine runs under and the wrapper script that
              serves <code style={{ color: '#888' }}>/tts</code>. The script
              can be either an absolute path or relative to the workspace
              root (where companion-server is launched from).
            </div>
          </>
        )}

        {spec.needsModelRoot && (
          <>
            <FieldRow label={spec.modelRootLabel ?? 'Engine model folder'}>
              <PathPicker
                value={ttsModelPath}
                onChange={setTtsModelPath}
                placeholder={spec.modelRootHint ?? 'C:/path/to/engine'}
                pick={async () => {
                  const path = await pickFolder({ title: `Pick the ${spec.modelRootLabel ?? 'engine'} folder` });
                  if (path) setTtsModelPath(path);
                }}
                buttonLabel="Browse folder"
              />
            </FieldRow>
            {spec.modelRootHint && (
              <div style={{ fontSize: 11, color: '#666', marginLeft: 168, marginTop: -4, marginBottom: 8, lineHeight: 1.5 }}>
                {spec.modelRootHint}
              </div>
            )}
          </>
        )}

        {spec.needsPresetVoice && (
          <FieldRow label="Voice">
            <select
              value={spec.presetVoices?.find((v) => v.value === ttsVoice) ? ttsVoice : '__custom'}
              onChange={(e) => {
                if (e.target.value === '__custom') return;
                setTtsVoice(e.target.value);
              }}
              style={inputStyle}
            >
              {spec.presetVoices?.map((v) => (
                <option key={v.value} value={v.value}>{v.label}</option>
              ))}
              <option value="__custom">Other…</option>
            </select>
          </FieldRow>
        )}
        {spec.needsPresetVoice && !spec.presetVoices?.find((v) => v.value === ttsVoice) && (
          <FieldRow label="Custom voice id">
            <input
              type="text"
              value={ttsVoice}
              onChange={(e) => setTtsVoice(e.target.value)}
              placeholder="e.g. ja-JP-SomeOtherNeural"
              style={inputStyle}
            />
          </FieldRow>
        )}

        {spec.needsVoiceSample && (
          <div style={{
            marginTop: 8, paddingTop: 12, paddingBottom: 4,
            borderTop: '1px solid #1f2227',
          }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 }}>
              <strong style={{ fontSize: 12, color: '#cbd5e1' }}>Voice sample</strong>
              <span style={{ fontSize: 11, color: '#666' }}>
                — short clip the engine uses as a prosody prompt on every line it speaks
              </span>
            </div>
            <FieldRow label="Sample audio">
              <PathPicker
                value={ttsRefAudio}
                onChange={setTtsRefAudio}
                placeholder="C:/Users/.../0003.wav  (a 3-10s clip of the target voice)"
                pick={async () => {
                  const path = await pickFile({
                    title: 'Pick a 3-10s voice sample clip',
                    filters: [
                      { label: 'Audio', extensions: ['wav', 'mp3', 'flac', 'ogg', 'm4a'] },
                      { label: 'All files', extensions: ['*'] },
                    ],
                  });
                  if (path) setTtsRefAudio(path);
                }}
              />
            </FieldRow>
            <FieldRow label="Sample transcript">
              <input
                type="text"
                value={ttsRefText}
                onChange={(e) => setTtsRefText(e.target.value)}
                placeholder="Exact words spoken in the sample audio"
                style={inputStyle}
              />
            </FieldRow>
            <FieldRow label="Sample language">
              <select
                value={ttsRefLang}
                onChange={(e) => setTtsRefLang(e.target.value)}
                style={inputStyle}
              >
                <option value="">(use voice language)</option>
                {LANGUAGE_CHOICES.map((l) => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            </FieldRow>
            <div style={{ fontSize: 11, color: '#666', marginLeft: 168, marginTop: 4, lineHeight: 1.5 }}>
              Zero-shot voice cloning: the engine reads the sample on each
              call to lock in timbre + speaking style. Pick a clean,
              expressive 3-10 second clip in a single take. Different
              samples give different reading styles from the same trained
              voice — calm clip → calm narration, bright clip → upbeat.
            </div>
          </div>
        )}
        {spec.needsGpu && (
          <>
            <FieldRow label="GPU device">
              <select
                value={ttsGpu}
                onChange={(e) => setTtsGpu(parseInt(e.target.value, 10))}
                style={{ ...inputStyle, maxWidth: 480 }}
              >
                <option value={-1}>CPU only (slow)</option>
                {detectedGpus.length > 0 ? (
                  detectedGpus.map((g) => (
                    <option key={g.index} value={g.index}>
                      GPU {g.index}: {g.name}
                      {g.vram_total_mb != null
                        ? ` (${(g.vram_total_mb / 1024).toFixed(1)} GB)`
                        : ''}
                    </option>
                  ))
                ) : (
                  // Fallback when detection failed — keep the form usable
                  // and let advanced users still pick GPU 0 manually.
                  <option value={0}>GPU 0 (auto-detect failed; pick manually)</option>
                )}
                {/* If user has saved an index outside the detected
                    range (e.g. detection returned only GPU 0 but
                    config saved GPU 2 from a previous setup), keep
                    that value selectable so saving doesn't silently
                    coerce. */}
                {ttsGpu >= 0 && !detectedGpus.find((g) => g.index === ttsGpu) && detectedGpus.length > 0 && (
                  <option value={ttsGpu}>GPU {ttsGpu} (saved; not detected on this machine)</option>
                )}
              </select>
            </FieldRow>
            <div style={{ fontSize: 11, color: '#666', marginLeft: 168, marginTop: -4, marginBottom: 8, lineHeight: 1.5 }}>
              {detectedGpus.length === 0
                ? 'GPU detection unavailable (nvidia-smi not on PATH). Pick GPU 0 if you have one CUDA card, or CPU.'
                : `Detected ${detectedGpus.length} GPU${detectedGpus.length === 1 ? '' : 's'} on this machine.`}
            </div>
          </>
        )}
        <div style={{ fontSize: 11, color: '#666', marginTop: 4, lineHeight: 1.5 }}>
          The avatar's Live2D model and default expression are set
          per-character on the <a href="/" style={{ color: '#7aa9ff' }}>Home page</a>.
        </div>
      </Subsection>

      <EditorFooter
        status={
          <>
            {error && <Hint tone="warn">{error}</Hint>}
            {/* Order matters: a fresh dirty edit should switch back
                to "unsaved" instead of stale-"Applied". */}
            {!error && dirty && <Hint tone="muted">unsaved changes</Hint>}
            {!error && !dirty && savedAt && <Hint tone="good">✓ Applied — voice changes are live.</Hint>}
          </>
        }
      >
        <Button onClick={save} primary disabled={!dirty || saving}>
          {saving ? 'Applying…' : 'Apply'}
        </Button>
      </EditorFooter>
    </>
  );
}

// ── Subagent editor ──────────────────────────────────────────────

type Backend = 'direct' | 'webhook';

function SubagentEditor({
  current, tomlHintDismissed, onDismissHint,
}: {
  current: AvatarConfigView['subagent'];
  tomlHintDismissed: boolean;
  onDismissHint: () => void;
}) {
  const [enabled, setEnabled] = useState<boolean>(current.enabled);
  const [onlyXlate, setOnlyXlate] = useState<boolean>(current.only_when_translating);
  const [streaming, setStreaming] = useState<boolean>(current.streaming);
  const [timeout, setTimeout_] = useState<number>(current.timeout_secs);
  const [backend, setBackend] = useState<Backend>(current.use_zeroclaw_webhook ? 'webhook' : 'direct');
  const [apiKey, setApiKey] = useState<string>('');
  const [model, setModel] = useState<string>(current.llm_model || '');
  const [baseUrl, setBaseUrl] = useState<string>(current.llm_base_url || '');
  // `current.llm_disable_thinking` may be undefined on older server
  // builds — default to true (the historical hardcoded behavior).
  const [disableThinking, setDisableThinking] = useState<boolean>(current.llm_disable_thinking ?? true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dirty =
    enabled !== current.enabled ||
    onlyXlate !== current.only_when_translating ||
    streaming !== current.streaming ||
    timeout !== current.timeout_secs ||
    backend !== (current.use_zeroclaw_webhook ? 'webhook' : 'direct') ||
    apiKey.length > 0 ||
    model.trim() !== (current.llm_model || '') ||
    baseUrl.trim() !== (current.llm_base_url || '') ||
    disableThinking !== (current.llm_disable_thinking ?? true);

  const save = async () => {
    setSaving(true); setError(null);
    try {
      // Avatar-side toggles → /api/config/avatar (subagent.enabled,
      // subagent.only_when_translating live under [avatar.subagent] in
      // the TOML hierarchy, so we route them through the avatar override
      // path which knows how to patch that subtree).
      const avatarBody: Record<string, unknown> = {};
      if (enabled !== current.enabled) avatarBody.subagent_enabled = enabled;
      if (onlyXlate !== current.only_when_translating) avatarBody.subagent_only_when_translating = onlyXlate;
      if (streaming !== current.streaming) avatarBody.subagent_streaming = streaming;
      if (Object.keys(avatarBody).length) {
        const r = await fetch(`${HTTP_BASE}/api/config/avatar`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(avatarBody),
        });
        if (!r.ok) throw new Error(`avatar save: ${r.status} ${await r.text()}`);
      }
      // Backend + LLM connection → /api/config/subagent.
      const subBody: Record<string, unknown> = {};
      if (backend !== (current.use_zeroclaw_webhook ? 'webhook' : 'direct')) {
        subBody.use_zeroclaw_webhook = backend === 'webhook';
      }
      if (apiKey.length > 0) subBody.api_key = apiKey;
      if (model.trim() !== (current.llm_model || '')) subBody.model = model.trim();
      if (baseUrl.trim() !== (current.llm_base_url || '')) subBody.base_url = baseUrl.trim();
      if (disableThinking !== (current.llm_disable_thinking ?? true)) subBody.disable_thinking = disableThinking;
      if (timeout !== current.timeout_secs) subBody.timeout_secs = timeout;
      if (Object.keys(subBody).length) {
        const r = await fetch(`${HTTP_BASE}/api/config/subagent`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(subBody),
        });
        if (!r.ok) throw new Error(`subagent save: ${r.status} ${await r.text()}`);
      }
      setSavedAt(Date.now());
      setApiKey('');
      // The server hot-swapped the subagent in-place. Fade the
      // "Applied" hint after 4s so it doesn't linger.
      setTimeout(() => setSavedAt(null), 4000);
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };

  return (
    <>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 12, lineHeight: 1.5 }}>
        When your chat language doesn't match the voice language, this
        translates replies before speaking. It also picks the right facial
        expression for each line.
      </div>
      <FieldRow label="Translate replies">
        <Toggle checked={enabled} onChange={setEnabled} />
      </FieldRow>
      <FieldRow label="Only when needed">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Toggle checked={onlyXlate} onChange={setOnlyXlate} />
          <span style={{ fontSize: 11, color: '#666' }}>
            {onlyXlate
              ? 'skip when chat & voice are the same language'
              : 'always run, even for same-language chats'}
          </span>
        </div>
      </FieldRow>

      <div style={{
        display: 'flex', gap: 12, padding: '10px 0', borderBottom: '1px solid #1f2227',
        fontSize: 13, alignItems: 'center', flexWrap: 'wrap',
      }}>
        <span style={{ minWidth: 160, color: '#888' }}>How it runs</span>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center', cursor: 'pointer' }}>
          <input type="radio" name="backend" checked={backend === 'direct'} onChange={() => setBackend('direct')} />
          <span style={{ color: backend === 'direct' ? '#10b981' : '#cbd5e1' }}>
            Direct AI <span style={{ color: '#666' }}>(fast — needs an API key)</span>
          </span>
        </label>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center', cursor: 'pointer' }}>
          <input type="radio" name="backend" checked={backend === 'webhook'} onChange={() => setBackend('webhook')} />
          <span style={{ color: backend === 'webhook' ? '#f59e0b' : '#cbd5e1' }}>
            Through main agent <span style={{ color: '#666' }}>(slower, no key needed)</span>
          </span>
        </label>
      </div>

      {backend === 'direct' && (
        <Subsection label="AI service">
          <FieldRow label="API endpoint">
            <input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.openai.com/v1" style={monoInputStyle} />
          </FieldRow>
          <FieldRow label="Model name">
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)}
              placeholder="gpt-4o-mini" style={monoInputStyle} />
          </FieldRow>
          <FieldRow
            label="API key"
            hint="Saved on this computer only (companion.runtime.json). Keep that file out of git."
          >
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={current.llm_api_key_set ? '••• saved (paste to replace)' : 'paste your OpenAI / z.ai / etc. key'}
              style={monoInputStyle}
              autoComplete="off"
            />
          </FieldRow>
          <FieldRow
            label="Model reasoning"
            hint={disableThinking
              ? 'Off — sends thinking:{type:disabled}. GLM-4.5/4.6/5 family skip chain-of-thought (~1 s vs ~15–25 s). Other endpoints ignore the flag.'
              : 'On — the model reasons before answering. Slower, but better translation/expression picks on tricky inputs.'}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Toggle checked={!disableThinking} onChange={(on) => setDisableThinking(!on)} />
              <span style={{ fontSize: 12, color: tokens.textMuted }}>
                {disableThinking ? 'off (fast)' : 'on (slower, richer)'}
              </span>
            </div>
          </FieldRow>
        </Subsection>
      )}

      <Subsection label="Timing & streaming">
        <FieldRow label="Time limit (seconds)">
          <input
            type="number" min={5} max={300}
            value={timeout}
            onChange={(e) => setTimeout_(Math.max(1, parseInt(e.target.value, 10) || 60))}
            style={{ ...inputStyle, maxWidth: 100 }}
          />
        </FieldRow>
        <div style={{ fontSize: 11, color: '#666', marginLeft: 168, marginBottom: 8 }}>
          How long to wait for a translation before giving up.
          Direct AI usually replies in 1–3 seconds; the main-agent path
          can take 5–10.
        </div>
        <FieldRow label="Stream while speaking">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Toggle checked={streaming} onChange={setStreaming} />
            <span style={{ fontSize: 11, color: '#666' }}>
              {streaming
                ? 'TTS starts on the first sentence (~3s) — faster, uses keyword expressions'
                : 'wait for the full translation (~15-25s) before speaking — picks richer expressions'}
            </span>
          </div>
        </FieldRow>
        <div style={{ fontSize: 11, color: '#666', marginLeft: 168 }}>
          Streaming requires <strong>Direct AI</strong> mode (above).
          With "Through main agent" it falls back to the non-streaming
          path automatically.
        </div>
      </Subsection>

      <EditorFooter
        status={
          <>
            {error && <Hint tone="warn">{error}</Hint>}
            {!error && dirty && <Hint tone="muted">unsaved changes</Hint>}
            {!error && !dirty && savedAt && <Hint tone="good">✓ Applied — subagent swapped live.</Hint>}
          </>
        }
      >
        <Button onClick={save} primary disabled={!dirty || saving}>
          {saving ? 'Applying…' : 'Apply'}
        </Button>
      </EditorFooter>

      {backend === 'webhook' && !tomlHintDismissed && (
        <SubagentSpeedupHint onDismiss={onDismissHint} />
      )}
    </>
  );
}

// ── Toggle / generic widgets ────────────────────────────────────

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      role="switch"
      aria-checked={checked}
      className="ws-btn"
      style={{
        width: 38, height: 22,
        background: checked ? tokens.primary : '#2a2f3a',
        borderRadius: 11, border: 'none', position: 'relative',
        cursor: 'pointer', flexShrink: 0, padding: 0,
        transition: 'background 120ms ease',
      }}
    >
      <span style={{
        position: 'absolute', top: 2, left: checked ? 18 : 2,
        width: 18, height: 18, borderRadius: '50%',
        background: '#fff',
        boxShadow: '0 1px 2px rgba(0,0,0,0.3)',
        transition: 'left 140ms cubic-bezier(0.4, 0, 0.2, 1)',
      }} />
    </button>
  );
}

/** Inline path field with a Browse button. Wraps the native file
 *  picker so the user doesn't have to type or paste OS paths by hand. */
function PathPicker({
  value, onChange, placeholder, pick, buttonLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  pick: () => Promise<void>;
  buttonLabel?: string;
}) {
  const [picking, setPicking] = useState(false);
  return (
    <div style={{ display: 'flex', gap: 6, flex: 1, minWidth: 0 }}>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        style={{ ...inputStyle, flex: 1, minWidth: 0 }}
      />
      <button
        type="button"
        disabled={picking}
        onClick={async () => {
          setPicking(true);
          try { await pick(); }
          finally { setPicking(false); }
        }}
        style={{
          padding: '8px 14px', background: 'transparent', color: '#888',
          border: '1px solid #2a2d33', borderRadius: 6, fontSize: 13,
          cursor: picking ? 'not-allowed' : 'pointer', opacity: picking ? 0.5 : 1,
          flexShrink: 0,
        }}
      >
        {picking ? '…' : (buttonLabel ?? 'Browse')}
      </button>
    </div>
  );
}

function FieldRow({
  label, hint, children,
}: {
  label: string;
  /** Optional secondary copy under the field. */
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div style={{
      display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap',
      padding: '12px 0',
      borderBottom: `1px solid ${tokens.border}`,
    }}>
      <label style={{
        minWidth: 168,
        paddingTop: 9,                 // visually centers against the input
        color: tokens.textMuted,
        fontSize: 12.5,
        fontWeight: 500,
        letterSpacing: '0.005em',
      }}>{label}</label>
      <div style={{
        flex: '1 1 280px', minWidth: 220,
        display: 'flex', flexDirection: 'column', gap: 6,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {children}
        </div>
        {hint && (
          <div style={{ fontSize: 11.5, color: tokens.textDim, lineHeight: 1.5 }}>
            {hint}
          </div>
        )}
      </div>
    </div>
  );
}

function SubagentSpeedupHint({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div style={{
      marginTop: 12, padding: 14, background: '#1e2433',
      border: '1px solid #2d3a55', borderRadius: 8,
      fontSize: 12, color: '#cbd5e1', lineHeight: 1.55, position: 'relative',
    }}>
      <button type="button" onClick={onDismiss} title="Dismiss" style={{
        position: 'absolute', top: 8, right: 8, background: 'transparent',
        border: 'none', color: '#888', cursor: 'pointer', fontSize: 14,
      }}>✕</button>
      <div style={{ fontWeight: 600, color: '#fff', marginBottom: 6 }}>💡 Make this faster</div>
      Routing through the main agent adds 5–10 seconds per reply. If you
      have an OpenAI / z.ai / similar API key, switch the option above to
      <strong> Direct AI</strong> for ~1–3 second replies.
      <div style={{ marginTop: 6, color: '#94a3b8' }}>
        Cheap fast options: gpt-4o-mini, Groq Llama-3.3-70B, Z.ai GLM-4-Flash.
        Or run Ollama locally for free at <code>localhost:11434/v1</code>.
        Hit <strong>Save</strong> then <strong>Restart</strong> after you change it.
      </div>
    </div>
  );
}

/** A labelled subsection within a Section. Renders the children
 *  inline (always visible — no click-to-expand) under a small caps
 *  header with a divider above, so related controls stay grouped
 *  without hiding anything behind a disclosure. */
function Subsection({
  label, children,
}: { label: string; children: React.ReactNode }) {
  return (
    <div style={{
      marginTop: 18,
      paddingTop: 14,
      borderTop: `1px solid ${tokens.border}`,
    }}>
      <div style={{
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        color: tokens.textDim,
        marginBottom: 10,
      }}>{label}</div>
      {children}
    </div>
  );
}

function Section({
  title, description, children,
}: {
  title: string;
  /** Optional one-liner under the section title. Sets context for the section's controls. */
  description?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section style={{
      background: tokens.bgPanel,
      border: `1px solid ${tokens.border}`,
      borderRadius: tokens.radius,
      padding: '20px 22px',
      marginTop: 20,
    }}>
      <header style={{ marginBottom: description ? 14 : 12 }}>
        <h2 style={{
          margin: 0,
          fontSize: 15.5,
          fontWeight: 600,
          color: tokens.text,
          letterSpacing: '-0.005em',
        }}>{title}</h2>
        {description && (
          <p style={{
            margin: '4px 0 0 0',
            fontSize: 12.5,
            color: tokens.textMuted,
            lineHeight: 1.55,
          }}>{description}</p>
        )}
      </header>
      {children}
    </section>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 12 }}>
      {children}
    </div>
  );
}

/// Footer toolbar for an editor: status hints on the left, action
/// buttons on the right, separator above. Use this in place of an
/// ad-hoc `<Row>` to give every editor the same end-of-form rhythm.
function EditorFooter({
  status, children,
}: {
  status?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
      marginTop: 16,
      paddingTop: 16,
      borderTop: `1px solid ${tokens.border}`,
    }}>
      <div style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        {status}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {children}
      </div>
    </div>
  );
}

function ReadonlyRow({
  label, value, tone,
}: {
  label: string;
  value: string;
  tone?: 'good' | 'warn' | 'muted';
}) {
  const color = tone === 'good' ? '#10b981' : tone === 'warn' ? '#f59e0b' : '#cbd5e1';
  return (
    <div style={{
      display: 'flex', gap: 12, padding: '6px 0',
      borderBottom: '1px solid #1f2227', fontSize: 13,
    }}>
      <span style={{ minWidth: 160, color: '#888' }}>{label}</span>
      <span style={{ color, fontFamily: 'ui-monospace, monospace', fontSize: 12, wordBreak: 'break-all' }}>
        {value}
      </span>
    </div>
  );
}

function Hint({ tone, children }: { tone: 'muted' | 'good' | 'warn'; children: React.ReactNode }) {
  const color =
    tone === 'good' ? tokens.success :
    tone === 'warn' ? tokens.warn :
    tokens.textDim;
  return (
    <div style={{
      fontSize: 12,
      color,
      lineHeight: 1.5,
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
    }}>
      {children}
    </div>
  );
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div role="alert" style={{
      background: 'rgba(239, 68, 68, 0.10)',
      border: `1px solid rgba(239, 68, 68, 0.30)`,
      color: '#fca5a5',
      padding: '12px 14px',
      borderRadius: tokens.radius,
      marginTop: 16,
      fontSize: 13,
      lineHeight: 1.5,
    }}>
      <strong style={{ color: '#fecaca' }}>Failed to load config.</strong>{' '}
      {message}
    </div>
  );
}

function Button({
  children, onClick, primary, disabled, title,
}: {
  children: React.ReactNode;
  onClick: () => void;
  primary?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  const isPrimary = !!primary && !disabled;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`ws-btn${isPrimary ? ' ws-btn--primary' : ''}`}
      style={{
        padding: '8px 14px',
        background: isPrimary ? tokens.primary : 'transparent',
        color: isPrimary ? '#fff' : tokens.textMuted,
        border: `1px solid ${isPrimary ? tokens.primary : tokens.border}`,
        borderRadius: tokens.radiusSm,
        fontSize: 12.5,
        fontWeight: 500,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
        minHeight: 34,
      }}
    >
      {children}
    </button>
  );
}

// inputStyle / monoInputStyle moved to ../lib/theme — imported above.
