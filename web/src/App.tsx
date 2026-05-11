import { useEffect, useState } from 'react';
import { flushSync } from 'react-dom';
import {
  BrowserRouter, Link, Navigate, Route, Routes,
  useNavigate, useLocation,
} from 'react-router-dom';
import { HTTP_BASE } from './lib/apiBase';
import { prewarm } from './lib/fetchCache';

// Eagerly import all routes. Used to be `lazy()` + Suspense, but that
// caused a per-route chunk-fetch + parse on first navigation, which
// felt like a tab-switch jitter (50–100ms even over loopback). For a
// desktop app shipped as a single bundle there's no upside to lazy
// loading — the whole bundle is on disk anyway, and merging Avatar
// (the largest chunk because of pixi-live2d-display) with the rest
// makes route changes synchronous.
import Home from './pages/Home';
import Avatar from './pages/Avatar';
import Pulse from './pages/Pulse';
import Settings from './pages/Settings';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tauriInvoke(): ((cmd: string, args?: Record<string, unknown>) => Promise<any>) | null {
  if (typeof window === 'undefined') return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const w = window as any;
  const inv = w.__TAURI_INTERNALS__?.invoke ?? w.__TAURI__?.invoke ?? null;
  return typeof inv === 'function' ? inv : null;
}

const PET_VISIBLE_KEY = 'companion.petVisible.v1';

// True only when this window is the main one (NOT the overlay). The
// overlay shouldn't render its own copy of the nav / pet toggle.
const IS_OVERLAY_WINDOW =
  typeof window !== 'undefined' &&
  new URLSearchParams(window.location.search).has('overlay');

export default function App() {
  return (
    <BrowserRouter>
      <ViewTransitionStyles />
      {IS_OVERLAY_WINDOW && <OverlayTransparencyStyles />}
      {!IS_OVERLAY_WINDOW && <BootPrewarm />}
      <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden' }}>
        {!IS_OVERLAY_WINDOW && <Nav />}
        {!IS_OVERLAY_WINDOW && <ZeroclawHealthBanner />}
        {/* Pages set their own scroll container. The wrapper here uses
            `flex: 1 1 0` + `minHeight: 0` so the child can compute its
            `height: 100%` against a definite cross-size — without
            `minHeight: 0`, the flex item's default min-content height
            grows to fit the page content and the inner overflow-auto
            never engages, which is what cuts the Save/Restart buttons
            off the bottom of Settings on shorter viewports. */}
        <div style={{ flex: '1 1 0', minHeight: 0, position: 'relative', display: 'flex', flexDirection: 'column' }}>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/avatar" element={<Avatar />} />
            {/* /characters folded into Home; keep redirect for any deep-links. */}
            <Route path="/characters" element={<Navigate to="/" replace />} />
            <Route path="/pulse" element={<Pulse />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}

/** Fire common GETs in parallel on app boot so the cache is warm
 *  before the user clicks any nav link. By the time they navigate
 *  to /pulse, /api/pulse/feed?limit=50 is already cached and the
 *  page renders instantly from cache while a background revalidate
 *  picks up any new items. */
function BootPrewarm() {
  useEffect(() => {
    prewarm([
      `${HTTP_BASE}/api/status`,
      `${HTTP_BASE}/api/characters`,
      `${HTTP_BASE}/api/config`,
      `${HTTP_BASE}/api/pulse/status`,
      `${HTTP_BASE}/api/pulse/feed?limit=50`,
      `${HTTP_BASE}/api/pulse/unread_count`,
    ]);
  }, []);
  return null;
}

/** Override the dark body/html background that's set globally in
 *  index.html so the overlay (pet) window is genuinely transparent.
 *  Without this, Tauri's `transparent: true` is wasted — the WebView2
 *  paints the body's `#0b0d10` over the entire window and the pet
 *  looks like it lives in a dark rectangle instead of floating on
 *  the desktop. We only inject this in the overlay window so the
 *  main window keeps its dark theme intact. */
function OverlayTransparencyStyles() {
  // Set inline styles directly on the elements we need transparent.
  // WebView2 wouldn't honor a <style>-tag !important override against
  // the index.html stylesheet (the new tag's `sheet` property stayed
  // false in CDP — never parsed as a stylesheet). Inline styles win
  // because they have higher specificity than any external rule and
  // bypass the parsing path entirely.
  useEffect(() => {
    const html = document.documentElement;
    const body = document.body;
    const root = document.getElementById('root');
    const prevHtml = html.style.backgroundColor;
    const prevBody = body.style.backgroundColor;
    const prevRoot = root?.style.backgroundColor ?? '';
    html.style.backgroundColor = 'transparent';
    body.style.backgroundColor = 'transparent';
    if (root) root.style.backgroundColor = 'transparent';
    return () => {
      html.style.backgroundColor = prevHtml;
      body.style.backgroundColor = prevBody;
      if (root) root.style.backgroundColor = prevRoot;
    };
  }, []);
  return null;
}

/** Inject view-transition CSS once at app root. Browsers that don't
 *  support startViewTransition just ignore it. Kept very short
 *  (70ms) — the transition pauses paints for its duration so anything
 *  longer feels like nav lag. 70ms is enough for the eye to register
 *  a soft transition without feeling sluggish. */
function ViewTransitionStyles() {
  return (
    <style>{`
      @keyframes companion-fade-in { from { opacity: 0; } to { opacity: 1; } }
      @keyframes companion-fade-out { from { opacity: 1; } to { opacity: 0; } }
      ::view-transition-old(root) {
        animation: companion-fade-out 70ms ease-out both;
      }
      ::view-transition-new(root) {
        animation: companion-fade-in 70ms ease-out both;
      }
    `}</style>
  );
}

/**
 * Banner that warns the user when zeroclaw (the main agent) isn't
 * reachable. The companion never spawns/kills zeroclaw — it's a
 * separate daemon the user manages, which may live on this machine
 * OR on a server / Raspberry Pi / laptop on the LAN.
 *
 * We probe `/api/status`'s `zeroclaw_up` — that's authoritative
 * because companion-server checks zeroclaw at whatever URL is
 * configured (local OR remote). We deliberately do NOT use the Tauri
 * `check_zeroclaw_health` command for the banner: it defaults to
 * `127.0.0.1:42617` when called without a URL, which would give a
 * false "not reachable" for any remote-zeroclaw setup.
 */
function ZeroclawHealthBanner() {
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const r = await fetch('/api/status');
        let ok = false;
        if (r.ok) {
          const j = await r.json();
          ok = !!j.zeroclaw_up;
        }
        if (!cancelled) setHealthy(ok);
      } catch {
        if (!cancelled) setHealthy(false);
      }
    };
    void check();
    const id = setInterval(check, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (healthy !== false || dismissed) return null;
  return (
    <div
      style={{
        background: '#3a1c1c',
        borderBottom: '1px solid #5a2a2a',
        color: '#fcd5d5',
        padding: '8px 16px',
        fontSize: 12,
        display: 'flex',
        alignItems: 'center',
        gap: 12,
      }}
    >
      <span>⚠️</span>
      <span style={{ flex: 1 }}>
        <strong>The main agent isn't running.</strong> Chat won't work until
        you start it. This app stays separate from the agent on purpose —
        you manage the agent yourself. We'll re-check every 30 seconds.
      </span>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        style={{
          background: 'transparent',
          color: '#fcd5d5',
          border: '1px solid #5a2a2a',
          borderRadius: 4,
          padding: '2px 10px',
          cursor: 'pointer',
          fontSize: 11,
        }}
      >
        dismiss
      </button>
    </div>
  );
}

function Nav() {
  // Stored preference is just a hint for the boot path (so a new Tauri
  // launch can auto-restore the pet to its last visible state). The
  // ACTUAL state below comes from Tauri's `is_avatar_window_visible`,
  // polled on mount + every 2s + after each toggle. Without this, the
  // button drifted out of sync whenever the avatar window was closed
  // by some path other than the toggle (Alt+F4, a stuck show command,
  // or a Tauri restart that didn't honor the stored preference).
  const [petVisible, setPetVisible] = useState<boolean>(() => {
    try { return localStorage.getItem(PET_VISIBLE_KEY) === '1'; }
    catch { return false; }
  });

  // Sync from Tauri on mount + every 2s. The UI button reflects what
  // Tauri reports, not localStorage.
  useEffect(() => {
    const inv = tauriInvoke();
    if (!inv) return;
    let cancelled = false;
    const sync = async () => {
      try {
        const visible = await inv('is_avatar_window_visible');
        if (!cancelled) setPetVisible(!!visible);
      } catch { /* avatar window may not exist mid-restart */ }
    };
    // Initial sync — also restore from localStorage if Tauri starts
    // hidden but the user had pet ON last session.
    void (async () => {
      try {
        const visible = await inv('is_avatar_window_visible');
        const want = localStorage.getItem(PET_VISIBLE_KEY) === '1';
        if (!visible && want) {
          await inv('show_avatar_window').catch(() => {});
        }
        await sync();
      } catch { /* non-fatal */ }
    })();
    // 5s is plenty — the Pet ON/OFF state only diverges from the
    // toggle when something exotic happens (Alt+F4 the overlay,
    // Tauri restart). 2s was wasted Nav re-renders.
    const id = setInterval(sync, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const togglePet = () => {
    const inv = tauriInvoke();
    setPetVisible((v) => {
      const next = !v;
      try { localStorage.setItem(PET_VISIBLE_KEY, next ? '1' : '0'); }
      catch { /* non-fatal */ }
      if (inv) {
        void inv(next ? 'show_avatar_window' : 'hide_avatar_window').catch(
          (e) => console.error('pet toggle invoke failed:', e),
        );
      }
      return next;
    });
  };

  return (
    <nav
      style={{
        display: 'flex',
        gap: 16,
        // Slightly more vertical padding so the title doesn't touch the
        // OS title bar in environments where the system extends chrome
        // into the content rect (Windows 11 Mica, some Tauri setups).
        padding: '14px 24px',
        borderBottom: '1px solid #1f2227',
        background: '#0e1014',
        alignItems: 'center',
        flexShrink: 0,
      }}
    >
      <Link to="/" style={{ color: '#fff', fontWeight: 600, textDecoration: 'none', fontSize: 14 }}>
        zeroclaw·companion
      </Link>
      <span style={{ flex: 1 }} />
      <NavLink to="/" label="Home" />
      <NavLink to="/avatar" label="Avatar" />
      <NavLink to="/pulse" label="Pulse" />
      <NavLink to="/settings" label="Settings" />
      <button
        type="button"
        onClick={togglePet}
        title={petVisible ? 'Hide the always-on-top desktop pet window' : 'Show the always-on-top desktop pet window'}
        style={{
          marginLeft: 8,
          padding: '4px 12px',
          borderRadius: 6,
          background: petVisible ? '#3b82f6' : 'transparent',
          border: petVisible ? 'none' : '1px solid #2a2d33',
          color: petVisible ? '#fff' : '#aaa',
          fontSize: 12,
          cursor: 'pointer',
        }}
      >
        {petVisible ? '🪟 Pet ON' : '🪟 Show pet'}
      </button>
    </nav>
  );
}

/** Custom NavLink that wraps `navigate(to)` in
 *  `document.startViewTransition` so route changes cross-fade for
 *  ~140ms instead of hard-cutting. Falls back to a normal navigate
 *  in browsers without the API (Safari < 18, Firefox < recent).
 *  The `flushSync` is required: startViewTransition snapshots the
 *  DOM synchronously inside its callback, so the React update must
 *  flush before it returns.
 *
 *  We use a plain `<a>` instead of react-router-dom's `<Link>`
 *  because Link's internal click handler races our preventDefault
 *  in some configurations and ends up doing its own navigate
 *  before our startViewTransition callback runs — bypassing the
 *  cross-fade entirely. */
function NavLink({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const isActive = location.pathname === to;
  const onClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // honor browser defaults
    e.preventDefault();
    if (location.pathname === to) return; // no-op
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const startVT: ((cb: () => void) => unknown) | undefined = (document as any).startViewTransition?.bind(document);
    if (startVT) {
      startVT(() => { flushSync(() => navigate(to)); });
    } else {
      navigate(to);
    }
  };
  return (
    <a
      href={to}
      onClick={onClick}
      style={{
        color: isActive ? '#fff' : '#aaa',
        textDecoration: 'none',
        fontSize: 14,
        fontWeight: isActive ? 600 : 400,
        transition: 'color 120ms ease',
      }}
    >
      {label}
    </a>
  );
}
