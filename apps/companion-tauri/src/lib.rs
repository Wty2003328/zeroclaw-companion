//! Companion Tauri shell.
//!
//! Lifecycle:
//! 1. App starts.
//! 2. Spawns the bundled `companion-server` sidecar (provides `/api/*`,
//!    `/ws/avatar`, the static web bundle).
//! 3. Main window loads `/`. Avatar window can be opened on demand for a
//!    transparent, always-on-top desktop pet.
//! 4. On exit, kills the sidecar so the user doesn't end up with an
//!    orphaned process.

use std::sync::Mutex;

use tauri::{AppHandle, Manager, WindowEvent};
use tauri_plugin_shell::{ShellExt, process::CommandChild};

/// Holds the companion-server sidecar process so we can kill it on exit.
struct ServerProcess(Mutex<Option<CommandChild>>);

/// Audio playback runs on a dedicated worker thread because rodio's
/// `OutputStream` (Windows COM handles) is `!Send` and can't sit in
/// Tauri's `State` directly. The Tauri-managed `AudioState` is just a
/// command sender — `Send + Sync` — and the worker owns the stream,
/// sink, and current-turn id.
enum AudioCmd {
    Play {
        wav_bytes: Vec<u8>,
        turn_id: String,
        /// 0-based chunk index within this turn. Combined with turn_id
        /// it identifies a unique audio chunk; duplicate (turn_id, seq)
        /// pairs are dropped so multiple windows / multiple WS clients
        /// don't queue the same audio twice into the rodio Sink (which
        /// would play it twice — the symptom users heard as "she said
        /// the sentence twice").
        seq: u32,
    },
    Stop,
}

struct AudioState {
    tx: std::sync::mpsc::Sender<AudioCmd>,
}

impl AudioState {
    fn spawn() -> anyhow::Result<Self> {
        let (tx, rx) = std::sync::mpsc::channel::<AudioCmd>();
        std::thread::Builder::new()
            .name("companion-audio".into())
            .spawn(move || run_audio_worker(rx))?;
        Ok(Self { tx })
    }
}

fn run_audio_worker(rx: std::sync::mpsc::Receiver<AudioCmd>) {
    // Open the default Windows audio output. cpal/WASAPI in this
    // process — Windows classifies as multimedia (NOT communications),
    // so no AGC / echo cancellation gets applied to TTS.
    let (_stream, handle) = match rodio::OutputStream::try_default() {
        Ok(pair) => pair,
        Err(e) => {
            tracing::error!("companion-audio: failed to open output: {e}");
            return;
        }
    };
    let mut current_turn: Option<String> = None;
    let mut sink: Option<rodio::Sink> = None;
    // (turn_id, seq) of chunks already appended to the current sink.
    // Multiple windows / multiple WS clients each fire `play_audio_native`
    // for the same broadcast Audio frame; without this dedupe set we
    // append the same WAV to the sink twice (or more) and rodio plays
    // it back twice. Cleared on turn change so we don't grow forever.
    let mut seen_chunks: std::collections::HashSet<u32> = std::collections::HashSet::new();

    while let Ok(cmd) = rx.recv() {
        match cmd {
            AudioCmd::Stop => {
                sink = None;
                current_turn = None;
                seen_chunks.clear();
            }
            AudioCmd::Play {
                wav_bytes,
                turn_id,
                seq,
            } => {
                if current_turn.as_deref() != Some(&turn_id) {
                    // New turn — drop the previous sink (and its queue)
                    // so we don't carry over chunks from a stale reply.
                    sink = None;
                    seen_chunks.clear();
                    current_turn = Some(turn_id.clone());
                }
                if !seen_chunks.insert(seq) {
                    tracing::debug!(
                        "companion-audio: dropping duplicate chunk turn={turn_id} seq={seq} \
                         (likely fanout from multiple WS clients)"
                    );
                    continue;
                }
                if sink.is_none() {
                    sink = match rodio::Sink::try_new(&handle) {
                        Ok(s) => Some(s),
                        Err(e) => {
                            tracing::error!("companion-audio: sink alloc: {e}");
                            continue;
                        }
                    };
                }
                let bytes_len = wav_bytes.len();
                let cursor = std::io::Cursor::new(wav_bytes);
                match rodio::Decoder::new_wav(cursor) {
                    Ok(source) => {
                        if let Some(ref s) = sink {
                            s.append(source);
                            tracing::info!(
                                "companion-audio: queued chunk turn={turn_id} seq={seq} bytes={bytes_len} sink_len={}",
                                s.len(),
                            );
                        }
                    }
                    Err(e) => {
                        tracing::warn!("companion-audio: wav decode: {e}");
                    }
                }
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,companion=debug".into()),
        )
        .compact()
        .init();

    // Make WebView2 stop classifying our audio as a "communications"
    // session. Windows otherwise treats non-browser hosts as voice
    // apps and applies AGC + acoustic echo cancellation, producing
    // the echo / processed-voice symptoms users hear in Tauri but
    // not in Edge browser. These flags are read by the WebView2
    // runtime before any window is created.
    //
    // - AudioServiceOutOfProcess: keep audio in-process so the WebView2
    //   stream doesn't inherit a separate Windows audio session.
    // - autoplay-policy=no-user-gesture-required: matches the user
    //   intent in a single-purpose app shell.
    #[cfg(target_os = "windows")]
    {
        let prev = std::env::var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS").unwrap_or_default();
        let extra = "--disable-features=AudioServiceOutOfProcess \
                     --autoplay-policy=no-user-gesture-required";
        let combined = if prev.is_empty() {
            extra.to_string()
        } else {
            format!("{prev} {extra}")
        };
        // SAFETY: set_var is unsafe in edition 2024. We run before any
        // window or other thread that would read env. Single-threaded init.
        unsafe {
            std::env::set_var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", combined);
        }
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let handle = app.handle().clone();
            spawn_companion_server(&handle);
            Ok(())
        })
        .manage(ServerProcess(Mutex::new(None)))
        .manage(match AudioState::spawn() {
            Ok(s) => s,
            Err(e) => {
                // Don't panic — fall back to a sender that drops
                // commands. The frontend will fail invoke and revert
                // to its WebView <video> path.
                tracing::error!("companion-tauri: audio worker spawn failed: {e}");
                let (tx, _rx) = std::sync::mpsc::channel::<AudioCmd>();
                AudioState { tx }
            }
        })
        .invoke_handler(tauri::generate_handler![
            show_avatar_window,
            hide_avatar_window,
            play_audio_native,
            stop_audio_native,
            restart_app,
            get_avatar_window_geometry,
            set_avatar_window_position,
            get_avatar_monitor,
            start_dragging_avatar_window,
            check_zeroclaw_health,
            open_external_url,
            is_avatar_window_visible,
            open_models_folder,
            pick_file,
            pick_folder,
            list_gpus,
        ])
        .on_window_event(|window, event| {
            // Intercept avatar (overlay) window close: don't destroy
            // the window — just hide it so it remains toggleable via
            // the Nav's "Show pet" button. Without this, Alt+F4 on
            // the overlay leaves Tauri with a dropped window handle
            // and subsequent `show_avatar_window` invokes hit "avatar
            // window not found".
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "avatar" {
                    let _ = window.hide();
                    api.prevent_close();
                    return;
                }
            }
            if matches!(event, WindowEvent::Destroyed) && window.label() == "main" {
                let app = window.app_handle();
                let app_handle = app.clone();
                // Graceful shutdown: POST /api/shutdown so companion-
                // server runs its TTS stop_server() (which POSTs
                // /shutdown to the Python TTS, runs CUDA cleanup, then
                // exits). Without this, killing the sidecar via
                // TerminateProcess orphans the Python TTS, leaving the
                // CUDA driver in a fragmented state for ~30–90s and
                // causing user-reported "games stutter after closing
                // companion." We wait up to 10s for the graceful path
                // to complete, then fall back to child.kill().
                std::thread::spawn(move || {
                    let _ = ureq::post("http://127.0.0.1:9181/api/shutdown")
                        .timeout(std::time::Duration::from_secs(2))
                        .call();
                    // Give companion-server room to stop TTS
                    // (its own stop_server has an 8s graceful budget).
                    let mut waited = 0;
                    while waited < 12 {
                        if ureq::get("http://127.0.0.1:9181/health")
                            .timeout(std::time::Duration::from_millis(500))
                            .call()
                            .is_err()
                        {
                            break; // server is gone — clean exit
                        }
                        std::thread::sleep(std::time::Duration::from_secs(1));
                        waited += 1;
                    }
                    // Fall back to TerminateProcess if it didn't exit.
                    if let Some(state) = app_handle.try_state::<ServerProcess>() {
                        if let Some(child) = state.0.lock().ok().and_then(|mut g| g.take()) {
                            let _ = child.kill();
                            tracing::info!(
                                "companion-tauri: hard-killed sidecar after {waited}s"
                            );
                        }
                    }
                });
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn spawn_companion_server(app: &AppHandle) {
    let shell = app.shell();
    match shell.sidecar("companion-server") {
        Ok(cmd) => match cmd.spawn() {
            Ok((_rx, child)) => {
                tracing::info!("companion-tauri: spawned companion-server sidecar");
                if let Some(state) = app.try_state::<ServerProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        *guard = Some(child);
                    }
                }
            }
            Err(e) => {
                tracing::error!("companion-tauri: failed to spawn sidecar: {e}");
            }
        },
        Err(e) => {
            tracing::error!(
                "companion-tauri: sidecar resolution failed (is the binary bundled?): {e}"
            );
        }
    }
}

#[tauri::command]
fn show_avatar_window(app: AppHandle) -> Result<(), String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    win.show().map_err(|e| e.to_string())?;
    win.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn hide_avatar_window(app: AppHandle) -> Result<(), String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    win.hide().map_err(|e| e.to_string())?;
    Ok(())
}

/// Play a base64-encoded WAV chunk via the native rodio backend.
///
/// All chunks of the same `turn_id` queue into the same Sink so they
/// play back-to-back without gaps. A new `turn_id` interrupts and
/// drops the previous queue. Bytes go through cpal → WASAPI in the
/// Tauri host process, NOT the WebView2 audio pipeline — bypasses
/// the "communications" classification + DSP that processes TTS in
/// WebView2.
#[tauri::command]
fn play_audio_native(
    state: tauri::State<'_, AudioState>,
    audio_b64: String,
    turn_id: String,
    seq: u32,
) -> Result<(), String> {
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(&audio_b64)
        .map_err(|e| format!("base64 decode: {e}"))?;
    state
        .tx
        .send(AudioCmd::Play {
            wav_bytes: bytes,
            turn_id,
            seq,
        })
        .map_err(|e| format!("audio worker gone: {e}"))
}

/// Interrupt any in-progress native playback. Called when the frontend
/// component unmounts or the user clears chat history.
#[tauri::command]
fn stop_audio_native(state: tauri::State<'_, AudioState>) {
    let _ = state.tx.send(AudioCmd::Stop);
}

/// Restart the Tauri host process. Used by the Settings page after the
/// user saves a subagent override — the change takes effect on next
/// boot because the live subagent is built once at companion-server
/// startup and isn't hot-swappable. Tauri's `app.restart()` cleanly
/// kills the sidecar (via the WindowEvent::Destroyed handler) before
/// re-launching the binary.
#[tauri::command]
fn restart_app(app: AppHandle) {
    app.restart();
}

#[derive(serde::Serialize)]
struct WindowGeometry {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

#[derive(serde::Serialize)]
struct MonitorBounds {
    /// Monitor's top-left corner in physical screen coordinates.
    x: i32,
    y: i32,
    /// Monitor size in physical pixels.
    width: u32,
    height: u32,
}

/// Read the avatar (overlay) window's current outer position + size.
/// JS uses this to (a) save the desktop pet's last position to
/// localStorage on a debounced move event and (b) feed snap-to-edge
/// math without a separate roundtrip per pixel.
#[tauri::command]
fn get_avatar_window_geometry(app: AppHandle) -> Result<WindowGeometry, String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    let pos = win.outer_position().map_err(|e| e.to_string())?;
    let size = win.outer_size().map_err(|e| e.to_string())?;
    Ok(WindowGeometry {
        x: pos.x,
        y: pos.y,
        width: size.width,
        height: size.height,
    })
}

/// Move the avatar (overlay) window. Called on overlay-window mount
/// to restore the user's last saved position, and by the snap-to-edge
/// helper after each drag.
#[tauri::command]
fn set_avatar_window_position(app: AppHandle, x: i32, y: i32) -> Result<(), String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    win.set_position(tauri::PhysicalPosition::new(x, y))
        .map_err(|e| e.to_string())
}

/// Begin dragging the avatar window. Used by the JS-level drag
/// handler in overlay mode — `data-tauri-drag-region` would normally
/// be enough, but pixi-live2d-display's interaction system swallows
/// mousedown on the canvas before Tauri's runtime sees it. We work
/// around this by listening for mousedown ourselves and explicitly
/// invoking the OS-level window drag.
#[tauri::command]
fn start_dragging_avatar_window(app: AppHandle) -> Result<(), String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    win.start_dragging().map_err(|e| e.to_string())
}

/// Open a native file picker. Returns the selected path or None if
/// the user cancelled. Used by the Settings → Voice engine "Browse"
/// buttons to fill in the launcher script, reference audio, etc.
///
/// `filters` is a list of (label, [extensions]) pairs; pass an empty
/// vec for "any file". `start_dir` is optional and defaults to the
/// last-opened directory when None.
#[tauri::command]
async fn pick_file(
    app: AppHandle,
    title: Option<String>,
    filters: Option<Vec<(String, Vec<String>)>>,
    start_dir: Option<String>,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let mut builder = app.dialog().file();
    if let Some(t) = title {
        builder = builder.set_title(t);
    }
    if let Some(dir) = start_dir.filter(|s| !s.is_empty()) {
        builder = builder.set_directory(std::path::PathBuf::from(dir));
    }
    if let Some(f_list) = filters {
        for (label, exts) in f_list {
            let exts_ref: Vec<&str> = exts.iter().map(|s| s.as_str()).collect();
            builder = builder.add_filter(&label, &exts_ref);
        }
    }
    // Block on the dialog with a tokio oneshot so the JS invoke awaits
    // until the user picks or cancels. tauri-plugin-dialog's API is
    // callback-based on desktop.
    let (tx, rx) = tokio::sync::oneshot::channel::<Option<String>>();
    builder.pick_file(move |path| {
        let _ = tx.send(path.and_then(|p| p.into_path().ok()).map(|p| p.to_string_lossy().to_string()));
    });
    rx.await.map_err(|e| e.to_string())
}

/// Pick a directory (e.g. the GPT-SoVITS install root). Same UX as
/// `pick_file` but the user selects a folder instead of a file.
#[tauri::command]
async fn pick_folder(
    app: AppHandle,
    title: Option<String>,
    start_dir: Option<String>,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let mut builder = app.dialog().file();
    if let Some(t) = title {
        builder = builder.set_title(t);
    }
    if let Some(dir) = start_dir.filter(|s| !s.is_empty()) {
        builder = builder.set_directory(std::path::PathBuf::from(dir));
    }
    let (tx, rx) = tokio::sync::oneshot::channel::<Option<String>>();
    builder.pick_folder(move |path| {
        let _ = tx.send(path.and_then(|p| p.into_path().ok()).map(|p| p.to_string_lossy().to_string()));
    });
    rx.await.map_err(|e| e.to_string())
}

/// Detected GPU info for the Settings dropdown. The TTS engine uses
/// the index field; name + vram are display-only.
#[derive(serde::Serialize)]
struct GpuInfo {
    index: i32,
    name: String,
    /// Free / total VRAM in MB. None when we can't tell (WMI fallback).
    vram_total_mb: Option<u64>,
}

/// Enumerate the GPUs available for TTS inference. Order of attempts:
///   1. `nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits`
///      — gives index + name + total VRAM, the gold standard.
///   2. `wmic path win32_videocontroller get name` (Windows fallback).
///      Returns ALL video adapters (including iGPU + virtual ones)
///      so the indices won't necessarily align with CUDA device ids
///      — but it's better than showing GPU 0/1/2/3 hardcoded.
///   3. Empty list — caller should add a "CPU only" option and
///      maybe a generic "GPU 0" guess.
///
/// Always best-effort; never errors out.
#[tauri::command]
fn list_gpus() -> Vec<GpuInfo> {
    // Try nvidia-smi first — most accurate and gives VRAM.
    if let Ok(out) = std::process::Command::new("nvidia-smi")
        .args([
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
    {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout);
            let mut gpus = Vec::new();
            for line in text.lines() {
                let parts: Vec<&str> = line.split(',').map(|s| s.trim()).collect();
                if parts.len() >= 2 {
                    let index: i32 = parts[0].parse().unwrap_or(-1);
                    let name = parts[1].to_string();
                    let vram_total_mb = parts.get(2).and_then(|s| s.parse::<u64>().ok());
                    if index >= 0 && !name.is_empty() {
                        gpus.push(GpuInfo { index, name, vram_total_mb });
                    }
                }
            }
            if !gpus.is_empty() {
                return gpus;
            }
        }
    }

    // Windows fallback: WMI via wmic. Returns ALL video controllers,
    // not just CUDA-capable ones — hopefully fine because most users
    // either have one GPU or know which slot their training rig is in.
    #[cfg(target_os = "windows")]
    {
        if let Ok(out) = std::process::Command::new("wmic")
            .args(["path", "win32_videocontroller", "get", "name"])
            .output()
        {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout);
                let mut gpus = Vec::new();
                for (i, line) in text.lines().enumerate() {
                    let trimmed = line.trim();
                    // First line is "Name" header; skip empty lines.
                    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("Name") {
                        continue;
                    }
                    gpus.push(GpuInfo {
                        index: gpus.len() as i32,
                        name: trimmed.to_string(),
                        vram_total_mb: None,
                    });
                    let _ = i;
                }
                if !gpus.is_empty() {
                    return gpus;
                }
            }
        }
    }

    Vec::new()
}

/// Open the Live2D models directory in the OS file explorer.
/// Used by the character editor's "Open models folder" button so
/// users can drop in new model folders without going through a UI
/// uploader. Resolves the path the same way `handle_list_models` does:
/// `<cwd>/web/dist/live2d/models/`. We create the directory first so
/// the open call doesn't fail on a clean install.
#[tauri::command]
fn open_models_folder() -> Result<String, String> {
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    let dir = cwd.join("web").join("dist").join("live2d").join("models");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let dir_str = dir.to_string_lossy().to_string();
    // Use a separate process so we don't block on the UI thread.
    // Windows-only for now; other platforms get the same behavior via
    // tauri-plugin-shell's open if the user runs in dev there.
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&dir_str)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&dir_str)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        std::process::Command::new("xdg-open")
            .arg(&dir_str)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    Ok(dir_str)
}

/// Read the avatar (overlay) window's current visibility. The Nav's
/// "Show pet" toggle uses this as the source of truth — without it,
/// the button drifts out of sync if the window state changes without
/// going through `show_avatar_window` / `hide_avatar_window` (e.g.
/// the user Alt+F4'd the overlay, or a previous run started with
/// `visible: false` while localStorage still had `petVisible = 1`).
#[tauri::command]
fn is_avatar_window_visible(app: AppHandle) -> Result<bool, String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    win.is_visible().map_err(|e| e.to_string())
}

/// Open an http(s) URL in the user's default browser. Tauri's WebView2
/// silently drops `<a target="_blank">` and same-window `window.open`
/// can only navigate inside the IPC origin — so the Pulse drawer's
/// "Open ↗" button (and any other external link in the UI) routes
/// through here. Validation is enforced by tauri-plugin-shell's
/// default open scope regex (`^((mailto:\w+)|(tel:\w+)|(https?://\w+)).+`),
/// which rejects schemes like `file://` or `javascript:`.
#[tauri::command]
#[allow(deprecated)] // shell.open is fine for a small in-app helper; we don't need the new opener plugin's extra surface area.
fn open_external_url(app: AppHandle, url: String) -> Result<(), String> {
    app.shell().open(url, None).map_err(|e| e.to_string())
}

/// Probe whether upstream zeroclaw is running at the configured URL.
/// We never start or stop zeroclaw from this app — it's a separate
/// long-lived daemon the user manages. We only check.
#[tauri::command]
fn check_zeroclaw_health(url: String) -> Result<bool, String> {
    // Simple sync GET; runs off-thread under Tauri's invoke executor.
    let target = if url.is_empty() { "http://127.0.0.1:42617".to_string() } else { url };
    let result = ureq::get(&format!("{}/health", target.trim_end_matches('/')))
        .timeout(std::time::Duration::from_secs(2))
        .call();
    match result {
        Ok(resp) => Ok(resp.status() == 200),
        Err(_) => Ok(false),
    }
}

/// Return the work area of the monitor containing the avatar window
/// (in physical screen coords). Used by the snap-to-edge helper to
/// compute how close the pet is to a screen edge.
#[tauri::command]
fn get_avatar_monitor(app: AppHandle) -> Result<MonitorBounds, String> {
    let win = app
        .get_webview_window("avatar")
        .ok_or_else(|| "avatar window not found".to_string())?;
    let monitor = win
        .current_monitor()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "no current monitor".to_string())?;
    let pos = monitor.position();
    let size = monitor.size();
    Ok(MonitorBounds {
        x: pos.x,
        y: pos.y,
        width: size.width,
        height: size.height,
    })
}
