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
        .invoke_handler(tauri::generate_handler![show_avatar_window, hide_avatar_window])
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
