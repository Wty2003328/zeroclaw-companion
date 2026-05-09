/**
 * Tauri shell helpers.
 *
 * WebView2 (the browser engine Tauri uses on Windows) silently drops
 * `<a target="_blank">` and `window.open(url)` for cross-origin URLs —
 * there's no popup support in a single-window app shell. The Pulse
 * drawer's "Open ↗" button used to be a plain anchor and did nothing
 * in the desktop build (worked fine in the dev browser).
 *
 * The fix: route external opens through a Tauri command that calls
 * tauri-plugin-shell's default browser launcher. In the dev browser
 * (no Tauri runtime), fall back to `window.open` which works there.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tauriInvoke(): ((cmd: string, args?: Record<string, unknown>) => Promise<any>) | null {
  if (typeof window === 'undefined') return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const w = window as any;
  const inv = w.__TAURI_INTERNALS__?.invoke ?? w.__TAURI__?.invoke ?? null;
  return typeof inv === 'function' ? inv : null;
}

/** Open an http(s) URL in the user's default browser.
 *  Resolves once the OS has handed off the URL — typically <50ms.
 *  Errors are logged and swallowed so a misclick can't break the UI. */
export async function openExternal(url: string): Promise<void> {
  if (!url) return;
  const inv = tauriInvoke();
  try {
    if (inv) {
      await inv('open_external_url', { url });
    } else {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
  } catch (e) {
    console.warn('openExternal failed:', e);
  }
}

/** Open a native file picker. Returns the absolute file path the
 *  user selected, or null if cancelled / running outside Tauri.
 *  Used by the Settings page's "Browse" buttons.
 *
 *  `filters` lets us scope the dialog to specific extensions (e.g.
 *  WAV-only for the reference audio). Falls back to "any file" when
 *  empty. `startDir` is optional and defaults to the OS-picked
 *  recent location. */
export async function pickFile(opts: {
  title?: string;
  filters?: { label: string; extensions: string[] }[];
  startDir?: string;
} = {}): Promise<string | null> {
  const inv = tauriInvoke();
  if (!inv) return null;
  // Tauri serializes Vec<(String, Vec<String>)> as JS [string, string[]] tuples.
  const filters = (opts.filters ?? []).map(f => [f.label, f.extensions]);
  try {
    const result = await inv('pick_file', {
      title: opts.title,
      filters,
      startDir: opts.startDir,
    });
    return typeof result === 'string' ? result : null;
  } catch (e) {
    console.warn('pickFile failed:', e);
    return null;
  }
}

export interface DetectedGpu {
  index: number;
  name: string;
  /** Total VRAM in MB if known (nvidia-smi path); null otherwise. */
  vram_total_mb: number | null;
}

/** Detect the GPUs available to this machine. Best-effort:
 *  - nvidia-smi if installed (cleanest result with VRAM)
 *  - Windows WMI fallback (lists all video adapters)
 *  - empty array if both fail
 *  Settings.tsx falls back to "CPU only" + a guessed "GPU 0" entry
 *  when this returns nothing, so the form is always usable. */
export async function listGpus(): Promise<DetectedGpu[]> {
  const inv = tauriInvoke();
  if (!inv) return [];
  try {
    const result = await inv('list_gpus');
    return Array.isArray(result) ? (result as DetectedGpu[]) : [];
  } catch (e) {
    console.warn('listGpus failed:', e);
    return [];
  }
}

/** Open a native folder picker. Returns the directory path or null. */
export async function pickFolder(opts: {
  title?: string;
  startDir?: string;
} = {}): Promise<string | null> {
  const inv = tauriInvoke();
  if (!inv) return null;
  try {
    const result = await inv('pick_folder', {
      title: opts.title,
      startDir: opts.startDir,
    });
    return typeof result === 'string' ? result : null;
  } catch (e) {
    console.warn('pickFolder failed:', e);
    return null;
  }
}
