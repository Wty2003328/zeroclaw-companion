// Prevents additional console window on Windows in release; will not affect dev / debug builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    companion_tauri_lib::run();
}
