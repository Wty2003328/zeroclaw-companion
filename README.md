# zeroclaw-companion

> A Live2D anime companion and information dashboard that gives the
> [zeroclaw](https://github.com/zeroclaw-labs/zeroclaw) AI agent a
> face, a voice, and a living workspace.

[![License: MIT OR Apache-2.0](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg)](#license)
[![Rust 1.88+](https://img.shields.io/badge/rust-1.88%2B-orange.svg)](https://www.rust-lang.org/)
[![Tauri 2](https://img.shields.io/badge/tauri-2.x-brightgreen.svg)](https://tauri.app/)

`zeroclaw-companion` is a desktop application that wraps the
zeroclaw AI agent in a fully-featured user-facing experience:

- A **Live2D avatar** that speaks every reply with your chosen
  voice and lip-syncs to the audio. Cross-language: chat in English,
  hear the avatar reply in Japanese (or any combination — the
  built-in subagent handles per-paragraph translation).
- A **desktop-pet mode** — a transparent, always-on-top, frameless
  window you can drag, snap to a screen edge, and talk to without
  ever opening the main app.
- A **character roster** so different personas (each with its own
  Live2D model and system prompt) are one click apart.
- A **Pulse dashboard** for ambient information feeds (RSS, Hacker
  News, anything you write a `Collector` for) backed by SQLite.
- Full **Tauri 2 desktop application** packaging on Windows, macOS,
  and Linux, with hardware-accelerated rendering and native audio
  output.

The companion is an **independent application** that talks to
vanilla zeroclaw over its public REST + SSE API. Nothing in this
repository modifies or forks zeroclaw — drop the companion next to
any compatible zeroclaw install and it works.

## Features

### Live2D avatar

- Renders Cubism 2 (`.moc` / `model.json`) and Cubism 4 (`.moc3` /
  `model3.json`) models via [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display)
- Hi-DPI rendering (`devicePixelRatio` + antialiasing + power-pref:
  high-performance) for clarity matching native viewers like
  Live2DViewerEX
- Per-model parameter sliders (28+ exposed for typical Cubism 2
  models — drive `PARAM_ANGLE_X`, `PARAM_BREATH`, `PARAM_MOUTH_*`
  etc. live)
- Idle motion auto-play, eye/face tracking from cursor or webcam,
  hit-area click → `Tap{Head,Body}` motion

### Desktop pet mode

- Transparent, frameless, always-on-top window
- Drag from anywhere on the avatar (works around PIXI's mousedown
  swallow via an explicit `start_dragging` Tauri command)
- Snap-to-edge with multi-monitor awareness; position persists
  across restarts
- Chromeless until hover — chat bar + corner buttons fade in only
  when the cursor is over the pet
- Compact chat bar in the overlay so you can talk to the avatar
  without opening the main window; main window mirrors history

### TTS port

- Generic wire contract — any model wrapper that speaks
  `POST /tts {text, language, voice?, speed?}` + `GET /health`
  works (GPT-SoVITS, Fish-Speech, MeloTTS, XTTS, F5-TTS, edge-tts,
  …)
- Reference Asuna v4 GPT-SoVITS wrapper at
  `tools/avatar/asuna_tts_server.py`
- Streaming sentence-chunked synthesis: first audio plays ~1s after
  the agent reply lands instead of waiting for the full reply
- Native rodio playback in Tauri (cpal → WASAPI multimedia) so the
  voice doesn't get the WebView2 "communications channel" AGC + echo
  cancellation treatment

### Subagent (translation + expression detection)

- Cheap LLM call that, per agent reply, returns clean chat-language
  text + translated TTS-language text + Live2D expression name +
  intensity
- Bypassed for short replies (single bulk call); per-paragraph
  translation for long ones (avoids z.ai-style 60s connection
  timeouts on big inputs)
- Two backends: direct OpenAI-compatible (`api_key` /
  `api_key_env`) for speed, or routed through zeroclaw's webhook to
  reuse its already-decrypted provider key
- Strips agent thinking-trace preamble ("Let me check...", "The
  user said...") before TTS so leaked scratchpad never reaches the
  user

### Chat / TTS language decoupling

Chat with the agent in English, have the avatar speak Japanese.
Set `[avatar] chat_language` and `[avatar.tts] language`
independently in `companion.toml`.

### Character management

Each character bundles `{name, model_id, system_prompt}`.
Switching the active character:

- Swaps the live2d model on the canvas
- Prepends the character's `system_prompt` to every user message
  before companion-server forwards it to zeroclaw — so different
  personas don't require touching zeroclaw's config

### Pulse dashboard

SQLite-backed feed of items from RSS/Atom feeds and Hacker News
with a per-collector "Run now" trigger. Extensible via the
`Collector` trait.

### Settings page

Editable from the UI, persisted to `companion.runtime.json`:

- Subagent backend (direct LLM ↔ zeroclaw webhook), API key,
  model, base URL
- Live2D model picker (anything under `web/public/live2d/models/`)
- Server URL (override for remote companion-server)

## Architecture

```
   user ─chat─▶ zeroclaw  ─────SSE /api/events ──▶ companion
                  ▲                                    │
                  └──────POST /api/chat (proxy) ◀──────┤
                                                       ▼
                                          companion subagent
                                          (clean + translate + emote)
                                                       │
                                                       ▼
                                          TTS port  (POST /tts)
                                                       │
                                                       ▼
                                       Live2D viewer (WS /ws/avatar)
```

In Tauri mode, `companion-tauri` spawns `companion-server` as a
sidecar and renders the web UI in a native WebView2 window. On
exit, Tauri sends `POST /api/shutdown` to the sidecar; the sidecar
runs `tts.stop_server()` (which POSTs `/shutdown` to the Python
TTS, runs `torch.cuda.empty_cache()` + `synchronize()` then
`os._exit(0)`) before exiting itself. Only after this graceful
chain — or after a 12s timeout — does Tauri fall back to
`TerminateProcess`. This avoids the GPU-driver fragmentation that
hard-killing CUDA processes leaves behind.

`zeroclaw` is **never** spawned or killed by the companion — only
queried. A red banner appears in the main window when zeroclaw is
unreachable; you start it yourself and the banner clears on the
next 30s poll.

## Prerequisites

- **Rust 1.88+** (`rustup show`)
- **Node.js 20+** (`node -v`) for the web bundle build
- **`cargo install tauri-cli@^2`** if you want the desktop app
- **zeroclaw** running somewhere reachable
  (default `http://127.0.0.1:8080`; this repo is configured for
  `42617` — adjust `[zeroclaw] url` in `companion.toml`)
- **Optional:** GPT-SoVITS + Python (the included Asuna wrapper
  reads its model path from the `TTS_MODEL_PATH` env var, which
  `companion-server` derives from `[avatar.tts] model_path` in
  `companion.toml` — point that at your GPT-SoVITS checkout root)
- **Optional:** an OpenAI-compatible LLM key for the subagent
  (only needed when `[avatar] chat_language != [avatar.tts]
  language`)

Platform deps for Tauri: see <https://tauri.app/start/prerequisites/>.
On Windows, that's WebView2 (preinstalled on Windows 11 + recent
Windows 10).

## Quickstart — desktop app

```bash
git clone https://github.com/Wty2003328/zeroclaw-companion
cd zeroclaw-companion

# Configure
cp companion.toml.example companion.toml
$EDITOR companion.toml   # set zeroclaw URL, TTS engine, etc.

# Drop a Live2D model
#   web/public/live2d/models/<name>/<name>.model3.json   (Cubism 4)
#   web/public/live2d/models/<name>/model0.json          (Cubism 2)
# Sample models: https://www.live2d.com/en/learn/sample/
# Pick which one is default in companion.toml [avatar.model] model_dir.

# Build the web bundle + the server binary
cd web && npm install && npm run build && cd ..
cargo build -p companion-server --release

# Build + run the Tauri shell (it bundles the server as a sidecar
# automatically via build.rs)
cd apps/companion-tauri
cargo tauri build --no-bundle      # debug-ish: skip MSI/DMG packaging
./target/release/companion-tauri.exe   # or .app / equivalent on Linux
```

Open the **Settings** tab in the running app to:

1. Swap the active **Live2D model**
2. Configure the **subagent backend** (paste your LLM key once;
   stored in `companion.runtime.json` next to `companion.toml`)
3. Toggle **Pet mode** (🪟 Show pet) for the always-on-top
   transparent overlay

The **Characters** tab lets you create, switch, and delete personas.

## Quickstart — server only (browser)

If you don't want the desktop shell:

```bash
# Build server + web bundle as above, then run:
cargo run --release -p companion-server
# → http://127.0.0.1:9181/
```

## Configuration

`companion.toml` (sample at `companion.toml.example`):

```toml
[zeroclaw]
url           = "http://127.0.0.1:42617"   # vanilla zeroclaw daemon
timeout_secs  = 300                         # generous — agent loops can run long

[server]
host          = "127.0.0.1"
port          = 9181

[avatar]
enabled       = true
chat_language = "en"                        # what the user types in
                                            # If different from tts.language,
                                            # the subagent translates per-reply.

[avatar.tts]
engine          = "gpt-sovits-v4"
api_url         = "http://127.0.0.1:9880"
port            = 9880
language        = "ja"                      # what the avatar SPEAKS
voice           = "asuna"
auto_start      = true                      # let companion-server own
                                            # the TTS lifecycle (graceful
                                            # shutdown on exit)
launch_command  = "python tools/avatar/asuna_tts_server.py"
model_path      = "/path/to/GPT-SoVITS"     # forwarded as TTS_MODEL_PATH
gpu_device      = 0
streaming       = true                      # sentence-chunked synthesis

[avatar.subagent]
enabled              = true
use_zeroclaw_webhook = true                  # or false to call LLM directly
only_when_translating = true                 # skip subagent when chat = tts language

[avatar.model]
model_dir          = "/live2d/models/asuna/model0.json"
default_expression = "F_NOMAL"
scale              = 0.2
anchor             = "center"
```

### Per-machine overrides — `companion.runtime.json`

Created automatically by the Settings UI for things like API keys
and subagent backend choice:

```json
{
  "subagent": {
    "use_zeroclaw_webhook": false,
    "api_key": "<your-key>",
    "model": "glm-4.5-flash",
    "base_url": "https://api.z.ai/api/coding/paas/v4"
  }
}
```

This is `.gitignore`d. The companion server reads it after
`companion.toml` and overlays the values.

### Characters — `companion.characters.json`

Created by the Characters page. Sibling of `companion.toml`.

```json
{
  "active_id": "asuna",
  "characters": [
    {
      "id": "asuna",
      "name": "Asuna",
      "model_id": "asuna",
      "system_prompt": "You are Yuuki Asuna from SAO. Speak warmly..."
    }
  ]
}
```

## Project layout

```
zeroclaw-companion/
├── crates/
│   ├── companion-core/      shared: zeroclaw client, SSE bridge, LLM client, config
│   ├── companion-avatar/    Live2D pipeline: TTS port, subagent, lip sync, WS handler
│   └── companion-pulse/     Pulse dashboard (SQLite store + RSS/HN collectors)
├── apps/
│   ├── companion-server/    binary: serves the web bundle + WS routes + REST API
│   └── companion-tauri/     Tauri 2 desktop shell (bundles companion-server)
├── web/
│   ├── src/
│   │   ├── pages/           Avatar, Characters, Settings, Pulse, Home
│   │   ├── components/      Live2DViewer, AvatarControls, useAvatarSocket
│   │   └── lib/             apiBase, characters, models, petWindow, webcamTracker
│   └── public/live2d/       Live2D model assets (Cubism 2 + 4 supported)
├── tools/avatar/            reference TTS wrappers (Asuna v4 GPT-SoVITS)
├── scripts/                 e2e Playwright + websocket tests, smoke probes
├── docs/                    architecture / migration / E2E smoke notes
├── companion.toml.example
└── README.md
```

## Development

```bash
# Workspace
cargo check --workspace
cargo test  --workspace          # 80+ unit + integration tests
cargo clippy --workspace --all-targets

# Web dev mode (proxies API + WS to companion-server on :9181)
cd web && npm run dev            # http://127.0.0.1:5173

# Tauri dev mode (hot reloads web changes; sidecar still cargo-rebuilt manually)
cd apps/companion-tauri && cargo tauri dev
```

`apps/companion-tauri/build.rs` automatically copies the freshest
`target/release/companion-server` into `binaries/` on every Tauri
build, so a `cargo build -p companion-server --release` followed by
`cargo tauri build` always ships the latest sidecar.

## Testing

End-to-end Playwright + websocket suites under `scripts/`. Each is
runnable in isolation against a `companion-server` listening on
`:9181`:

| Suite | Coverage |
|---|---|
| `e2e_canvas_prefs_test` | rotation/mirror/bg-image/idle-motion/eye-tracking pref round-trip |
| `e2e_pet_chrome_test` | hover-reveal chrome in pet mode |
| `e2e_overlay_drag_test` | `data-tauri-drag-region` attributes correctly applied |
| `e2e_drag_to_translate_test` | drag in main moves model; drag in overlay drags window |
| `e2e_model_swap_test` | `/api/models` + Settings model picker |
| `e2e_param_sliders_test` | Live2D parameter slider round-trip |
| `e2e_webcam_tracking_test` | webcam toggle + camera lifecycle |
| `e2e_characters_test` | character CRUD + activate + page render |
| `e2e_browser_test` | full chat round-trip (needs zeroclaw + TTS) |
| `e2e_reload_test` | history persists across reload |
| `e2e_overlay_chat_test` | overlay-typed chat reaches main window |
| `e2e_multi_window_test` | main + overlay don't clobber localStorage |
| `e2e_subagent_test` | subagent translation + chunking pipeline |
| `e2e_audio_inspect.py` | audio chunk fingerprinting (no duplication) |

```bash
# Run a single suite
python scripts/e2e_characters_test.py   # any Python with playwright + websocket-client

# Or a full sweep — see scripts/smoke.sh
./scripts/smoke.sh
```

Rust tests:

```bash
cargo test --workspace                                          # all
cargo test -p companion-server --release                        # server + characters module
cargo test -p companion-avatar -- --test-threads=1              # avatar pipeline
```

## Subsystem status

| Subsystem            | Status                                                  |
|----------------------|---------------------------------------------------------|
| companion-core       | ✓ zeroclaw client, LLM client, config, runtime overlays |
| companion-avatar     | ✓ TTS port, subagent (per-paragraph translation), expression mapping, WS handler, parameter API |
| companion-pulse      | ✓ SQLite store, RSS + HN collectors, REST API           |
| companion-tauri      | ✓ desktop shell, sidecar lifecycle, native rodio audio, drag/snap pet window |
| Asuna v4 TTS wrapper | ✓ `tools/avatar/asuna_tts_server.py` with graceful CUDA shutdown |
| Character management | ✓ JSON persistence + per-character system prompt + model swap |

## Troubleshooting

### Games stutter for 30–90s after closing the companion

Used to be the default behavior — `TerminateProcess` on the Python
TTS skipped `torch.cuda.empty_cache()` and left the GPU driver
fragmented. Fixed: companion now does a graceful shutdown chain
(Tauri → `/api/shutdown` → companion-server → `/shutdown` →
Python). If it ever regresses, check that:

1. `[avatar.tts] auto_start = true` and `launch_command` points at
   the wrapper (so companion owns the TTS lifecycle)
2. The Python wrapper has the `shutdown_cleanup()` helper at the
   bottom (look for the `@app.post("/shutdown")` handler)
3. `PYTORCH_NO_CUDA_MEMORY_CACHING=1` is set in the wrapper's env

### Chat fails with "zeroclaw unreachable"

Companion never starts zeroclaw — start it yourself
(`zeroclaw gateway start` or however your install is set up). The
red banner across the top of the app re-checks every 30s.

### Subagent times out on long replies

z.ai's coding-paas endpoint has a 60s connection budget per call.
Companion automatically falls back to per-paragraph translation
for inputs > 500c. If your provider is consistently slow, switch
to a different one in **Settings → Subagent backend**.

### Pet window doesn't drag

Click directly on the avatar (the rendered Asuna pixels). Empty
transparent areas of the canvas don't capture the mousedown.
If clicks still don't drag, ensure `data-tauri-drag-region` is
on the canvas — `scripts/e2e_overlay_drag_test.py` verifies this
and is a regression test for the fix.

## Contributing

PRs welcome. Useful entry points:

- New TTS engine? Write a Python (or anything) wrapper that speaks
  the wire contract in `crates/companion-avatar/src/tts_server.rs`'s
  module docstring. Point `[avatar.tts] launch_command` at it.
- New Live2D model? Drop the directory under
  `web/public/live2d/models/` and it'll appear in the model picker.
- New collector for Pulse? Implement `Collector` in
  `crates/companion-pulse/src/collectors.rs`.

Run the test sweep before opening a PR:

```bash
cargo test --workspace
./scripts/smoke.sh
```

## License

This project is licensed under either of, at your option:

- **Apache License, Version 2.0** ([LICENSE-APACHE](LICENSE-APACHE) or
  <https://www.apache.org/licenses/LICENSE-2.0>)
- **MIT License** ([LICENSE-MIT](LICENSE-MIT) or
  <https://opensource.org/licenses/MIT>)

SPDX-License-Identifier: `MIT OR Apache-2.0`

### Contribution

Unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in the work by you, as defined in the
Apache-2.0 license, shall be dual-licensed as above, without any
additional terms or conditions.

### Third-party assets

This repository's source code is dual-licensed as above. **Live2D
model assets** under `web/public/live2d/models/` are NOT covered by
that license — each model is the property of its original author and
licensed separately. The repository ships without any models; you
provide your own and accept the model author's terms when you do.
The Cubism SDK sample models (Haru, Hiyori, Mark, Wanko, etc.) are
licensed by Live2D Inc. under the
[Live2D Free Material License](https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html)
for individual use and small-team commercial use; verify the terms
before redistributing. The Cubism SDK runtime files
(`live2d.min.js`, `live2dcubismcore.min.js`) are similarly subject
to Live2D's distribution terms — they live under
`web/public/live2d/` and are gitignored by default.
