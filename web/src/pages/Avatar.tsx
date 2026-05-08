import { useState, useCallback, useRef, useEffect } from 'react';
import Live2DViewer, { type Live2DViewerHandle, type ModelActions } from '../components/avatar/Live2DViewer';
import AvatarControls from '../components/avatar/AvatarControls';
import { useAvatarSocket, type LipSyncDataProto } from '../components/avatar/useAvatarSocket';

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
}

const HISTORY_KEY = 'companion.chatHistory.v1';
const HISTORY_LIMIT = 200; // keep last N turns

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
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUnlockedRef = useRef(false);
  const viewerRef = useRef<Live2DViewerHandle>(null);
  const historyRef = useRef<HTMLDivElement>(null);

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/ws/avatar`;

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
      const mime = format === 'mp3' ? 'mpeg' : format;
      const audioBlob = new Blob(
        [Uint8Array.from(atob(audioBase64), (c) => c.charCodeAt(0))],
        { type: `audio/${mime}` }
      );
      const audioUrl = URL.createObjectURL(audioBlob);
      const audio = new Audio(audioUrl);
      audioRef.current = audio;
      audio.onended = () => {
        setIsPlaying(false);
        setPendingAudio(null);
        URL.revokeObjectURL(audioUrl);
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
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      if (!resp.ok) {
        console.error('Chat send failed:', resp.status, await resp.text());
        appendTurn({
          role: 'assistant',
          text: `[error: ${resp.status} ${resp.statusText}]`,
          ts: Date.now(),
        });
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
    return () => {
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
    };
  }, []);

  return (
    <div style={{ display: 'flex', flexDirection: 'row', height: '100%', gap: 12, padding: 12 }}>
      {/* Left column: avatar + controls */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
        <div
          style={{
            flex: 1,
            position: 'relative',
            borderRadius: 12,
            overflow: 'hidden',
            background: '#0a0a0a',
            minHeight: 0,
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
        </div>

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
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: isUser ? 'flex-end' : 'flex-start',
        gap: 2,
      }}
    >
      <div
        style={{
          fontSize: 10,
          color: '#666',
          padding: '0 4px',
        }}
      >
        {isUser ? 'you' : 'asuna'} · {fmtTime(turn.ts)}
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
