import { useState, useCallback, useRef, useEffect } from 'react';
import Live2DViewer, { type Live2DViewerHandle, type ModelActions } from '../components/avatar/Live2DViewer';
import AvatarControls from '../components/avatar/AvatarControls';
import {
  useAvatarSocket,
  type LipSyncDataProto,
  type DebugFrame,
} from '../components/avatar/useAvatarSocket';
import { HTTP_BASE, WS_BASE } from '../lib/apiBase';

interface ModelInfo {
  modelUrl: string;
  scale: number;
  anchor: string;
  defaultExpression: string;
}

interface ChatTurn {
  role: 'user' | 'assistant';
  text: string;
  ts: number; // epoch ms
  /** Diagnostic info attached to assistant turns (subagent translation). */
  debug?: DebugFrame;
}

const HISTORY_KEY = 'companion.chatHistory.v1';
const HISTORY_LIMIT = 200; // keep last N turns

// ── Canvas / panel preferences ──────────────────────────────────
interface CanvasPrefs {
  background: string;     // CSS color, e.g. '#0a0a0a' or 'transparent'
  transparent: boolean;
  showControls: boolean;  // expressions/motions panel
  /** Multiplier on the auto-fit scale. 1 = fit to canvas, >1 zooms in. */
  scaleMultiplier: number;
  /** Pixel offset from the canvas center, after auto-fit. */
  offsetX: number;
  offsetY: number;
}
const PREFS_KEY = 'companion.avatarPrefs.v1';
const DEFAULT_PREFS: CanvasPrefs = {
  background: '#0a0a0a',
  transparent: false,
  showControls: false, // hidden by default per user request
  scaleMultiplier: 1,
  offsetX: 0,
  offsetY: 0,
};
function loadPrefs(): CanvasPrefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    return { ...DEFAULT_PREFS, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}
function savePrefs(p: CanvasPrefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  } catch { /* non-fatal */ }
}

function loadHistory(): ChatTurn[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(-HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function saveHistory(history: ChatTurn[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-HISTORY_LIMIT)));
  } catch {
    // localStorage full or disabled; non-fatal
  }
}

export default function Avatar() {
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null);
  const [subtitle, setSubtitle] = useState<string>('');
  const [isPlaying, setIsPlaying] = useState(false);
  const [lipSyncData, setLipSyncData] = useState<LipSyncDataProto | null>(null);
  const [chatInput, setChatInput] = useState('');
  const [sending, setSending] = useState(false);
  const [modelActions, setModelActions] = useState<ModelActions>({ expressions: [], motions: [] });
  const [pendingAudio, setPendingAudio] = useState<HTMLAudioElement | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [history, setHistory] = useState<ChatTurn[]>(() => loadHistory());
  const [prefs, setPrefs] = useState<CanvasPrefs>(() => loadPrefs());
  const [showSettings, setShowSettings] = useState<boolean>(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUnlockedRef = useRef(false);
  const audioUrlRef = useRef<string | null>(null);
  const viewerRef = useRef<Live2DViewerHandle>(null);
  const historyRef = useRef<HTMLDivElement>(null);

  useEffect(() => savePrefs(prefs), [prefs]);

  /** Stop any audio currently playing and free its blob URL. Called
   *  before starting a new audio frame OR on unmount. Without this,
   *  back-to-back chats produce two simultaneous Asuna voices. */
  const stopCurrentAudio = useCallback(() => {
    if (audioRef.current) {
      try {
        audioRef.current.pause();
        audioRef.current.src = '';
      } catch { /* ignore */ }
      audioRef.current = null;
    }
    if (audioUrlRef.current) {
      try { URL.revokeObjectURL(audioUrlRef.current); } catch { /* ignore */ }
      audioUrlRef.current = null;
    }
  }, []);

  const wsUrl = `${WS_BASE}/ws/avatar`;

  // Auto-scroll history to bottom whenever it grows.
  useEffect(() => {
    const el = historyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [history]);

  // Persist history on every change.
  useEffect(() => {
    saveHistory(history);
  }, [history]);

  const appendTurn = useCallback((turn: ChatTurn) => {
    setHistory((prev) => [...prev, turn].slice(-HISTORY_LIMIT));
  }, []);

  const clearHistory = useCallback(() => {
    if (confirm('Clear all chat history?')) {
      setHistory([]);
    }
  }, []);

  const { connected, sendReady, sendMotionRequest, sendExpressionRequest } = useAvatarSocket(wsUrl, {
    onModelInfo: (info) => {
      setModelInfo(info);
      sendReady();
    },
    onExpression: (name) => {
      viewerRef.current?.setExpression(name);
    },
    onMotion: (group, _name) => {
      const match = modelActions.motions.find((m) => m.group === group);
      if (match) viewerRef.current?.playMotion(match.group, match.index);
    },
    onAudio: (audioBase64, format, _sampleRate, lipSync) => {
      // Critical: stop any in-flight audio before starting a new one.
      // Otherwise back-to-back chats stack — you'd hear two Asunas
      // overlapping. Pause the previous element AND revoke its blob
      // URL so no orphan plays.
      stopCurrentAudio();

      const mime = format === 'mp3' ? 'mpeg' : format;
      const audioBlob = new Blob(
        [Uint8Array.from(atob(audioBase64), (c) => c.charCodeAt(0))],
        { type: `audio/${mime}` }
      );
      const audioUrl = URL.createObjectURL(audioBlob);
      const audio = new Audio(audioUrl);
      audioRef.current = audio;
      audioUrlRef.current = audioUrl;
      audio.onended = () => {
        setIsPlaying(false);
        setPendingAudio(null);
        if (audioUrlRef.current === audioUrl) {
          URL.revokeObjectURL(audioUrl);
          audioUrlRef.current = null;
        }
      };
      setIsPlaying(true);
      setLipSyncData(lipSync);
      audio.play()
        .then(() => {
          audioUnlockedRef.current = true;
          setAudioError(null);
        })
        .catch((err) => {
          console.error('audio playback blocked:', err);
          setIsPlaying(false);
          setAudioError(err.name === 'NotAllowedError'
            ? 'Browser blocked audio. Click "Play" to enable.'
            : `Audio error: ${err.message}`);
          setPendingAudio(audio);
        });
    },
    onText: (content) => {
      setSubtitle(content);
      // Append the assistant's reply to history (the chat-language text
      // shown in the subtitle, NOT the TTS-language version).
      appendTurn({ role: 'assistant', text: content, ts: Date.now() });
    },
    onDebug: (frame) => {
      // Attach the diagnostic frame to the most-recent assistant turn
      // so the user can verify the subagent translated correctly via
      // the chat bubble's "details" expander.
      setHistory((prev) => {
        const next = [...prev];
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === 'assistant') {
            next[i] = { ...next[i], debug: frame };
            break;
          }
        }
        return next;
      });
    },
    onIdle: () => {
      setSubtitle('');
      setLipSyncData(null);
    },
    onError: (message) => console.error('Avatar error:', message),
  });

  const handleExpression = useCallback((name: string) => {
    sendExpressionRequest(name);
    viewerRef.current?.setExpression(name);
  }, [sendExpressionRequest]);

  const handleMotion = useCallback((group: string, index: number) => {
    sendMotionRequest(group, String(index));
    viewerRef.current?.playMotion(group, index);
  }, [sendMotionRequest]);

  const handleSendChat = useCallback(async () => {
    const text = chatInput.trim();
    if (!text || sending) return;

    // Browser autoplay pre-warm via 1-frame silent WAV.
    if (!audioUnlockedRef.current) {
      const silent = new Audio(
        'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA='
      );
      silent.play().then(() => { audioUnlockedRef.current = true; }).catch(() => {});
    }

    appendTurn({ role: 'user', text, ts: Date.now() });
    setSending(true);
    setChatInput('');
    try {
      const resp = await fetch(`${HTTP_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      if (!resp.ok) {
        const body = await resp.text();
        console.error('Chat send failed:', resp.status, body);
        // Show the human-readable detail companion-server returns —
        // it explains exactly what went wrong (timeout / upstream error).
        let display: string;
        if (resp.status === 504) {
          display = `⏱️  Timed out. ${body || 'The agent took too long; bump [zeroclaw] timeout_secs in companion.toml.'}`;
        } else if (resp.status === 502) {
          display = `🔌  Upstream zeroclaw failed: ${body || 'unknown error'}`;
        } else {
          display = `[error ${resp.status}] ${body || resp.statusText}`;
        }
        appendTurn({ role: 'assistant', text: display, ts: Date.now() });
      }
      // Note: assistant reply is appended via the onText WS handler, not
      // here. /api/chat returns the same text but the WS path delivers it
      // alongside the audio frame — we want history and audio in sync.
    } catch (e) {
      console.error('Chat send error:', e);
      appendTurn({
        role: 'assistant',
        text: `[network error: ${(e as Error).message}]`,
        ts: Date.now(),
      });
    } finally {
      setSending(false);
    }
  }, [chatInput, sending, appendTurn]);

  useEffect(() => {
    return () => stopCurrentAudio();
  }, [stopCurrentAudio]);

  const canvasBg = prefs.transparent
    ? 'transparent'
    : (prefs.background || '#0a0a0a');

  return (
    <div style={{ display: 'flex', flexDirection: 'row', height: '100%', gap: 12, padding: 12 }}>
      {/* Left column: avatar + (collapsible) controls */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
        <div
          style={{
            flex: 1,
            position: 'relative',
            borderRadius: 12,
            overflow: 'hidden',
            background: canvasBg,
            minHeight: 0,
            // Subtle checker pattern when transparent so the user can
            // see the canvas extents.
            backgroundImage: prefs.transparent
              ? 'linear-gradient(45deg, #1a1a1a 25%, transparent 25%), linear-gradient(-45deg, #1a1a1a 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #1a1a1a 75%), linear-gradient(-45deg, transparent 75%, #1a1a1a 75%)'
              : undefined,
            backgroundSize: prefs.transparent ? '20px 20px' : undefined,
            backgroundPosition: prefs.transparent ? '0 0, 0 10px, 10px -10px, -10px 0px' : undefined,
          }}
        >
          {modelInfo ? (
            <Live2DViewer
              ref={viewerRef}
              modelUrl={modelInfo.modelUrl}
              scale={modelInfo.scale}
              anchor={modelInfo.anchor}
              defaultExpression={modelInfo.defaultExpression}
              lipSyncData={lipSyncData}
              isPlaying={isPlaying}
              onActionsReady={setModelActions}
              scaleMultiplier={prefs.scaleMultiplier}
              offsetX={prefs.offsetX}
              offsetY={prefs.offsetY}
            />
          ) : (
            <div
              style={{
                height: '100%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#888',
              }}
            >
              <div style={{ textAlign: 'center' }}>
                <div style={{ fontSize: 32, marginBottom: 12 }}>{connected ? '🎭' : '🔌'}</div>
                <div>{connected ? 'Waiting for model info…' : 'Connecting to avatar…'}</div>
              </div>
            </div>
          )}
          {pendingAudio && (
            <div
              style={{
                position: 'absolute',
                top: 16,
                left: '50%',
                transform: 'translateX(-50%)',
                background: '#3b82f6',
                color: '#fff',
                padding: '10px 18px',
                borderRadius: 10,
                fontSize: 14,
                cursor: 'pointer',
                fontWeight: 600,
                boxShadow: '0 4px 12px rgba(59,130,246,0.4)',
              }}
              onClick={() => {
                pendingAudio
                  .play()
                  .then(() => {
                    audioUnlockedRef.current = true;
                    setIsPlaying(true);
                    setAudioError(null);
                    setPendingAudio(null);
                  })
                  .catch((e) => setAudioError(`Still blocked: ${e.message}`));
              }}
            >
              ▶  {audioError ?? 'Click to play audio'}
            </div>
          )}
          {subtitle && (
            <div
              style={{
                position: 'absolute',
                bottom: 16,
                left: '50%',
                transform: 'translateX(-50%)',
                maxWidth: '80%',
                background: 'rgba(0, 0, 0, 0.7)',
                color: '#fff',
                padding: '8px 16px',
                borderRadius: 10,
                fontSize: 14,
                backdropFilter: 'blur(4px)',
              }}
            >
              {subtitle}
            </div>
          )}
          {/* Floating top-right settings/toggle row */}
          <div
            style={{
              position: 'absolute',
              top: 12,
              right: 12,
              display: 'flex',
              gap: 6,
            }}
          >
            <CanvasButton
              title="Canvas settings"
              onClick={() => setShowSettings((s) => !s)}
              active={showSettings}
            >
              ⚙
            </CanvasButton>
            <CanvasButton
              title={prefs.showControls ? 'Hide expressions / motions' : 'Show expressions / motions'}
              onClick={() => setPrefs((p) => ({ ...p, showControls: !p.showControls }))}
              active={prefs.showControls}
            >
              {prefs.showControls ? '✕' : '☰'}
            </CanvasButton>
          </div>
          {showSettings && (
            <CanvasSettingsPopover
              prefs={prefs}
              onChange={setPrefs}
              onClose={() => setShowSettings(false)}
            />
          )}
        </div>

        {prefs.showControls && (
          <div
            style={{
              background: '#16181c',
              borderRadius: 10,
              padding: 12,
              flexShrink: 0,
            }}
          >
            <AvatarControls
              expressions={modelActions.expressions}
              motions={modelActions.motions}
              onExpressionRequest={handleExpression}
              onMotionRequest={handleMotion}
            />
          </div>
        )}
      </div>

      {/* Right column: chat history + input */}
      <div
        style={{
          width: 380,
          display: 'flex',
          flexDirection: 'column',
          background: '#16181c',
          borderRadius: 12,
          minHeight: 0,
        }}
      >
        <div
          style={{
            padding: '10px 14px',
            borderBottom: '1px solid #1f2227',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ fontSize: 13, fontWeight: 600 }}>Chat history</div>
          <div style={{ fontSize: 11, color: '#666', display: 'flex', gap: 12, alignItems: 'center' }}>
            <span>{history.length} turn{history.length === 1 ? '' : 's'}</span>
            <button
              type="button"
              onClick={clearHistory}
              disabled={history.length === 0}
              style={{
                background: 'transparent',
                color: '#888',
                border: '1px solid #2a2d33',
                borderRadius: 4,
                padding: '2px 8px',
                fontSize: 11,
                cursor: history.length === 0 ? 'not-allowed' : 'pointer',
                opacity: history.length === 0 ? 0.4 : 1,
              }}
            >
              Clear
            </button>
          </div>
        </div>

        <div
          ref={historyRef}
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: 12,
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
            minHeight: 0,
          }}
        >
          {history.length === 0 && (
            <div style={{ color: '#666', fontSize: 13, textAlign: 'center', marginTop: 32 }}>
              No messages yet. Type below to start.
            </div>
          )}
          {history.map((turn, i) => (
            <ChatBubble key={`${turn.ts}-${i}`} turn={turn} />
          ))}
        </div>

        <div style={{ padding: 12, borderTop: '1px solid #1f2227' }}>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSendChat()}
              placeholder="Send a message…"
              style={{
                flex: 1,
                background: '#0b0d10',
                color: '#fff',
                padding: '10px 14px',
                borderRadius: 8,
                border: '1px solid #2a2d33',
                fontSize: 14,
                outline: 'none',
              }}
            />
            <button
              type="button"
              onClick={handleSendChat}
              disabled={!chatInput.trim() || sending}
              style={{
                padding: '10px 18px',
                background: chatInput.trim() && !sending ? '#3b82f6' : '#1f2937',
                color: '#fff',
                border: 'none',
                borderRadius: 8,
                fontSize: 14,
                cursor: chatInput.trim() && !sending ? 'pointer' : 'not-allowed',
              }}
            >
              {sending ? '…' : 'Send'}
            </button>
          </div>
          <div
            style={{
              marginTop: 8,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 11,
              color: '#666',
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: connected ? '#10b981' : '#ef4444',
              }}
            />
            <span>{connected ? 'Connected to companion' : 'Disconnected'}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function ChatBubble({ turn }: { turn: ChatTurn }) {
  const isUser = turn.role === 'user';
  const [showDetails, setShowDetails] = useState(false);
  const hasDetails = !isUser && !!turn.debug;

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: isUser ? 'flex-end' : 'flex-start',
        gap: 2,
        maxWidth: '100%',
      }}
    >
      <div style={{ fontSize: 10, color: '#666', padding: '0 4px' }}>
        {isUser ? 'you' : 'asuna'} · {fmtTime(turn.ts)}
        {hasDetails && (
          <button
            type="button"
            onClick={() => setShowDetails((s) => !s)}
            style={{
              marginLeft: 6,
              background: 'transparent',
              border: 'none',
              color: '#3b82f6',
              fontSize: 10,
              cursor: 'pointer',
              padding: 0,
            }}
          >
            {showDetails ? 'hide details' : 'details'}
          </button>
        )}
      </div>
      <div
        style={{
          maxWidth: '88%',
          padding: '8px 12px',
          borderRadius: 10,
          background: isUser ? '#1e3a5f' : '#1f2227',
          color: '#fff',
          fontSize: 13,
          lineHeight: 1.45,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {turn.text}
      </div>
      {hasDetails && showDetails && turn.debug && (
        <div
          style={{
            maxWidth: '88%',
            marginTop: 4,
            padding: '8px 10px',
            background: '#0d0e12',
            border: '1px solid #1f2227',
            borderRadius: 8,
            fontSize: 11,
            color: '#aaa',
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          <DebugRow label="expression" value={turn.debug.expression} />
          <DebugRow
            label="subagent"
            value={turn.debug.subagent_used ? '✓ used (LLM-driven)' : '✗ fell back to keyword detection'}
            tone={turn.debug.subagent_used ? '#10b981' : '#f59e0b'}
          />
          <DebugRow label="chat text" value={turn.debug.chat_text} mono />
          <DebugRow
            label="spoken text"
            value={turn.debug.spoken_text}
            mono
            highlight={turn.debug.chat_text !== turn.debug.spoken_text}
          />
        </div>
      )}
    </div>
  );
}

function DebugRow({
  label,
  value,
  mono,
  highlight,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  highlight?: boolean;
  tone?: string;
}) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
      <span style={{ minWidth: 80, color: '#666', fontSize: 10 }}>{label}</span>
      <span
        style={{
          flex: 1,
          color: tone ?? (highlight ? '#a5b4fc' : '#ddd'),
          fontFamily: mono ? 'ui-monospace, monospace' : undefined,
          fontSize: 11,
          wordBreak: 'break-word',
        }}
      >
        {value || <em style={{ color: '#555' }}>(empty)</em>}
      </span>
    </div>
  );
}

function CanvasButton({
  children,
  onClick,
  title,
  active,
}: {
  children: React.ReactNode;
  onClick: () => void;
  title: string;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      style={{
        width: 32,
        height: 32,
        background: active ? '#3b82f6' : 'rgba(0,0,0,0.6)',
        color: '#fff',
        border: '1px solid rgba(255,255,255,0.1)',
        borderRadius: 8,
        fontSize: 14,
        cursor: 'pointer',
        backdropFilter: 'blur(4px)',
        padding: 0,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      {children}
    </button>
  );
}

function CanvasSettingsPopover({
  prefs,
  onChange,
  onClose,
}: {
  prefs: CanvasPrefs;
  onChange: (next: CanvasPrefs) => void;
  onClose: () => void;
}) {
  const palette = ['#0a0a0a', '#1f1f23', '#1a1f2e', '#2a1a1a', '#1a2a1a', '#ffffff'];
  return (
    <div
      style={{
        position: 'absolute',
        top: 52,
        right: 12,
        width: 240,
        background: '#16181c',
        border: '1px solid #2a2d33',
        borderRadius: 10,
        padding: 12,
        boxShadow: '0 8px 24px rgba(0,0,0,0.4)',
        zIndex: 10,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>Canvas</div>
        <button
          type="button"
          onClick={onClose}
          style={{
            background: 'transparent',
            border: 'none',
            color: '#888',
            cursor: 'pointer',
            fontSize: 14,
          }}
          aria-label="Close"
        >
          ✕
        </button>
      </div>

      <div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#aaa', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={prefs.transparent}
            onChange={(e) => onChange({ ...prefs, transparent: e.target.checked })}
          />
          Transparent background
        </label>
      </div>

      <div style={{ opacity: prefs.transparent ? 0.4 : 1 }}>
        <div style={{ fontSize: 12, color: '#aaa', marginBottom: 6 }}>Background color</div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          {palette.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => onChange({ ...prefs, background: c })}
              disabled={prefs.transparent}
              style={{
                width: 22,
                height: 22,
                borderRadius: 4,
                background: c,
                border:
                  prefs.background === c
                    ? '2px solid #3b82f6'
                    : '1px solid #2a2d33',
                cursor: prefs.transparent ? 'not-allowed' : 'pointer',
                padding: 0,
              }}
              aria-label={`Background ${c}`}
              title={c}
            />
          ))}
          <input
            type="color"
            value={prefs.background.startsWith('#') ? prefs.background : '#0a0a0a'}
            onChange={(e) => onChange({ ...prefs, background: e.target.value })}
            disabled={prefs.transparent}
            style={{
              width: 24,
              height: 24,
              border: '1px solid #2a2d33',
              borderRadius: 4,
              background: 'transparent',
              cursor: prefs.transparent ? 'not-allowed' : 'pointer',
              padding: 0,
            }}
            title="Custom color"
          />
        </div>
      </div>

      <div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#aaa', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={prefs.showControls}
            onChange={(e) => onChange({ ...prefs, showControls: e.target.checked })}
          />
          Show expression / motion controls
        </label>
      </div>

      <div style={{ borderTop: '1px solid #2a2d33', paddingTop: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 12, color: '#aaa' }}>Model</span>
          <button
            type="button"
            onClick={() => onChange({ ...prefs, scaleMultiplier: 1, offsetX: 0, offsetY: 0 })}
            style={{
              background: 'transparent',
              color: '#888',
              border: '1px solid #2a2d33',
              borderRadius: 4,
              padding: '2px 8px',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            Reset
          </button>
        </div>
        <SliderRow
          label="Zoom"
          value={prefs.scaleMultiplier}
          min={0.3} max={3} step={0.05}
          fmt={(v) => `${(v * 100).toFixed(0)}%`}
          onChange={(v) => onChange({ ...prefs, scaleMultiplier: v })}
        />
        <SliderRow
          label="X offset"
          value={prefs.offsetX}
          min={-400} max={400} step={5}
          fmt={(v) => `${Math.round(v)}px`}
          onChange={(v) => onChange({ ...prefs, offsetX: v })}
        />
        <SliderRow
          label="Y offset"
          value={prefs.offsetY}
          min={-400} max={400} step={5}
          fmt={(v) => `${Math.round(v)}px`}
          onChange={(v) => onChange({ ...prefs, offsetY: v })}
        />
      </div>
    </div>
  );
}

function SliderRow({
  label,
  value,
  min,
  max,
  step,
  fmt,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  fmt: (v: number) => string;
  onChange: (v: number) => void;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#888' }}>
        <span>{label}</span>
        <span style={{ fontFamily: 'monospace' }}>{fmt(value)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        style={{ width: '100%', accentColor: '#3b82f6' }}
      />
    </div>
  );
}

function fmtTime(ts: number): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}
