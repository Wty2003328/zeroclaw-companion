/**
 * Native audio playback bridge.
 *
 * In Tauri, route audio bytes through `tauri::invoke('play_audio_native')`
 * instead of the embedded WebView's <video>/Web Audio path. WebView2 on
 * Windows classifies its audio as "communications" → AGC + acoustic echo
 * cancellation get applied to TTS output. The Tauri Rust side runs
 * playback via `rodio` → cpal → WASAPI in the host process, which
 * Windows treats as multimedia.
 *
 * In the browser there's no `__TAURI_INTERNALS__` global so we fall
 * back to the existing <video> renderer (clean enough there).
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<any>;

function tauriInvoke(): InvokeFn | null {
  if (typeof window === 'undefined') return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const w = window as any;
  // Tauri 2 invoke is at __TAURI_INTERNALS__.invoke. Older 1.x put it
  // under window.__TAURI__.invoke — handle both for safety.
  const inv =
    w.__TAURI_INTERNALS__?.invoke ?? w.__TAURI__?.invoke ?? null;
  return typeof inv === 'function' ? (inv as InvokeFn) : null;
}

/** True only when running under Tauri AND the native bridge is reachable. */
export function nativeAudioAvailable(): boolean {
  return tauriInvoke() !== null;
}

/** Play a chunk of WAV bytes through Tauri's native rodio backend.
 *  All chunks of the same `turnId` queue into one continuous sink.
 *  `seq` is the 0-based chunk index — the rodio worker drops repeats
 *  of the same (turnId, seq) so we don't double-play when the avatar
 *  overlay window AND the main window both receive the broadcast.
 *  `last` flags the turn's final chunk so the worker's jitter buffer
 *  can start a short reply immediately instead of waiting for more.
 *  An empty `audioB64` with `last=true` is the end-of-turn terminator
 *  (no trailing audio) — forward it so the worker flushes + finishes. */
export async function playAudioNative(
  audioB64: string,
  turnId: string,
  seq: number,
  last: boolean,
): Promise<void> {
  const invoke = tauriInvoke();
  if (!invoke) throw new Error('not running under Tauri');
  await invoke('play_audio_native', { audioB64, turnId, seq, last });
}

/** Interrupt any in-flight native playback (drops the queue). */
export async function stopAudioNative(): Promise<void> {
  const invoke = tauriInvoke();
  if (!invoke) return;
  try {
    await invoke('stop_audio_native');
  } catch {
    // No-op if Tauri command isn't registered yet.
  }
}
