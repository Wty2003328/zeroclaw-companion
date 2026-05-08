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
    Play { wav_bytes: Vec<u8>, turn_id: String },
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

    while let Ok(cmd) = rx.recv() {
        match cmd {
            AudioCmd::Stop => {
                sink = None;
                current_turn = None;
            }
            AudioCmd::Play { wav_bytes, turn_id } => {
                if current_turn.as_deref() != Some(&turn_id) {
                    // New turn — drop the previous sink (and its queue)
                    // so we don't carry over chunks from a stale reply.
                    sink = None;
                    current_turn = Some(turn_id);
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
                let cursor = std::io::Cursor::new(wav_bytes);
                match rodio::Decoder::new_wav(cursor) {
                    Ok(source) => {
                        if let Some(ref s) = sink {
                            s.append(source);
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
        ])
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) && window.label() == "main" {
                let app = window.app_handle();
                if let Some(state) = app.try_state::<ServerProcess>() {
                    if let Some(child) = state.0.lock().ok().and_then(|mut g| g.take()) {
                        let _ = child.kill();
                        tracing::info!("companion-tauri: killed sidecar on exit");
                    }
                }
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
