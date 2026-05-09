import { useState, useCallback, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import Live2DViewer, { type Live2DViewerHandle, type ModelActions, type ModelParameter } from '../components/avatar/Live2DViewer';
import AvatarControls from '../components/avatar/AvatarControls';
import {
  useAvatarSocket,
  type LipSyncDataProto,
  type DebugFrame,
} from '../components/avatar/useAvatarSocket';
import { HTTP_BASE, WS_BASE } from '../lib/apiBase';
import { nativeAudioAvailable, playAudioNative, stopAudioNative } from '../lib/nativeAudio';
import { openExternal } from '../lib/tauriShell';
import {
  getPetGeometry,
  setPetPosition,
  getPetMonitor,
  loadPetPosition,
  savePetPosition,
  computeSnap,
  startDraggingPet,
} from '../lib/petWindow';
import {
  fetchInstalledModels,
  getUserModelChoice,
  type InstalledModel,
} from '../lib/models';
import { fetchCharacters } from '../lib/characters';
import { startWebcamTracking, stopWebcamTracking } from '../lib/webcamTracker';

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
  /** Model rotation in degrees, around its visual center. */
  rotation: number;
  /** Mirror the model horizontally (left ↔ right). */
  mirrorX: boolean;
  /** Optional data-URL background image (overlays the solid color). */
  bgImageUrl: string | null;
  /** Background image opacity (0..1). */
  bgImageOpacity: number;
  /** Background image fit mode. */
  bgImageFit: 'cover' | 'contain' | 'fill';
  /** When true, the avatar plays a random Idle-group motion every
   *  `idleMotionSecs` seconds while not currently speaking. */
  idleMotion: boolean;
  /** Seconds between idle motions. Long enough that the avatar looks
   *  "alive" without spamming animations. */
  idleMotionSecs: number;
  /** Model gaze follows the mouse cursor over the canvas. */
  eyeTracking: boolean;
  /**
   * Webcam-driven gaze tracking. When enabled, requests webcam
   * permission and uses frame-difference motion detection (no ML
   * model download) to estimate where the user is moving and steer
   * the avatar's gaze toward it. Wins over mouse-based eyeTracking
   * when both are on.
   */
  webcamTracking: boolean;
}
const PREFS_KEY = 'companion.avatarPrefs.v1';
const DEFAULT_PREFS: CanvasPrefs = {
  background: '#0a0a0a',
  transparent: false,
  showControls: false, // hidden by default per user request
  scaleMultiplier: 1,
  offsetX: 0,
  offsetY: 0,
  rotation: 0,
  mirrorX: false,
  bgImageUrl: null,
  bgImageOpacity: 1,
  bgImageFit: 'cover',
  idleMotion: false,
  idleMotionSecs: 12,
  eyeTracking: false,
  webcamTracking: false,
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

// Audio playback. We use a single hidden <video> element as the
// renderer because:
// - Chromium gives <video> the strongest "media playback" treatment in
//   its internal audio session classification.
// - Per-app audio mixers (Razer Synapse / THX Spatial / Nahimic / NVIDIA
//   Broadcast) tend to recognize <video>-driven streams as media even
//   when the host process is a custom Tauri app they don't have a
//   profile for. Web Audio API and `<audio>` elements are treated more
//   ambiguously and often get communications-style DSP.
// - We still get per-byte fidelity: the WAV is decoded by the same
//   media stack as YouTube videos.
let sharedVideo: HTMLVideoElement | null = null;
function getRenderer(): HTMLVideoElement {
  if (sharedVideo) return sharedVideo;
  const v = document.createElement('video');
  v.muted = false;
  v.controls = false;
  v.playsInline = true;
  v.style.position = 'fixed';
  v.style.left = '-9999px';
  v.style.width = '1px';
  v.style.height = '1px';
  v.style.opacity = '0';
  v.style.pointerEvents = 'none';
  document.body.appendChild(v);
  sharedVideo = v;
  return v;
}

interface PlaybackHandle {
  duration: number;
  stop: () => void;
}

async function decodeAndPlay(
  bytes: ArrayBuffer,
  onEnded: () => void,
): Promise<PlaybackHandle> {
  const renderer = getRenderer();
  // Stop any in-flight playback and free the previous URL.
  try { renderer.pause(); } catch { /* ignore */ }
  if (renderer.src) {
    try { URL.revokeObjectURL(renderer.src); } catch { /* ignore */ }
    renderer.removeAttribute('src');
    renderer.load();
  }

  const blob = new Blob([bytes], { type: 'audio/wav' });
  const url = URL.createObjectURL(blob);
  renderer.src = url;

  const cleanup = () => {
    renderer.removeEventListener('ended', endedHandler);
    renderer.removeEventListener('error', errorHandler);
    try { URL.revokeObjectURL(url); } catch { /* ignore */ }
  };
  const endedHandler = () => {
    cleanup();
    onEnded();
  };
  const errorHandler = () => {
    cleanup();
    onEnded();
  };
  renderer.addEventListener('ended', endedHandler);
  renderer.addEventListener('error', errorHandler);

  await renderer.play();
  return {
    duration: Number.isFinite(renderer.duration) ? renderer.duration : 0,
    stop: () => {
      try { renderer.pause(); } catch { /* ignore */ }
      cleanup();
    },
  };
}

// True when this window is the transparent always-on-top "desktop pet"
// overlay (Tauri opens it with `/avatar?overlay=1`). Overlay windows
// must NOT manage chat history: they're a second WebView2 process that
// also subscribes to /ws/avatar, and if both windows append assistant
// turns + race to write localStorage, the overlay's stale state can
// erase user turns it never received (the user types into the main
// window, not the overlay). Detect once at module scope so it's stable.
const IS_OVERLAY =
  typeof window !== 'undefined' &&
  new URLSearchParams(window.location.search).has('overlay');


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
  const [history, setHistory] = useState<ChatTurn[]>(() => (IS_OVERLAY ? [] : loadHistory()));
  const [prefs, setPrefs] = useState<CanvasPrefs>(() => loadPrefs());
  const [showSettings, setShowSettings] = useState<boolean>(false);
  const audioUnlockedRef = useRef(false);
  const playbackRef = useRef<PlaybackHandle | null>(null);
  // Set when /api/chat returns a reply we haven't seen on the WS yet;
  // cleared by the WS onText handler when it delivers the same turn.
  // Used by handleSendChat's fallback to know whether it should
  // re-append after a grace period.
  const pendingHttpReplyRef = useRef<string | null>(null);
  // Set when the HTTP fallback path actually appended a turn (WS
  // missed the deadline). If the WS Text frame eventually does arrive
  // later, the onText handler REPLACES the just-added fallback turn
  // with the cleaner WS version instead of appending a duplicate.
  const httpFallbackFiredRef = useRef<string | null>(null);
  // Snapshot of the model's available motions, kept in sync via
  // onActionsReady. The Live2DViewer's idle-motion auto-play reads
  // through this ref so it picks up new motions when a model swaps
  // without re-rendering the viewer's effect deps.
  const modelMotionsRef = useRef<{ group: string; index: number }[] | null>(null);
  // True when the user's cursor is hovering the canvas. In overlay
  // (desktop pet) mode this gates the visibility of the chat bar and
  // corner buttons — the avatar floats chromeless on the desktop and
  // the controls fade in only on demand. No effect in main window.
  const [overlayHover, setOverlayHover] = useState(false);

  // User-selected Live2D model (overrides the server's WS ModelInfo).
  // Settings page writes localStorage; we listen for storage events
  // to react in real time + on the same window via custom events.
  const [installedModels, setInstalledModels] = useState<InstalledModel[]>([]);
  const [userModelId, setUserModelId] = useState<string | null>(() => getUserModelChoice());
  // Active character's model_id (from /api/characters). Takes
  // priority over the Settings-page manual model choice.
  const [activeCharacterModelId, setActiveCharacterModelId] = useState<string | null>(null);
  useEffect(() => {
    void fetchInstalledModels().then(setInstalledModels);
    const refreshActiveCharacter = () => {
      fetchCharacters()
        .then((file) => {
          const active = file.characters.find((c) => c.id === file.active_id);
          setActiveCharacterModelId(active?.model_id || null);
        })
        .catch(() => setActiveCharacterModelId(null));
    };
    refreshActiveCharacter();
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'companion.userModel.v1') {
        setUserModelId(getUserModelChoice());
      }
    };
    const onCustom = () => setUserModelId(getUserModelChoice());
    const onCharChange = () => refreshActiveCharacter();
    window.addEventListener('storage', onStorage);
    window.addEventListener('companion:userModel', onCustom);
    window.addEventListener('companion:characters', onCharChange);
    // Cross-window: the overlay avatar runs in a separate Tauri window
    // so the in-window 'companion:characters' event from the main window
    // can never reach it. BroadcastChannel does cross same-origin
    // contexts, so a character switch in the main window now triggers
    // a Live2D swap here too.
    let bc: BroadcastChannel | null = null;
    try {
      bc = new BroadcastChannel('companion');
      bc.onmessage = (e) => {
        if (e.data?.kind === 'characters') refreshActiveCharacter();
      };
    } catch { /* unsupported in some legacy contexts */ }
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('companion:userModel', onCustom);
      window.removeEventListener('companion:characters', onCharChange);
      if (bc) bc.close();
    };
  }, []);
  // Live2D parameter overrides, keyed by parameter id (e.g.
  // "PARAM_ANGLE_X"). Live2DViewer continuously re-applies these so
  // the model's motion system can't overwrite them. Persisted per
  // active model id so swapping models doesn't carry stale overrides.
  const [paramOverrides, setParamOverrides] = useState<Record<string, number>>({});
  const [availableParams, setAvailableParams] = useState<ModelParameter[]>([]);
  // Read params from the loaded model after onActionsReady fires
  // (proxies "model finished loading"). Slight delay is intentional
  // so the model's first motion has populated current values.
  useEffect(() => {
    if (modelActions.expressions.length === 0 && modelActions.motions.length === 0) {
      return;
    }
    const id = setTimeout(() => {
      const params = viewerRef.current?.getParameters() ?? [];
      setAvailableParams(params);
    }, 600);
    return () => clearTimeout(id);
  }, [modelActions]);
  // Storage: per-effective-model so overrides don't leak across
  // model swaps OR character changes. Character's model_id takes
  // priority — same precedence as effectiveModelInfo above.
  const paramStorageKey = (() => {
    const mid = activeCharacterModelId ?? userModelId ?? 'server-default';
    return `companion.params.${mid}.v1`;
  })();
  // Hydrate overrides on model swap.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(paramStorageKey);
      setParamOverrides(raw ? JSON.parse(raw) : {});
    } catch {
      setParamOverrides({});
    }
  }, [paramStorageKey]);
  // Persist on every change.
  useEffect(() => {
    try {
      if (Object.keys(paramOverrides).length === 0) {
        localStorage.removeItem(paramStorageKey);
      } else {
        localStorage.setItem(paramStorageKey, JSON.stringify(paramOverrides));
      }
    } catch { /* non-fatal */ }
  }, [paramOverrides, paramStorageKey]);

  // Effective model precedence (highest wins):
  //   1. Active character's model_id (from /api/characters).
  //   2. User's manual pick in Settings (companion.userModel.v1).
  //   3. Server's default that arrives via WS ModelInfo.
  const effectiveModelInfo: ModelInfo | null = (() => {
    if (!modelInfo) return null;
    const candidateId = activeCharacterModelId ?? userModelId;
    if (!candidateId) return modelInfo;
    const picked = installedModels.find((m) => m.id === candidateId);
    if (!picked) return modelInfo;
    return { ...modelInfo, modelUrl: picked.modelUrl };
  })();

  // Webcam face/motion tracking — drive model.focus() from the
  // user's webcam. Frame-difference based, no ML library bundled.
  // Requests camera permission on enable; releases on disable or
  // unmount. Suppressed silently if the browser denies access.
  const [webcamError, setWebcamError] = useState<string | null>(null);
  useEffect(() => {
    if (!prefs.webcamTracking) return;
    let cancelled = false;
    setWebcamError(null);
    startWebcamTracking((focus) => {
      if (cancelled) return;
      // pixi-live2d-display's model.focus expects window/page-pixel
      // coords. Map (-1, 1) → a virtual point centered at the canvas.
      const canvas = document.querySelector('canvas');
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const px = rect.left + rect.width * (focus.x * 0.5 + 0.5);
      const py = rect.top + rect.height * (focus.y * 0.5 + 0.5);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const model = (window as any).__live2dModel;
      try {
        model?.focus?.(px, py);
      } catch { /* model unloaded */ }
    }).catch((e) => {
      console.warn('webcam tracking failed:', e);
      setWebcamError(String(e?.message ?? e));
      // Auto-disable so the toggle reflects the actual state.
      setPrefs((p) => ({ ...p, webcamTracking: false }));
    });
    return () => { cancelled = true; stopWebcamTracking(); };
  }, [prefs.webcamTracking]);

  // Pet window drag — fire Tauri's native OS-level drag from pointerdown.
  //
  // The OS-native drag is way smoother than per-frame set_position
  // (no IPC hops between move events; the OS handles the move loop).
  // The earlier failure mode was the `data-tauri-drag-region=""` attr
  // on the canvas wrapper triggering Tauri's runtime drag at the same
  // time as our JS handler — race condition. With that attr removed,
  // we can call start_dragging directly here without conflict.
  //
  // We listen at document with capture:true so pixi-live2d-display's
  // interaction layer can't swallow the pointerdown before we fire.
  useEffect(() => {
    if (!IS_OVERLAY) return;
    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0 || e.pointerType === 'touch') return;
      // Walk up from the target; if any ancestor opts OUT
      // (data-tauri-drag-region="false"), skip the drag — needed for
      // the chat input, settings popover, corner buttons, etc.
      let node: HTMLElement | null = e.target as HTMLElement;
      while (node) {
        const attr = node.getAttribute?.('data-tauri-drag-region');
        if (attr === 'false') return;
        node = node.parentElement;
      }
      // Don't await — start_dragging must happen synchronously inside
      // the native mouse event for Windows to honor the drag.
      void startDraggingPet();
    };
    document.addEventListener('pointerdown', onPointerDown, { capture: true });
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, { capture: true });
    };
  }, []);

  // Pet window placement: restore on mount + persist + snap-to-edge.
  //
  // Tauri's data-tauri-drag-region implements the drag at OS level,
  // so we don't get JS dragstart/dragend events. Instead, poll the
  // window's outer position; when it stops changing for `SETTLE_MS`
  // we treat the drag as finished, save the position, and snap to a
  // monitor edge if close enough. Polling is cheap (single Tauri
  // invoke every 250ms while the overlay is open) and avoids needing
  // platform-specific drag-event listeners.
  useEffect(() => {
    if (!IS_OVERLAY) return;
    let cancelled = false;
    let lastX = -Infinity;
    let lastY = -Infinity;
    let stableSince = 0;
    // Encoded "last position we already snapped/saved" — null means
    // no save has happened yet for this stable streak.
    let savedAtKey: string | null = null;
    const POLL_MS = 250;
    const SETTLE_MS = 700;
    const SNAP_THRESHOLD = 30;

    const restore = async () => {
      const saved = loadPetPosition();
      if (!saved) return;
      await setPetPosition(saved.x, saved.y);
    };
    void restore();

    const tick = async () => {
      if (cancelled) return;
      const geom = await getPetGeometry();
      if (!geom) return;
      const now = Date.now();
      if (geom.x !== lastX || geom.y !== lastY) {
        // Window is moving — reset settle timer + snap memo.
        lastX = geom.x;
        lastY = geom.y;
        stableSince = now;
        savedAtKey = null;
        return;
      }
      const key = `${geom.x},${geom.y}`;
      if (now - stableSince < SETTLE_MS || savedAtKey === key) return;
      // Position is stable AND we haven't yet handled this stop.
      const monitor = await getPetMonitor();
      if (!monitor) {
        savePetPosition(geom.x, geom.y);
        savedAtKey = key;
        return;
      }
      const snap = computeSnap(geom, monitor, SNAP_THRESHOLD);
      if (snap.snapped && (snap.x !== geom.x || snap.y !== geom.y)) {
        await setPetPosition(snap.x, snap.y);
        savePetPosition(snap.x, snap.y);
        savedAtKey = `${snap.x},${snap.y}`;
        lastX = snap.x;
        lastY = snap.y;
        stableSince = Date.now();
      } else {
        savePetPosition(geom.x, geom.y);
        savedAtKey = key;
      }
    };
    const id = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  // Sentence-chunked playback queue. The companion server can send
  // several Audio frames per turn (one per sentence). Buffer them
  // here and drain sequentially via `drainQueue` so back-to-back
  // chunks play continuously without overlap or audible gaps.
  const audioQueueRef = useRef<ArrayBuffer[]>([]);
  const currentTurnRef = useRef<string | null>(null);
  const drainingRef = useRef(false);
  const viewerRef = useRef<Live2DViewerHandle>(null);
  const historyRef = useRef<HTMLDivElement>(null);

  useEffect(() => savePrefs(prefs), [prefs]);

  /** Stop any in-flight audio AND clear the queue. Called when a new
   *  turn arrives or on unmount. */
  const stopCurrentAudio = useCallback(() => {
    if (playbackRef.current) {
      playbackRef.current.stop();
      playbackRef.current = null;
    }
    audioQueueRef.current = [];
    drainingRef.current = false;
  }, []);

  /** Pull the next chunk from the queue and play it. When it ends,
   *  recurse into the next chunk if there is one. */
  const drainQueue = useCallback(() => {
    if (drainingRef.current) return;
    const next = audioQueueRef.current.shift();
    if (!next) return;
    drainingRef.current = true;
    decodeAndPlay(next, () => {
      drainingRef.current = false;
      if (audioQueueRef.current.length > 0) {
        drainQueue();
      } else {
        setIsPlaying(false);
        playbackRef.current = null;
      }
    })
      .then((handle) => {
        playbackRef.current = handle;
        audioUnlockedRef.current = true;
        setAudioError(null);
        setPendingAudio(null);
      })
      .catch((err: Error) => {
        console.error('audio decode/play failed:', err);
        drainingRef.current = false;
        if (err.name === 'NotAllowedError' || /suspend/i.test(err.message)) {
          setAudioError('Browser blocked audio. Click "Play" to enable.');
          // Re-prepend the chunk so the manual-play resume continues
          // from where we got blocked.
          audioQueueRef.current.unshift(next);
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const stub = { play: () => { drainQueue(); return Promise.resolve(); } } as any;
          setPendingAudio(stub);
        } else {
          setAudioError(`Audio error: ${err.message}`);
          if (audioQueueRef.current.length > 0) drainQueue();
          else setIsPlaying(false);
        }
      });
  }, []);

  /** Push base64 WAV bytes onto the WebView playback queue. Used as
   *  a fallback when native audio is unavailable or fails. */
  const enqueueWebviewAudio = useCallback((audioBase64: string) => {
    const bytes = Uint8Array.from(atob(audioBase64), (c) => c.charCodeAt(0));
    audioQueueRef.current.push(bytes.buffer);
    drainQueue();
  }, [drainQueue]);

  const wsUrl = `${WS_BASE}/ws/avatar`;

  // Auto-scroll history to bottom whenever it grows.
  useEffect(() => {
    const el = historyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [history]);

  // Persist history on every change. Overlay window never writes —
  // it isn't authoritative for the chat panel; main window owns it.
  useEffect(() => {
    if (IS_OVERLAY) return;
    saveHistory(history);
  }, [history]);

  const appendTurn = useCallback((turn: ChatTurn) => {
    if (IS_OVERLAY) return; // overlay isn't authoritative for chat history
    setHistory((prev) => {
      const next = [...prev, turn].slice(-HISTORY_LIMIT);
      console.log(
        `[chat] +${turn.role} "${turn.text.slice(0, 40)}" → ${next.length} turns (`
        + `${next.filter(t => t.role === 'user').length}u/${next.filter(t => t.role === 'assistant').length}a)`
      );
      return next;
    });
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
    onAudio: (audioBase64, _format, _sampleRate, lipSync, turnId, seq, last) => {
      // New turn → flush both the WebView queue AND the native sink.
      if (turnId && turnId !== currentTurnRef.current) {
        stopCurrentAudio();
        if (nativeAudioAvailable()) void stopAudioNative();
        currentTurnRef.current = turnId;
      }

      // Empty audio = server signaled `last` for a failed chunk.
      if (!audioBase64) {
        if (!drainingRef.current && audioQueueRef.current.length === 0) {
          setIsPlaying(false);
        }
        return;
      }

      setLipSyncData(lipSync);
      setIsPlaying(true);

      // Native path (Tauri only): rodio plays through the host
      // process's WASAPI session, which Windows classifies as
      // multimedia. Bypasses WebView2's "communications" DSP.
      if (nativeAudioAvailable()) {
        // eslint-disable-next-line no-console
        console.log('[audio] → rodio (native)', { turnId, seq, last, bytes: audioBase64.length });
        void playAudioNative(audioBase64, turnId, seq).then(() => {
          // eslint-disable-next-line no-console
          console.log('[audio] rodio invoke OK', { turnId, seq });
          // rodio's Sink doesn't emit a JS-visible "ended" event; we
          // optimistically drop "speaking" state when the last chunk
          // has been queued. The Sink will play through its queue.
          if (last) {
            // Give the queue a moment to finish. Cheap heuristic
            // (we don't have decoded duration here) — frontend "speaking"
            // state is just visual; the audio plays correctly either way.
            setTimeout(() => setIsPlaying(false), 500);
          }
        }).catch((err) => {
          console.error('[audio] rodio FAILED, falling back to webview:', err);
          // Fall through to the <video> path
          enqueueWebviewAudio(audioBase64);
        });
        return;
      }

      // Browser path: existing <video>/Web Audio queue
      // eslint-disable-next-line no-console
      console.log('[audio] → webview <video> path (no Tauri)', { turnId, seq, last });
      enqueueWebviewAudio(audioBase64);
    },
    onUserMessage: (content) => {
      // The server echoes the user's typed message on the broadcast
      // channel so the main window records it in chat history,
      // regardless of which window typed it (the overlay can have
      // its own compact chat input but isn't authoritative for state).
      if (IS_OVERLAY) return; // overlay never writes history
      // Dedupe inside setHistory so we use the LATEST state — the
      // outer-closure `history` would be stale.
      setHistory((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'user' && last.text === content) {
          return prev; // optimistic append already landed (main typed)
        }
        const turn: ChatTurn = { role: 'user', text: content, ts: Date.now() };
        return [...prev, turn].slice(-HISTORY_LIMIT);
      });
    },
    onText: (content) => {
      setSubtitle(content);
      // WS delivered the assistant turn — clear the pending HTTP
      // fallback so handleSendChat's setTimeout doesn't double-append.
      pendingHttpReplyRef.current = null;
      // If the HTTP fallback already added a turn for THIS reply,
      // replace it with the WS version (cleaner, comes with audio
      // sync) instead of appending a duplicate.
      const fallbackText = httpFallbackFiredRef.current;
      httpFallbackFiredRef.current = null;
      if (fallbackText !== null) {
        setHistory((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          if (last.role !== 'assistant' || last.text !== fallbackText) {
            // Last turn isn't our fallback — fall through to append.
            return [
              ...prev,
              { role: 'assistant', text: content, ts: Date.now() } as ChatTurn,
            ].slice(-HISTORY_LIMIT);
          }
          // Replace the fallback turn with the WS-delivered one.
          const next = prev.slice(0, -1);
          next.push({ role: 'assistant', text: content, ts: Date.now() });
          console.log(
            `[chat] replaced fallback "${fallbackText.slice(0, 40)}" → "${content.slice(0, 40)}"`
          );
          return next;
        });
        return;
      }
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

    // Browser autoplay pre-warm: poke the shared <video> element
    // during this user gesture so the eventual decodeAndPlay (10–20s
    // later) doesn't get blocked by autoplay policy. play() on an
    // empty <video> rejects gracefully; what matters is that we've
    // touched the element under a gesture.
    if (!audioUnlockedRef.current) {
      try {
        const v = getRenderer();
        v.play().catch(() => { /* expected when no src; gesture still counts */ });
        audioUnlockedRef.current = true;
      } catch { /* ignore */ }
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
      } else {
        // Append the assistant reply from the HTTP response IF the WS
        // didn't already deliver it. The WS Text frame is preferred —
        // it carries the subagent-cleaned text + arrives in sync with
        // audio — but if the WS dropped during a long agent loop (we
        // saw a real case: 2-minute wait on "how to deal with sleep
        // disruption"), the user would otherwise see no reply at all.
        try {
          const body: { reply?: string } = await resp.json();
          if (body.reply) {
            const candidate = body.reply;
            // Bump the turn counter; the WS onText handler clears
            // pendingHttpReplyRef.current when it receives the next
            // Text frame for this turn. If the ref is still set after
            // 8s, the WS missed it and we fall back to the raw text.
            // 8s is the longest wait we've seen between /api/chat
            // returning and the corresponding WS Text frame arriving.
            pendingHttpReplyRef.current = candidate;
            setTimeout(() => {
              if (pendingHttpReplyRef.current !== candidate) {
                return; // WS delivered first; nothing to do
              }
              pendingHttpReplyRef.current = null;
              httpFallbackFiredRef.current = candidate;
              console.log('[chat] +assistant (HTTP fallback — WS missed)');
              appendTurn({ role: 'assistant', text: candidate, ts: Date.now() });
            }, 8000);
          }
        } catch { /* not JSON or already consumed; non-fatal */ }
      }
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
    <div
      style={{
        display: 'flex',
        flexDirection: 'row',
        height: '100%',
        gap: IS_OVERLAY ? 0 : 12,
        padding: IS_OVERLAY ? 0 : 12,
      }}
    >
      {/* Left column: avatar + (collapsible) controls */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: IS_OVERLAY ? 0 : 12, minWidth: 0 }}>
        <div
          // Drag is handled by the JS-level pointer-capture handler in
          // useEffect above (see "Pet window drag"). We deliberately
          // don't set `data-tauri-drag-region=""` here — it would cause
          // Tauri's runtime to start its own OS-level drag on the same
          // mousedown, which fails on transparent WebView2 windows and
          // races with our manual set_position calls.
          onMouseEnter={IS_OVERLAY ? () => setOverlayHover(true) : undefined}
          onMouseLeave={IS_OVERLAY ? () => setOverlayHover(false) : undefined}
          style={{
            flex: 1,
            position: 'relative',
            borderRadius: IS_OVERLAY ? 0 : 12,
            overflow: 'hidden',
            // In pet mode the canvas is always transparent (the user
            // wants the avatar floating on the desktop, not a window
            // with a colored background). In main mode the user picks.
            background: IS_OVERLAY
              ? 'transparent'
              : (prefs.transparent ? 'transparent' : canvasBg),
            minHeight: 0,
            // Subtle checker pattern when transparent so the user can
            // see the canvas extents. Suppressed in overlay mode.
            backgroundImage: !IS_OVERLAY && prefs.transparent
              ? 'linear-gradient(45deg, #1a1a1a 25%, transparent 25%), linear-gradient(-45deg, #1a1a1a 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #1a1a1a 75%), linear-gradient(-45deg, transparent 75%, #1a1a1a 75%)'
              : undefined,
            backgroundSize: !IS_OVERLAY && prefs.transparent ? '20px 20px' : undefined,
            backgroundPosition: !IS_OVERLAY && prefs.transparent ? '0 0, 0 10px, 10px -10px, -10px 0px' : undefined,
          }}
        >
          {/* User-supplied background image, painted ABOVE the solid
              color and BELOW the avatar. data-tauri-drag-region opted
              out so click-and-drag still picks up the parent (only
              the avatar pixels are draggable in overlay mode). */}
          {prefs.bgImageUrl && !IS_OVERLAY && !prefs.transparent && (
            <img
              src={prefs.bgImageUrl}
              alt=""
              style={{
                position: 'absolute',
                inset: 0,
                width: '100%',
                height: '100%',
                objectFit: prefs.bgImageFit,
                opacity: prefs.bgImageOpacity,
                pointerEvents: 'none',
                userSelect: 'none',
              }}
            />
          )}
          {effectiveModelInfo ? (
            <Live2DViewer
              ref={viewerRef}
              modelUrl={effectiveModelInfo.modelUrl}
              scale={effectiveModelInfo.scale}
              anchor={effectiveModelInfo.anchor}
              defaultExpression={effectiveModelInfo.defaultExpression}
              lipSyncData={lipSyncData}
              isPlaying={isPlaying}
              onActionsReady={(a) => {
                setModelActions(a);
                modelMotionsRef.current = a.motions;
              }}
              scaleMultiplier={prefs.scaleMultiplier}
              offsetX={prefs.offsetX}
              offsetY={prefs.offsetY}
              rotation={prefs.rotation}
              mirrorX={prefs.mirrorX}
              idleMotion={prefs.idleMotion}
              idleMotionIntervalMs={prefs.idleMotionSecs * 1000}
              eyeTracking={prefs.eyeTracking}
              motionsRef={modelMotionsRef}
              dragRegion={IS_OVERLAY}
              dragToTranslate={!IS_OVERLAY}
              onTranslate={(dx, dy) =>
                setPrefs((p) => ({
                  ...p,
                  offsetX: p.offsetX + dx,
                  offsetY: p.offsetY + dy,
                }))
              }
              parameterOverrides={paramOverrides}
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
                // pendingAudio.play() now resolves a Web Audio replay
                // promise (set up in decodeAndPlay's catch above);
                // either it succeeds and clears the banner, or it
                // updates audioError with the new failure reason.
                Promise.resolve(pendingAudio.play()).catch((e) =>
                  setAudioError(`Still blocked: ${e?.message ?? e}`)
                );
              }}
            >
              ▶  {audioError ?? 'Click to play audio'}
            </div>
          )}
          {subtitle && (
            <div
              // Opt out of the parent's drag region so users can
              // select / scroll subtitle text without picking up the
              // window in pet mode.
              {...{ 'data-tauri-drag-region': 'false' } as Record<string, string>}
              style={{
                position: 'absolute',
                // In pet mode, position ABOVE the chat bar (which sits
                // at bottom: 12 with ~50px height). Without this, the
                // chat bar covered the subtitle and the user reported
                // "in pet mode I also want subtitles" — they were
                // already firing, just hidden behind the input.
                bottom: IS_OVERLAY ? 76 : 16,
                left: '50%',
                transform: 'translateX(-50%)',
                maxWidth: IS_OVERLAY ? '92%' : '80%',
                background: 'rgba(0, 0, 0, 0.78)',
                color: '#fff',
                padding: '8px 14px',
                borderRadius: 10,
                fontSize: IS_OVERLAY ? 13 : 14,
                lineHeight: 1.4,
                backdropFilter: 'blur(6px)',
                WebkitBackdropFilter: 'blur(6px)',
                boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
                whiteSpace: 'pre-wrap',
                pointerEvents: 'none',
                // Always-visible in pet mode regardless of hover state.
                zIndex: 5,
              }}
            >
              {subtitle}
            </div>
          )}
          {/* Floating top-right settings/toggle row.
              In overlay (pet) mode this fades in only on hover so the
              desktop pet looks like just an avatar by default. */}
          <div
            style={{
              position: 'absolute',
              top: 12,
              right: 12,
              display: 'flex',
              gap: 6,
              opacity: IS_OVERLAY ? (overlayHover ? 1 : 0) : 1,
              transition: 'opacity 200ms ease',
              pointerEvents: IS_OVERLAY && !overlayHover ? 'none' : 'auto',
            }}
            // Buttons sit on top of the parent's drag region; opting
            // out so clicks don't accidentally start a window drag.
            {...{ 'data-tauri-drag-region': 'false' } as Record<string, string>}
          >
            <CanvasButton
              title="Canvas settings"
              onClick={() => setShowSettings((s) => !s)}
              active={showSettings}
            >
              ⚙
            </CanvasButton>
            {/* Expression / motion controls hidden in pet (overlay)
                mode — they stack visually with the chat box at the
                bottom and the user has to click them anyway. Pet mode
                stays minimal: avatar + chat + close. */}
            {!IS_OVERLAY && (
              <CanvasButton
                title={prefs.showControls ? 'Hide expressions / motions' : 'Show expressions / motions'}
                onClick={() => setPrefs((p) => ({ ...p, showControls: !p.showControls }))}
                active={prefs.showControls}
              >
                {prefs.showControls ? '✕' : '☰'}
              </CanvasButton>
            )}
            {IS_OVERLAY && (
              <CanvasButton
                title="Hide desktop pet"
                onClick={() => {
                  // Tell the main window's PET_VISIBLE_KEY listener
                  // (App.tsx Nav) to flip OFF, then call hide_avatar_window.
                  try {
                    localStorage.setItem('companion.petVisible.v1', '0');
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    const inv = ((window as any).__TAURI_INTERNALS__?.invoke
                      ?? (window as any).__TAURI__?.invoke) as
                      | ((cmd: string) => Promise<unknown>)
                      | undefined;
                    inv?.('hide_avatar_window');
                  } catch { /* non-fatal */ }
                }}
              >
                ✕
              </CanvasButton>
            )}
          </div>
          {showSettings && (
            <CanvasSettingsPopover
              prefs={prefs}
              onChange={setPrefs}
              onClose={() => setShowSettings(false)}
              availableParams={availableParams}
              paramOverrides={paramOverrides}
              onParamChange={(id, value) =>
                setParamOverrides((prev) => ({ ...prev, [id]: value }))
              }
              onParamReset={(id) =>
                setParamOverrides((prev) => {
                  const next = { ...prev };
                  delete next[id];
                  return next;
                })
              }
              onParamResetAll={() => setParamOverrides({})}
            />
          )}
        </div>

        {prefs.showControls && !IS_OVERLAY && (
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

      {/* Right column: chat history + input.
          Overlay window (Tauri's transparent always-on-top "desktop pet")
          renders ONLY the avatar — no chat panel. The main window is
          the authoritative chat surface; the overlay just animates the
          avatar based on broadcast WS frames. */}
      {!IS_OVERLAY && (
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
            contain: 'paint',
            overscrollBehavior: 'contain',
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
      )}

      {/* Compact chat bar — only in overlay (desktop pet) mode. The
          overlay window is transparent + frameless, so this is the
          ONLY way to talk to Asuna without opening the main window.
          Hidden by default so the pet looks like just an avatar
          floating on the desktop; fades in when the user hovers over
          the canvas. Anchored to the bottom edge with a translucent
          backdrop so the avatar above stays the visual focus. */}
      {IS_OVERLAY && (
        <form
          onMouseDown={(e) => e.stopPropagation()}
          onSubmit={(e) => {
            e.preventDefault();
            handleSendChat();
          }}
          style={{
            position: 'absolute',
            left: 12,
            right: 12,
            bottom: 12,
            display: 'flex',
            gap: 6,
            background: 'rgba(11, 13, 16, 0.78)',
            backdropFilter: 'blur(10px)',
            WebkitBackdropFilter: 'blur(10px)',
            padding: 8,
            borderRadius: 12,
            border: '1px solid rgba(58, 61, 67, 0.6)',
            // Keep the box visible at low opacity when idle so the user
            // can always find + click it. Brightens fully when the user
            // hovers anywhere on the avatar, types, or a reply is in-
            // flight. The `pointerEvents: auto` is unconditional —
            // previously the box was completely click-through when
            // hidden, which made it impossible to start a chat from a
            // cold state without first hovering the canvas.
            opacity: overlayHover || sending || chatInput.length > 0 ? 1 : 0.45,
            transition: 'opacity 180ms ease',
            pointerEvents: 'auto',
            // Soft drop shadow so the box stays legible against any
            // desktop background once the overlay is fully transparent.
            boxShadow: '0 4px 16px rgba(0, 0, 0, 0.35)',
          }}
          // Explicitly opt this subtree out of the parent's drag — clicks
          // on the input must focus, not start a window move.
          {...{ 'data-tauri-drag-region': 'false' } as Record<string, string>}
        >
          <input
            type="text"
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            placeholder={sending ? 'sending…' : 'Talk to your character…'}
            disabled={sending}
            // Stop pointerdown so the document-capture drag handler
            // doesn't fire `start_dragging` from focus clicks.
            onPointerDown={(e) => e.stopPropagation()}
            style={{
              flex: 1,
              background: 'rgba(11, 13, 16, 0.92)',
              color: '#fff',
              padding: '8px 12px',
              borderRadius: 8,
              border: '1px solid #2a2d33',
              fontSize: 13,
              outline: 'none',
            }}
          />
          <button
            type="submit"
            disabled={!chatInput.trim() || sending}
            onPointerDown={(e) => e.stopPropagation()}
            style={{
              padding: '6px 12px',
              background: chatInput.trim() && !sending ? '#3b82f6' : '#1f2937',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              fontSize: 13,
              cursor: chatInput.trim() && !sending ? 'pointer' : 'not-allowed',
              minWidth: 36,
            }}
          >
            ➤
          </button>
        </form>
      )}
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
        // Isolate each chat bubble's paints — long histories with
        // markdown were the worst offender for scroll lag.
        contain: 'content',
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
          wordBreak: 'break-word',
        }}
      >
        {/* User messages stay plain text — they typed it, no markdown
            interpretation needed. Assistant messages render through
            react-markdown so zeroclaw's bold / lists / code / headings
            display properly instead of `**bold**` showing literal
            asterisks. GFM extension covers tables and strikethrough. */}
        {isUser ? (
          <span style={{ whiteSpace: 'pre-wrap' }}>{turn.text}</span>
        ) : (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            // Inline-styled element renderers so the bubble's dark
            // background + small font are preserved. Without these,
            // react-markdown emits raw <p>, <ul>, <code> etc. with
            // browser defaults (white code backgrounds, big margins,
            // huge headings) that don't fit a 13px chat bubble.
            components={{
              p: ({ children }) => (
                <p style={{ margin: '0 0 6px 0', whiteSpace: 'pre-wrap' }}>{children}</p>
              ),
              ul: ({ children }) => (
                <ul style={{ margin: '4px 0', paddingLeft: 18 }}>{children}</ul>
              ),
              ol: ({ children }) => (
                <ol style={{ margin: '4px 0', paddingLeft: 18 }}>{children}</ol>
              ),
              li: ({ children }) => (
                <li style={{ margin: '2px 0' }}>{children}</li>
              ),
              h1: ({ children }) => (
                <div style={{ fontSize: 14, fontWeight: 600, margin: '6px 0 4px' }}>{children}</div>
              ),
              h2: ({ children }) => (
                <div style={{ fontSize: 13, fontWeight: 600, margin: '6px 0 4px' }}>{children}</div>
              ),
              h3: ({ children }) => (
                <div style={{ fontSize: 13, fontWeight: 600, margin: '4px 0 2px' }}>{children}</div>
              ),
              code: ({ children }) => (
                <code style={{
                  background: '#0d0e12',
                  padding: '1px 4px',
                  borderRadius: 3,
                  fontSize: 12,
                  fontFamily: 'ui-monospace, monospace',
                }}>{children}</code>
              ),
              pre: ({ children }) => (
                <pre style={{
                  background: '#0d0e12',
                  padding: 8,
                  borderRadius: 6,
                  fontSize: 11,
                  overflow: 'auto',
                  margin: '4px 0',
                }}>{children}</pre>
              ),
              a: ({ href, children }) => (
                <a
                  href={href}
                  onClick={(e) => {
                    e.preventDefault();
                    if (href) void openExternal(href);
                  }}
                  style={{ color: '#7aa9ff', textDecoration: 'underline', cursor: 'pointer' }}
                >{children}</a>
              ),
              strong: ({ children }) => (
                <strong style={{ fontWeight: 600 }}>{children}</strong>
              ),
              em: ({ children }) => (
                <em style={{ fontStyle: 'italic' }}>{children}</em>
              ),
              blockquote: ({ children }) => (
                <blockquote style={{
                  margin: '4px 0', paddingLeft: 8,
                  borderLeft: '2px solid #3b82f6', color: '#aaa',
                }}>{children}</blockquote>
              ),
              hr: () => <hr style={{ border: 'none', borderTop: '1px solid #2a2d33', margin: '6px 0' }} />,
            }}
          >
            {turn.text}
          </ReactMarkdown>
        )}
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
  availableParams,
  paramOverrides,
  onParamChange,
  onParamReset,
  onParamResetAll,
}: {
  prefs: CanvasPrefs;
  onChange: (next: CanvasPrefs) => void;
  onClose: () => void;
  availableParams: ModelParameter[];
  paramOverrides: Record<string, number>;
  onParamChange: (id: string, value: number) => void;
  onParamReset: (id: string) => void;
  onParamResetAll: () => void;
}) {
  const palette = ['#0a0a0a', '#1f1f23', '#1a1f2e', '#2a1a1a', '#1a2a1a', '#ffffff'];
  return (
    <div
      // The popover sits over the canvas; opt out of the parent's
      // drag region so settings clicks don't pick up the window.
      {...{ 'data-tauri-drag-region': 'false' } as Record<string, string>}
      style={{
        position: 'absolute',
        top: 52,
        right: 12,
        width: 280,
        maxHeight: 'calc(100% - 64px)',
        overflowY: 'auto',
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
          <span style={{ fontSize: 12, color: '#aaa' }}>Model transform</span>
          <button
            type="button"
            onClick={() => onChange({
              ...prefs,
              scaleMultiplier: 1,
              offsetX: 0,
              offsetY: 0,
              rotation: 0,
              mirrorX: false,
            })}
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
        <SliderRow
          label="Rotation"
          value={prefs.rotation}
          min={-180} max={180} step={1}
          fmt={(v) => `${Math.round(v)}°`}
          onChange={(v) => onChange({ ...prefs, rotation: v })}
        />
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#aaa', cursor: 'pointer', marginTop: 6 }}>
          <input
            type="checkbox"
            checked={prefs.mirrorX}
            onChange={(e) => onChange({ ...prefs, mirrorX: e.target.checked })}
          />
          Mirror horizontally
        </label>
      </div>

      {/* Background image (overrides solid color when set) */}
      <div style={{ borderTop: '1px solid #2a2d33', paddingTop: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 12, color: '#aaa' }}>Background image</span>
          {prefs.bgImageUrl && (
            <button
              type="button"
              onClick={() => onChange({ ...prefs, bgImageUrl: null })}
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
              Clear
            </button>
          )}
        </div>
        <input
          type="file"
          accept="image/*"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            const reader = new FileReader();
            reader.onload = () => {
              if (typeof reader.result === 'string') {
                onChange({ ...prefs, bgImageUrl: reader.result });
              }
            };
            reader.readAsDataURL(f);
          }}
          style={{ fontSize: 11, color: '#aaa' }}
        />
        {prefs.bgImageUrl && (
          <>
            <SliderRow
              label="Opacity"
              value={prefs.bgImageOpacity}
              min={0} max={1} step={0.05}
              fmt={(v) => `${Math.round(v * 100)}%`}
              onChange={(v) => onChange({ ...prefs, bgImageOpacity: v })}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#aaa', marginTop: 4 }}>
              <span style={{ minWidth: 70 }}>Fit</span>
              <select
                value={prefs.bgImageFit}
                onChange={(e) => onChange({ ...prefs, bgImageFit: e.target.value as CanvasPrefs['bgImageFit'] })}
                style={{
                  flex: 1,
                  background: '#0b0d10',
                  color: '#fff',
                  padding: '4px 6px',
                  borderRadius: 4,
                  border: '1px solid #2a2d33',
                  fontSize: 11,
                }}
              >
                <option value="cover">Cover (crop to fill)</option>
                <option value="contain">Contain (fit, may letterbox)</option>
                <option value="fill">Fill (stretch to fit)</option>
              </select>
            </div>
          </>
        )}
      </div>

      {/* Live2D parameters — manual sliders for every model param.
          Continuously re-applied by Live2DViewer so the sliders win
          against the model's own animation. Per-model storage so
          switching models clears prior overrides. */}
      <div style={{ borderTop: '1px solid #2a2d33', paddingTop: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 12, color: '#aaa' }}>
            Live2D parameters{availableParams.length > 0 ? ` (${availableParams.length})` : ''}
          </span>
          {Object.keys(paramOverrides).length > 0 && (
            <button
              type="button"
              onClick={onParamResetAll}
              title="Clear every parameter override"
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
              Reset all ({Object.keys(paramOverrides).length})
            </button>
          )}
        </div>
        {availableParams.length === 0 ? (
          <div style={{ fontSize: 11, color: '#666', lineHeight: 1.5 }}>
            Loading model parameters… (some models don't expose an
            introspectable param list).
          </div>
        ) : (
          <div style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
            maxHeight: 220,
            overflowY: 'auto',
            paddingRight: 4,
          }}>
            {availableParams.map((p) => {
              const value = paramOverrides[p.id] ?? p.current;
              const overridden = p.id in paramOverrides;
              const range = p.max - p.min;
              const step = range > 4 ? 0.05 : 0.01;
              return (
                <div key={p.id} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10 }}>
                    <span
                      style={{
                        flex: 1,
                        color: overridden ? '#a5b4fc' : '#888',
                        fontFamily: 'ui-monospace, monospace',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      title={p.id}
                    >
                      {p.id}
                    </span>
                    <span style={{ color: '#666', minWidth: 36, textAlign: 'right' }}>
                      {value.toFixed(2)}
                    </span>
                    {overridden && (
                      <button
                        type="button"
                        onClick={() => onParamReset(p.id)}
                        title="Clear override (return to model animation)"
                        style={{
                          background: 'transparent',
                          color: '#666',
                          border: 'none',
                          fontSize: 11,
                          cursor: 'pointer',
                          padding: 0,
                        }}
                      >
                        ✕
                      </button>
                    )}
                  </div>
                  <input
                    type="range"
                    min={p.min}
                    max={p.max}
                    step={step}
                    value={value}
                    onChange={(e) => onParamChange(p.id, Number(e.target.value))}
                    style={{ width: '100%', accentColor: overridden ? '#3b82f6' : '#444' }}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Behavior — idle motion + eye tracking */}
      <div style={{ borderTop: '1px solid #2a2d33', paddingTop: 10 }}>
        <div style={{ fontSize: 12, color: '#aaa', marginBottom: 6 }}>Behavior</div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#aaa', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={prefs.idleMotion}
            onChange={(e) => onChange({ ...prefs, idleMotion: e.target.checked })}
          />
          Auto-play idle motion
        </label>
        {prefs.idleMotion && (
          <SliderRow
            label="Interval"
            value={prefs.idleMotionSecs}
            min={3} max={60} step={1}
            fmt={(v) => `${Math.round(v)}s`}
            onChange={(v) => onChange({ ...prefs, idleMotionSecs: v })}
          />
        )}
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#aaa', cursor: 'pointer', marginTop: 6 }}>
          <input
            type="checkbox"
            checked={prefs.eyeTracking}
            onChange={(e) => onChange({ ...prefs, eyeTracking: e.target.checked })}
          />
          Gaze follows cursor
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#aaa', cursor: 'pointer', marginTop: 6 }}>
          <input
            type="checkbox"
            checked={prefs.webcamTracking}
            onChange={(e) => onChange({ ...prefs, webcamTracking: e.target.checked })}
          />
          Webcam motion tracking <span style={{ color: '#666', fontSize: 10 }}>(experimental)</span>
        </label>
        <div style={{ fontSize: 10, color: '#666', marginLeft: 24, marginTop: 2, lineHeight: 1.4 }}>
          Asks for webcam permission. Detects motion via frame
          difference (no ML model download) and steers the avatar's
          gaze toward where you move.
        </div>
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
