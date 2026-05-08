# End-to-end smoke test

This walks through verifying the full pipeline against a real upstream
zeroclaw daemon and a real TTS server. It's the manual companion to the
automated unit + integration tests (`cargo test --workspace`, 80+ tests
covering individual components in isolation).

## What you're checking

```
   user types        →   zeroclaw    →   SSE event   →    companion   →   TTS port  →   audio
   "Hi Asuna"           (any model)      agent.reply       subagent        (Asuna v4)     in browser
                                                           translate +
                                                           pick face
```

If the user types in English and the avatar speaks Japanese, all five
boxes in that diagram are working — the subagent translation, the SSE
bridge, the TTS port, and the WebSocket frame ordering.

## Prerequisites

- `zeroclaw` upstream daemon, configured with at least one provider
  + model + an API key it can actually reach.
- `companion-server` built (`cargo build --release -p companion-server`).
- Web bundle built (`cd web && npm install && npm run build`).
- An OpenAI-compatible API key for the avatar subagent (used for
  translation + expression analysis). Fastest options: OpenAI's
  `gpt-4o-mini`, OpenRouter's free tier, or Ollama on `localhost:11434/v1`.
- *Optional but recommended:* the Asuna GPT-SoVITS v4 wrapper or any
  other TTS server that speaks the `/tts` + `/health` contract.

## Step 1 — boot zeroclaw

In one terminal:

```bash
zeroclaw gateway start
# expected: "listening on http://127.0.0.1:8080"
```

Verify it's reachable:

```bash
curl -s http://127.0.0.1:8080/health    # → "ok"
```

## Step 2 — write `companion.toml`

Copy the example and edit. Minimum viable config for a same-language
smoke test (chat in English, avatar speaks English):

```toml
[zeroclaw]
url = "http://127.0.0.1:8080"

[server]
port = 9181

[avatar]
enabled = true
chat_language = "en"

[avatar.tts]
engine = "edge-tts"
api_url = "http://127.0.0.1:9880"   # whatever your TTS exposes
language = "en"
voice = "en-US-AriaNeural"
auto_start = false                  # leave false; start TTS manually

[avatar.subagent]
enabled = true

[avatar.subagent.llm]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
model = "gpt-4o-mini"
```

For the cross-language case (chat in English, Asuna speaks Japanese):

```toml
[avatar]
chat_language = "en"     # subtitles + LLM reasoning

[avatar.tts]
engine = "gpt-sovits-v4"
launch_command = "python tools/avatar/asuna_tts_server.py"
auto_start = true
language = "ja"          # what the avatar speaks
voice = "asuna"
reference_audio = "/abs/path/to/asuna_reference.wav"
reference_text  = "ここは私に任せて私を選んでくれる"
reference_language = "ja"
model_path = "/abs/path/to/GPT-SoVITS"
gpu_device = 0
```

## Step 3 — boot the TTS server

If `auto_start = false`, start it yourself in a separate terminal:

```bash
# generic example
python my_tts_server.py    # binds 127.0.0.1:9880

# Asuna v4 example
COMPANION_REPO=/path/to/zeroclaw-companion
python "$COMPANION_REPO/tools/avatar/asuna_tts_server.py"
```

Verify:

```bash
curl -s http://127.0.0.1:9880/health    # → 200, {"status":"ok",...}

# Direct synthesis test (bypasses companion + zeroclaw):
curl -X POST http://127.0.0.1:9880/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"hello","language":"en"}' \
  --output /tmp/check.wav
file /tmp/check.wav    # → "RIFF (little-endian) data, WAVE audio"
```

## Step 4 — boot companion-server

```bash
cd /path/to/zeroclaw-companion
COMPANION_CONFIG=$(pwd)/companion.toml ./target/release/companion-server
```

Expected log lines (with `RUST_LOG=info`):

```
companion: zeroclaw at http://127.0.0.1:8080 is up
companion: avatar enabled (chat_lang=en, tts_lang=ja, engine=gpt-sovits-v4)
companion: avatar subagent ready (model=gpt-4o-mini)
companion: SSE bridge connected
companion: listening on http://127.0.0.1:9181
```

## Step 5 — verify the public surface

```bash
curl -s http://127.0.0.1:9181/health         # → "ok"
curl -s http://127.0.0.1:9181/api/status | jq
# {
#   "ok": true,
#   "zeroclaw_up": true,
#   "avatar_enabled": true,
#   "pulse_enabled": false
# }
```

If `zeroclaw_up: false`, your `[zeroclaw] url` is wrong or the daemon
isn't running.

## Step 6 — open the avatar UI

Browse to `http://127.0.0.1:9181/avatar`. You should see:

- A loading state, then the Live2D model (Haru by default — drop your own
  model under `web/public/avatar/models/<name>/` and point
  `[avatar.model] model_dir` at it).
- "Connected to companion" indicator at the bottom.

## Step 7 — drive the pipeline

In the chat input, type **"Hi, how are you today?"** and hit Send.

Expected behavior:

1. `companion-server` logs:
   `POST /api/chat → echo'd to zeroclaw`
2. `zeroclaw` logs:
   The agent generates a reply in English.
3. `companion-server` logs:
   `avatar subagent: expression=F05 translated=true`
4. `companion-server` logs:
   `avatar: TTS synthesize OK (X bytes)`
5. The browser:
   - Avatar's expression changes (smile/F05 for a friendly reply).
   - Subtitle shows the English reply.
   - Audio plays — *in Japanese* if you set `tts.language = "ja"`.
   - Mouth moves while audio plays.

## Step 8 — Pulse smoke test (optional)

If `[pulse] enabled = true`:

```bash
# Trigger an immediate RSS run
curl -X POST http://127.0.0.1:9181/api/pulse/trigger/rss

# A few seconds later, check the feed
curl -s "http://127.0.0.1:9181/api/pulse/feed?limit=5" | jq '.items | length'
# → should be > 0 if any of your feeds have items
```

Or open `http://127.0.0.1:9181/pulse` and click "Run now" on the RSS card.

## Failure tree

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/status` shows `zeroclaw_up: false` | Wrong URL / daemon down | Check `[zeroclaw] url` and that `curl http://127.0.0.1:8080/health` returns `ok` |
| Subtitle appears, no audio | TTS port unreachable | `curl http://127.0.0.1:9880/health` — fix that first |
| Audio plays but in the wrong language | Subagent disabled or returned no `translated_text` | Set `[avatar.subagent] enabled = true` and verify the LLM endpoint works (`curl ... /v1/chat/completions`) |
| Avatar UI sticks on "Connecting…" | WS upgrade failing | Browser console: look for `WebSocket connection to 'ws://...:9181/ws/avatar' failed`. Usually a CSP issue when loading from `file://` — load via `http://127.0.0.1:9181/` instead |
| Subagent times out every reply | LLM API key invalid or model name wrong | `RUST_LOG=companion=debug` and look for `subagent: LLM call failed` — the underlying error is logged |
| Pulse feed empty after Run now | Collector errored | `curl http://127.0.0.1:9181/api/pulse/status \| jq '.runs[0]'` — the `error` field tells you why |

## Automated bits

Most of the above is exercised by `cargo test --workspace`:

- **TTS port wire contract** (`crates/companion-avatar/tests/tts_port_e2e.rs`,
  6 tests): boots a mock TTS server on a random port, drives
  `synthesize_with`, asserts the JSON body shape, the response header
  parsing (`X-Sample-Rate`, `X-Channels`, `X-Format`), the default
  fallbacks when headers are missing, and error propagation.
- **Zeroclaw client + SSE bridge** (`crates/companion-core/tests/zeroclaw_client_e2e.rs`,
  5 tests): boots a mock zeroclaw on a random port, asserts `/health`,
  `/api/chat` reply parsing, SSE event classification (`agent.reply`,
  `agent.token`, `role:assistant + final:true`).
- **LLM client** (`crates/companion-core/tests/llm_client_e2e.rs`, 3
  tests): mocks `/v1/chat/completions`, verifies normal + 5xx + malformed
  response paths.
- **Companion server boot** (`apps/companion-server/tests/http_smoke.rs`,
  3 tests): spawns the actual binary, hits `/health`, `/api/status`,
  asserts pulse routes 404 when disabled.

What `cargo test` *can't* cover:
- Real zeroclaw running a real LLM and emitting real SSE events.
- A live TTS model producing real audio.
- The browser's WebSocket round trip + `pixi-live2d-display` rendering.

Those are what this manual checklist exists for.
