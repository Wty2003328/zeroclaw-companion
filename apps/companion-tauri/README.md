# companion-tauri

Tauri 2 desktop shell for `zeroclaw-companion`. Bundles `companion-server`
as a sidecar binary and shows the companion web UI in a native window.

## Why

- Same UX whether the user is on a server (web) or a laptop (desktop app)
- One install instead of two binaries to start manually
- Optional transparent always-on-top window for a "desktop pet" mode
- On exit, the sidecar is killed cleanly — no orphaned processes

## Build

You need:
- Node.js 20+ (`npm`)
- Rust toolchain (already required for the workspace)
- `cargo install tauri-cli@^2` (one-time)
- Platform deps: see <https://tauri.app/start/prerequisites/>

```bash
# 1. Build the companion-server binary first; tauri.conf.json's
#    externalBin entry expects it under apps/companion-tauri/binaries/
cargo build -p companion-server --release

# 2. Drop it into the Tauri sidecar location with the platform-triple
#    suffix Tauri requires:
mkdir -p apps/companion-tauri/binaries
TARGET=$(rustc -Vv | sed -n 's/host: //p')
cp target/release/companion-server apps/companion-tauri/binaries/companion-server-$TARGET

# Windows variant:
# copy target\release\companion-server.exe apps\companion-tauri\binaries\companion-server-x86_64-pc-windows-msvc.exe

# 3. Build & run
cd apps/companion-tauri
cargo tauri dev      # development with hot reload
cargo tauri build    # production bundle (.exe / .dmg / .deb)
```

## Status

- The Cargo crate compiles as part of `cargo check --workspace`.
- `cargo tauri dev` / `build` requires Tauri CLI + the platform-specific
  webview deps; the workspace Cargo build doesn't depend on them.
- Icons in `icons/` are placeholders — drop in real PNG/ICO/ICNS files
  before shipping a release.

## Architecture

```
┌─ companion-tauri (this crate) ──────────────────────────────┐
│                                                             │
│  ┌── main window ──────────┐  ┌── avatar window ─────────┐  │
│  │ http://127.0.0.1:9181   │  │ /avatar (transparent,    │  │
│  │ (companion-server UI)   │  │  always-on-top)          │  │
│  └─────────────────────────┘  └──────────────────────────┘  │
│                  ▲                          ▲                │
│                  │ webview                  │                │
│                  └──────────────┬───────────┘                │
│                                 │                            │
│   spawns ───▶  companion-server (sidecar)                    │
│                · /api/*  · /ws/avatar  · static web bundle   │
│   killed ◀───  (on exit)                                     │
└─────────────────────────────────────────────────────────────┘
                                 │
                                 │ HTTP / SSE
                                 ▼
                       upstream zeroclaw daemon
```
