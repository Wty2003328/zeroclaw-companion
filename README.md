# zeroclaw-companion

Live2D avatar + Pulse dashboard for [zeroclaw](https://github.com/zeroclaw-labs/zeroclaw),
running as a sidecar.

**Why a separate repo?** Embedding these features in a fork of zeroclaw made every
upstream release a multi-day rebase. The companion talks to vanilla zeroclaw over its
public REST + SSE API instead — zero patches, no version coupling, much easier to track.

## What you get

- **Live2D anime avatar** with TTS + lip sync + expression mapping
- **Generic TTS port**: any model wrapper that speaks `POST /tts` + `GET /health`
  works (GPT-SoVITS, Fish-Speech, MeloTTS, XTTS, F5-TTS, edge-tts, …)
- **Chat / TTS language decoupling**: chat with the agent in English, have the avatar
  speak Japanese. The avatar subagent translates each reply via a cheap LLM call.
- **Pulse dashboard**: SQLite-backed feed of items from RSS/Atom feeds and Hacker News
  with a per-collector "Run now" trigger. Extensible via the `Collector` trait.

## Architecture

```
   user ─chat─▶ zeroclaw  ─────SSE /api/events ──▶ companion
                  ▲                                    │
                  └──────POST /api/chat (proxy)────────┤
                                                       ▼
                                          companion subagent
                                          (translate + emote)
                                                       │
                                                       ▼
                                          TTS port  (POST /tts)
                                                       │
                                                       ▼
                                       Live2D viewer (WS /ws/avatar)
```

The companion never modifies zeroclaw. It runs as its own process on its own port,
subscribes to zeroclaw's event stream, and drives the avatar pipeline locally.

## Quickstart

### 1. Run zeroclaw (vanilla, no fork required)

```bash
# install zeroclaw following its own README
zeroclaw gateway start
# listens on http://127.0.0.1:8080
```

### 2. Run companion-server

```bash
git clone https://github.com/Wty2003328/zeroclaw-companion
cd zeroclaw-companion

# config
cp companion.toml.example companion.toml
$EDITOR companion.toml

# build the web bundle
cd web && npm install && npm run build && cd ..

# build + run the server
cargo run --release -p companion-server
# listens on http://127.0.0.1:9181
```

Open http://127.0.0.1:9181/ — you should see the companion home page with a green
status indicator for upstream zeroclaw.

### 3. Optional: anime voice via GPT-SoVITS v4 (Asuna example)

```bash
# point launch_command at this script in companion.toml; the companion
# auto-starts it on boot (or run it yourself)
python tools/avatar/asuna_tts_server.py
```

See `companion.toml.example` for the full Asuna configuration block, and
`tools/avatar/asuna_tts_server.py` for the v4 LoRA inference pipeline.

## Layout

```
zeroclaw-companion/
├── crates/
│   ├── companion-core/      shared: zeroclaw client, SSE bridge, LLM client, config
│   ├── companion-avatar/    Live2D pipeline: TTS port, subagent, lip sync, WS handler
│   └── companion-pulse/     Pulse dashboard (stub — migration in progress)
├── apps/
│   └── companion-server/    binary: serves the web bundle + WS routes
├── web/                     Vite + React frontend
├── tools/avatar/            reference TTS wrappers (Asuna v4 GPT-SoVITS)
├── docs/                    architecture / migration notes
└── companion.toml.example
```

## Development

```bash
# Workspace-level
cargo check --workspace
cargo test --workspace          # 80+ unit + integration tests
cargo clippy --workspace --all-targets

# Frontend (with hot reload + proxy to companion-server)
cd web && npm run dev    # http://127.0.0.1:5173
```

## Smoke check a running stack

```bash
./scripts/smoke.sh        # Linux / macOS / git-bash
.\scripts\smoke.ps1       # Windows PowerShell
```

Probes zeroclaw `/health`, companion `/health` + `/api/status`, the TTS
port `/health` and a real `/tts` round trip, plus Pulse if enabled.
Tells you exactly which layer is down. Step-by-step manual recipe with
expected log output and a failure-tree table is in
`docs/E2E-SMOKE-TEST.md`.

## Status

| Subsystem            | Status                                                  |
|----------------------|---------------------------------------------------------|
| companion-core       | working (zeroclaw client, LLM, config)                  |
| companion-avatar     | working (TTS port, subagent, expression, WS handler)    |
| companion-pulse      | working (SQLite store, RSS + HN collectors, REST API)   |
| Asuna v4 TTS wrapper | working (`tools/avatar/asuna_tts_server.py`)            |
| Tauri desktop shell  | working (`apps/companion-tauri/`, bundles companion-server when packaged) |

## License

MIT OR Apache-2.0
