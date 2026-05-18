# Testing SOP — waifu-companion

> The cargo unit suite catches type-level bugs; the wire rigs catch
> contract drift; **only the running-binary lifecycle test catches
> bugs like the adopted-sidecar shutdown leak (iter 12)**, where the
> code compiles, every unit test passes, and the bug only manifests
> when a real process is asked to actually exit and orphans its
> children. This document is the layered protocol. Run every layer
> before saying "this works".

The layers ascend from cheap-and-narrow to slow-and-realistic. Each
upper layer assumes the ones below it have already passed.

## L1 — Compile and lint (≈30 s)

```bash
cd zeroclaw-companion
cargo check --workspace
cargo clippy --workspace --all-targets -- -D warnings
cd web && npx tsc --noEmit
```

**Pass:** no warnings, no errors. `clippy -D warnings` makes lints
fatal — never bypass with `#[allow]` without a one-line `// reason: …`.

## L2 — Unit + crate-integration (≈30 s)

```bash
cargo test --workspace --no-fail-fast
```

**Pass:** 130 tests, 0 failed. Drift from this baseline means
something landed under-tested; either a removed test or a flake — find
out which.

Key test files:

- `crates/companion-core/src/config.rs::tests` — runtime-override
  apply() shape + alias back-compat.
- `crates/companion-avatar/src/config.rs::tests` — avatar config
  serde shape; **includes the example-file regression test** that
  fails CI if `companion.toml.example` drifts from the live schema.
- `crates/companion-avatar/tests/translator_port_e2e.rs` — translator
  HTTP wire + adopted-shutdown lifecycle (the bug class fixed in iter
  12).
- `crates/companion-avatar/tests/tts_port_e2e.rs` — TTS HTTP wire +
  manager construction.
- `apps/companion-server/tests/http_smoke.rs` — companion-server
  spawned as a subprocess, /health + /api/status + 404 fallthrough.

## L3 — Wire-contract rigs (Python sidecars) (≈30 s each)

These rigs spin up the real Python sidecar (no Rust involved) and
hit the documented `/health`, `/translate` or `/tts`, and `/shutdown`
endpoints. They guard against the sidecar quietly breaking its
contract — the kind of drift that only surfaces when a Rust call
suddenly fails in production.

**Pre-req:** the sidecar's target port must be free. The rigs refuse
to run if their port is bound (so you never accidentally test against
a stale instance).

```bash
# TTS HTTP wire
/e/miniconda/envs/tts/python.exe tts_tools/test_server_e2e.py
#   port:    TTS_PORT (default 9880)
#   asserts: /health, /tts default, /tts sample_steps override,
#            empty-text → 400, /shutdown → exit 0

# NMT HTTP wire
/e/miniconda/envs/tts/python.exe tts_tools/test_nmt_e2e.py
#   port:    NMT_PORT (default 9881)
#   asserts: /translate en→ja for 4 sentence shapes, latency,
#            empty-text → 400, wrong-langs → 400, /shutdown → exit 0
```

**Pass:** "all checks green" / exit 0.

## L4 — Multi-service audio integrity (≈2 min, GPU)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_audio_integrity.py
#   spins: mock zeroclaw (42617) + real TTS sidecar (9880)
#          + companion-server (19182)
#   sends 4 multi-sentence canned replies through the full
#   chat → translate → synthesize → WS-frame pipeline
#   asserts each turn's total audio ≥ 65% of expected duration
```

**Pre-reqs:** ports 9880, 42617, 19182 free. **Requires GPU.**

**Pass:** all four cases ≥ 65% of expected duration. Per-segment
GIVE UP / "AR consistently truncating" warnings on specific inputs
(`おはようございます`, `お散歩しませんか？`) are a documented
known-limitation — they fire but the turn still passes because
surrounding sentences carry the duration.

## L5 — Running-binary lifecycle (≈30 s)  ← the layer that catches multi-process bugs

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_lifecycle.py
```

This is the layer the cargo suite cannot replace. It:

1. Spawns `target/release/companion-server.exe` as a real subprocess.
2. Spawns mock TTS + NMT sidecars whose `/health` keeps returning
   200 even after `/shutdown` is hit — simulating the "wedged
   adopted Python" case.
3. Drives a chat turn so the managers actually fire up and adopt
   the mocks.
4. Hits `POST /api/shutdown` and waits ≤ 12 s for the binary to exit.
5. Asserts the binary exited within the grace window (no hang).
6. Asserts the companion-server logs contain the
   `"adopted ... still responding ... process leaked"` warning —
   proving the orphan-detection branch fired instead of silently
   returning Ok.

**Pre-reqs:** `target/release/companion-server.exe` is **fresh**
(rebuilt after any Rust change to TTS/NMT/lifecycle code), ports
9881, 9880, 19182, and the test mock-zeroclaw port free.

**Pass:** the rig prints `PASS — lifecycle clean`.

**Why this layer matters**: in iter 12 a user-reported NMT leak on
app quit traced back to `stop_server()` being a no-op on the adopted
path (probe_health hit → no `self.child` → kill fallback unreachable).
Cargo tests didn't catch it because they don't exercise the actual
spawn → adopt → POST /api/shutdown → wait-for-exit cycle through
the real binary. **Every change that touches the TTS or NMT subprocess
lifecycle MUST run L5.**

## L6a — Backend HTTP coverage (≈10 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_backend_api.py
```

Boots the full mock stack (mock zeroclaw + TTS + NMT + companion-server
with avatar AND pulse enabled, against a temp config so the user's
characters.json is never touched), then hits every documented endpoint
with happy and edge-case inputs:

- `/health`, `/api/status` shape
- `GET /api/config` with all TTS override keys present (locks in the
  iter-2 streaming-fields + iter-10 schema-drift fix)
- `POST /api/config/{avatar,subagent,zeroclaw}` persist + apply
- `tts_speed` clamps to [0.25, 3.0]
- `POST /api/chat` round-trips through the mock
- `/api/characters` full CRUD + active-id lifecycle
- empty-id 400 / unknown-id 404
- `/api/characters/{id}/attachments` PUT/GET/LIST/DELETE (JSON shape)
- `/api/pulse/status`, `/api/pulse/feeds` CRUD, `/api/pulse/unread_count`
  (`{unread:int}`)
- SPA fallthrough (unknown /api/ path returns HTML, not bogus JSON)

**Pass:** 18/18 green. Add tests here when you add new endpoints —
the rig is one `_check(...)` line each.

## L6b — Frontend systematic coverage (≈15 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_frontend_e2e.py
```

A Playwright suite organized by axis — SMOKE / NAV / SETTINGS /
CHARACTERS / AVATAR / PULSE / PERSISTENCE / ERROR / A11Y — that boots
the same mock stack as L6a, then drives the UI:

- Every route renders without unfiltered console errors
- Top nav navigates between Home / Avatar / Pulse / Settings
- Settings → Avatar & voice: Voice speed change PERSISTS across reload
  (uses the React-controlled-input native-setter pattern; `el.value=`
  alone is silently no-op'd by React)
- Settings → Translation: all three ModeRadio options render + the
  Direct AI mode reveals the Service-configuration block
- Characters created via API are visible on Home
- Chat send → user bubble + assistant reply round-trip through the
  mock zeroclaw
- localStorage chat history persists across page reload
- Forced 500 on `/api/config/avatar` shows `saveErrorMessage()`'s
  helper text (iter-7 contract: "Avatar save failed — HTTP 500 …")
- The iter-3 ModeRadio focus halo is visible to keyboard users
  (asserts non-default outline OR `label:has() box-shadow`)

**Pass:** 16/16 green. Failure screenshots land in
`tts_samples/frontend_failures/<test-name>.png`.

This is the BASELINE coverage. When adding new pages / features,
add a corresponding axis-organized test here — don't bury it in
`scripts/e2e_*.py` (those are regression nets for specific past
bugs, not coverage).

## L6c — Regression-net e2e scripts (`scripts/e2e_*.py`)

The 21 scripts under `scripts/e2e_*.py` are NOT coverage — they're
bug-flavored regression nets, one per user-reported issue (chat
history reload bug, drag-to-translate, multi-window state, overlay
chrome, etc.). Run them when you've changed code that COULD revive
the bug they capture. Most need a richer stack than L6b — see the
config-matrix below.

```bash
# A representative subset of scripts/e2e_*.py — they drive Playwright
# against a running companion-server on :9181.
python scripts/e2e_characters_test.py    # CRUD on the roster
python scripts/e2e_canvas_prefs_test.py  # avatar canvas state
python scripts/e2e_subagent_test.py      # translation pipeline visible-in-UI
python scripts/e2e_chat_pipeline.py      # full chat-send round trip
```

**Env pre-req:** Playwright is not in the `tts` conda env (verified
in iter 12) — running these scripts straight from `python`
fails with `ModuleNotFoundError: No module named 'playwright'`. Set
up once:

```bash
python -m pip install playwright
python -m playwright install chromium
```

The chromium browser binary lands in `%LOCALAPPDATA%\ms-playwright\`
(~150 MB). If you'd rather keep that out of the TTS env, create a
dedicated `e2e` env for these scripts. **Skipping the install is
fine for unrelated changes — just record the skip in the report.**

**Service pre-req:** companion-server listening on `127.0.0.1:9181`
(with a **test** config — never against the user's daily setup,
that pollutes characters.json). Start one with:

```bash
COMPANION_CONFIG=/tmp/test.toml target/release/companion-server.exe
```

The `/tmp/test.toml` shape depends on which scripts you're running.
Most scripts assume a richer stack than the minimal characters-only
config; group them by config needs:

| Script | Needs avatar enabled | Needs zeroclaw reachable | Needs pulse | Notes |
|---|---|---|---|---|
| `e2e_characters_test.py` | no | no | no | minimal config OK |
| `e2e_canvas_prefs_test.py` | yes | no | no | drives /avatar canvas |
| `e2e_param_sliders_test.py` | yes | no | no | live2d parameter UI |
| `e2e_model_swap_test.py` | yes | no | no | swap active character |
| `e2e_subagent_test.py` | yes | yes (mocked OK) | no | translation pipeline |
| `e2e_chat_pipeline.py` | yes | yes | no | full chat round-trip |
| `e2e_reload_test.py` | yes | yes | no | needs prior chat history |
| `e2e_browser_test.py` | yes | yes | no | drives /avatar like a user |
| `e2e_overlay_chat_test.py` | yes | yes | no | pet window WebView |
| `e2e_pulse_test.py` | no | no | yes | pulse routes |
| `e2e_pulse_features_test.py` | no | no | yes | pulse UI |

For the "needs zeroclaw" rows: spin up a mock on the configured port
(see `tts_tools/test_audio_integrity.py::_mock_zeroclaw_script` for
a small FastAPI mock you can adapt) rather than pointing at a real
agent — the e2e shouldn't depend on a Pi being reachable.

**Pass:** each script exits 0. Failures here usually mean a UI
contract drifted from what the test expects (selector renamed,
endpoint moved, etc.) — fix the test alongside the change, never
delete it.

**Verified iter 12:** `e2e_characters_test.py` PASSES against a
freshly-built `target/release/companion-server.exe` driven by a
minimal characters-only config. Other scripts need their respective
config bundle (table above).

## L6d — Extended backend HTTP/WS coverage (≈30 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_backend_api_extended.py
```

Fills the holes in L6a:

- **WebSocket `/ws/avatar`** — handshake (Connected + ModelInfo),
  Audio frame shape after `/api/chat`, MotionRequest acceptance,
  reconnect produces a new session_id.
- HTTP method mismatches (GET `/api/chat`, GET `/api/avatar/asr`)
  return 4xx, never 200.
- CORS preflight `OPTIONS /api/chat` returns `Access-Control-Allow-Origin`.
- 50 parallel POST `/api/chat` requests all succeed.
- `/api/avatar/asr` with ~1 MB base64 payload works.
- Pulse extras: duplicate feed URL not double-inserted, 2 KB feed name
  doesn't crash, invalid URL gracefully rejected, unknown collector
  trigger doesn't 5xx, every documented `?limit/offset/source/search/unread`
  param parses.
- Character attachment: UTF-8 round-trip, path-traversal rejected
  (`../escape.md`, absolute paths), 200-char filename survives.
- **Persistence probe** — boots twice with the same scratch dir,
  asserts a `tts_speed` override survives a graceful restart.

**Pass:** 26/26 + persistence probe green.

## L6e — Integration (full pipeline) (≈3 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_integration_full.py
```

End-to-end wiring through the mock stack:

- Chat pipeline: `POST /api/chat` → mock zeroclaw → subagent → mock
  NMT → mock TTS → WS `Audio` frame arrives with correct base64 +
  lip-sync shape.
- Voice input: `POST /api/avatar/asr` → transcript → fed back into
  `/api/chat` → reply round-trips.
- Streaming TTS invariants: seq numbers are 0..N-1 contiguous, only
  the final frame has `last=true`, all share one `turn_id`.
- Cross-window broadcast: two WS clients both receive the
  `UserMessage` echo when `/api/chat` fires.

**Pass:** 4/4 green.

## L6f — Chaos / recovery (≈3 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_chaos.py
```

Drives the mock stack's control plane (`POST :9883/_set`) into known
failure modes and asserts graceful degradation:

- TTS forced 500 → `/api/chat` still returns the agent reply (TTS is
  best-effort on the broadcast side).
- TTS / NMT / speech **dead** → server stays alive, `/health` still 200.
- zeroclaw forced 500 → `/api/chat` returns 5xx (never 200 — would be
  lying about success).
- NMT slow 1.5 s within timeout → chat succeeds, absorbed the delay.
- Speech dead / 500 → `/api/avatar/asr` returns 5xx, not 200.
- **Recovery** — after clearing every chaos knob, `/api/chat` works
  again. Catches state the server held onto from a prior failure.

**Pass:** 8/8 green.

The chaos rig depends on `_mock_stack.py`'s control plane (port 9883).
That control plane is opt-in (`control_enabled=True` default); rigs
that don't need it can set `control_enabled=False` to save a port.

## L6f-prop — Property-based state-transition fuzz (≈20 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_state_transitions.py
/e/miniconda/envs/tts/python.exe tts_tools/test_state_transitions.py \
    --ops 500 --seed 42      # deterministic, larger walk
```

Random walk through `/api/chat`, `/api/avatar/asr`, `/api/config/*`,
character CRUD, pulse + WS endpoints — 200 ops by default. Asserts:

- `/health` returns 200 throughout.
- No 5xx from server-side code paths (sidecar 502/503 on garbage input
  is allowed — that's a gateway error, not a companion-server crash).
- WS connections cycle 20 times without session_id reuse.
- 15 concurrent WS subscribers all receive a `UserMessage` broadcast.

Heavy enough to be `quick=False` — only in `--full`. Fixed seed makes
repros deterministic.

**Pass:** 6/6 green.

## L6f-sec — Security / safety adversarial inputs (≈2 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_security.py
```

Hits the server with deliberately-malicious inputs. Pass criteria for
each: server stays alive, no 5xx, sensitive output not leaked.

- Path traversal on attachments: `../`, `..\\`, absolute, double-dot
  bypasses. URL-encoded variants get an advisory (the URL decoder
  runs before the filename validator — recommend hardening).
- Path traversal on character IDs: ids stored in a JSON manifest, not
  as filenames, so this is benign; verified.
- Header injection (CRLF in `X-Session-Id`) doesn't smuggle a second
  header, doesn't crash.
- 8 KB header doesn't crash.
- 4 MB JSON body doesn't crash.
- Unicode (emoji + RTL + CJK + ZWJ) survives `/api/chat` round-trip.
- SQL-meta chars (`'; DROP TABLE`) in pulse feed name + search don't
  break the SQLite layer.
- Attachment extension allowlist enforced (only `.md`).
- Control-char filenames rejected (NUL, LF, CR, tab, etc.).
- WS 256 KB junk frame doesn't crash the server.

**Pass:** 17/17 green.

## L6sse — SSE bridge invariants (≈3 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_sse_bridge.py
```

The companion subscribes to upstream zeroclaw's `/api/events` SSE
stream. This rig verifies the bridge:

- Mock zeroclaw's `/api/events` endpoint is reachable + `event-stream`
  content-type.
- `/api/status` keeps responding while subscribed (no backpressure).
- `/api/chat` works concurrently with an in-flight SSE stream.
- After `zc_dead` chaos toggle, recovery completes within watchdog
  tick + 2s — `/api/chat` works again.
- 5 transient SSE subscriptions don't leak file descriptors.
- Watchdog handling of upstream 4xx doesn't panic the classifier.
- 10-chat storm doesn't break SSE upstream.

**Pass:** 7/7 green.

## L6flows — Extended UI flows + multi-viewport visual baselines (≈45 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_frontend_flows.py
```

Goes deeper than L6g:

- Avatar canvas mouse-click → Touch WS event (smoke).
- Multi-tab chat history sync via second browser context.
- Empty-message Enter doesn't send a chat.
- Loading state present during /api/chat round-trip.
- 3-message chat history persists across reload.
- Invalid URL in settings doesn't kill the server.
- Network-offline simulation → recovery once online.
- Visual baselines: home / avatar / settings / pulse / overlay, plus
  mobile 375px and wide 1920px. 2% pixel-diff tolerance; first run
  writes baselines.

Visual baselines live in `tts_samples/visual_baselines/`. Failing
diff also writes `*_LATEST.png` so you can inline-compare.

**Pass:** 16/16 green (in baseline mode the first run trivially
passes; subsequent runs do the comparison).

Excluded from `--quick` because baseline comparisons are
environmental — DPI / font hinting / mock-stack timing can drift
the pixel diff above the 2% threshold across machines. Run on a
canonical CI / dev host with `--full` so the baselines are stable.

## L6perf — Performance regression guard (advisory in --full)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_perf_regression.py
/e/miniconda/envs/tts/python.exe tts_tools/test_perf_regression.py \
    --tolerance 2.0
```

Reads every CSV under `tts_samples/bench/`, computes per-metric
median across history, runs a fresh bench, fails if any new metric
exceeds 1.5× historical median. Skipped automatically when fewer
than 2 historical runs exist.

**Pass:** every metric within tolerance.

Excluded from `--quick` because the cold-start measurement boots
companion-server twice (≈10 s extra wallclock).

## L6report — HTML report rendering (`render_report.py`)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/render_report.py
/e/miniconda/envs/tts/python.exe tts_tools/render_report.py \
    tts_samples/run_all/20260515_191218
```

Takes a `run_all` output directory (or auto-picks the latest) and
emits `report.html` next to its `summary.md`:

- Per-suite cards with PASS/FAIL badges + expandable log panes.
- Bench-latency time-series charts (inline SVG, one per metric).
- Visual baselines grid (inline base64-PNG thumbnails).
- Histogram of suite durations.

Self-contained — no external CSS/JS — suitable as a CI artifact.
`run_all.py` calls it automatically at the end of every run unless
`--no-report` is passed.

## L6g — Extended frontend coverage (≈30 s)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/test_frontend_e2e_extended.py
```

Playwright extensions on top of L6b:

- `settings_avatar_form_round_trip` — Voice speed + CFM steps both
  persist in one save cycle.
- `settings_translation_persist` — radio mode persists across reload.
- `settings_subagent_toggle_persists` — checkbox state survives reload.
- `characters_create_and_activate_via_ui` — drive create through UI
  buttons (modal), confirm via API the character exists.
- `websocket_reconnect_after_close` — reload page → mic button reappears.
- `audio_element_mounts_after_chat` — chat round-trips, page renders.
- `overlay_route_renders` — `/avatar?overlay=1` has chat input visible.
- `keyboard_nav_enter_sends` — Tab into chat input, Enter triggers send.
- `console_errors_clean_on_avatar` — no unfiltered console.error
  within 2 s of `/avatar` load.
- `visual_baseline_settings` — screenshot diff vs baseline with 1%
  tolerance; first run writes baseline. Baselines under
  `tts_samples/visual_baselines/`.

**Pass:** 10/10 green.

## L6h — Latency benchmarks (≈40 s, advisory)

```bash
/e/miniconda/envs/tts/python.exe tts_tools/bench_latency.py
```

NOT pass/fail. Records to `tts_samples/bench/<timestamp>/results.csv`:

- `cold_companion_start_ms` — fresh binary boot → /health 200.
- `cold_mocks_start_ms` — mocks process boot → first /health 200.
- `chat_p50_ms` / `chat_p95_ms` — 30-iter `/api/chat` latency.
- `asr_p50_ms` / `asr_p95_ms` — 20-iter `/api/avatar/asr` latency.
- `ws_handshake_ms` — fresh `/ws/avatar` connect → ModelInfo, median.
- `ws_ttfp_ms` — POST `/api/chat` → first WS Audio frame, median.
- `status_p50_ms` / `status_p95_ms` — `/api/status` response time.

Track these across releases — a 3× jump on `ws_ttfp_ms` flags a
real-world latency regression that no pass/fail test catches.

## L6i — Orchestrator (`run_all.py`)

```bash
# Full sweep (mocks + Rust + Python + benchmarks, no GPU-heavy rigs)
/e/miniconda/envs/tts/python.exe tts_tools/run_all.py --quick

# Full sweep including perf-regression, fuzz, visual baselines
/e/miniconda/envs/tts/python.exe tts_tools/run_all.py --full

# Pick a single suite (any combination)
/e/miniconda/envs/tts/python.exe tts_tools/run_all.py --suites integration,chaos

# Skip `cargo build` if you trust target/release is fresh
/e/miniconda/envs/tts/python.exe tts_tools/run_all.py --quick --no-rebuild

# List every available suite
/e/miniconda/envs/tts/python.exe tts_tools/run_all.py --list

# Skip HTML report rendering
/e/miniconda/envs/tts/python.exe tts_tools/run_all.py --quick --no-report
```

Outputs:

- `tts_samples/run_all/<timestamp>/summary.md` — per-suite pass/fail
  table, port warnings, last-40-lines of each failing log inline.
- `tts_samples/run_all/<timestamp>/<suite>.log` — full stdout+stderr.
- Exit non-zero if any suite failed.

## L7 — Tauri shell smoke (≈90s, automated via CDP)

Wired as `tts_tools/test_tauri_shell.py`. Build + launch
companion-tauri with --features custom-protocol; attach via the
WebView2 CDP port (env: `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223 --remote-allow-origins=*`);
assert main window renders the nav + #root has children + backend
reachable; close via WM_CLOSE (the same signal as clicking X);
verify all sidecar ports (9181/9881/9891) released within 10s.

Caught the iter-12 leak class on 2026-05-18: NMT translator sidecar
(:9881) survived a WM_CLOSE on the Tauri main window — companion-server
+ SBV2 cleaned up, but the NMT TranslatorManager's shutdown path was
missing. Manual L7 had let this slip for weeks.

## L7-deprecated — Tauri shell smoke (legacy manual checklist)

```bash
# 1. Build with the right feature flag — without --features
#    custom-protocol the WebView loads localhost:5173 (the dev server)
#    and shows ERR_CONNECTION_REFUSED in production. This trap has
#    bitten us before.
cd apps/companion-tauri
cargo build --release --features custom-protocol

# 2. Launch and verify:
target/release/companion-tauri.exe

#    Manual checklist:
#    - Main window appears with the Home view (not a blank/error page)
#    - Settings → Avatar & voice loads /api/config and renders form
#    - Avatar tab shows the Live2D viewer
#    - Close the main window via the X button
#
# 3. CRITICAL: within 15s of closing the X button, run:
netstat -an | grep -E ":(9181|9880|9881)"
#
#    Expected: no LISTENING entries on any of those ports.
#    If any are still LISTENING, the Tauri shutdown didn't cascade
#    — file a bug citing the iter-12 leak class.
```

## The before-shipping checklist

Don't tell the user "this works" until:

- [ ] L1 (compile + lint) green
- [ ] L2 (unit) — 130+/0
- [ ] L3 — both wire rigs green (if the TTS or NMT path was touched)
- [ ] L4 — audio integrity green (if TTS pipeline touched)
- [ ] **L5 — lifecycle rig green (mandatory after ANY subprocess code change)**
- [ ] L6a/L6b — relevant baseline e2e scripts green (if UI touched)
- [ ] L6d/L6e/L6f — extended backend / integration / chaos green
       (default in `run_all.py --quick`)
- [ ] L6g — extended frontend green (if UI touched in a non-trivial way)
- [ ] L6h — bench numbers recorded (advisory, but spike means regression)
- [ ] **L6i — `run_all.py --quick` exits 0**
- [ ] L7 — Tauri smoke including the port-LISTENING check
       (any change that ships in a release build)

## Known regression classes — explicit guards

| Symptom | Caught by | Why unit tests don't catch it |
|---------|-----------|-------------------------------|
| Adopted sidecar leaks across app quit | L5 + the wedged-`/shutdown` translator test | `start_server` never sets `self.child` on adopt; cargo tests can't easily spawn a real subprocess that survives the test |
| Tauri WebView blank / ERR_CONNECTION_REFUSED | L7 launch check | feature-flag handling is build-time; only a real launch surfaces it |
| TTS/NMT process orphan after Tauri X-close | L7 netstat after close | the whole point is to exercise OS process lifecycle |
| `companion.toml.example` schema drift | L2 (`example_toml_deserializes_cleanly`) | added iter 5; any new config key MUST be reflected in the example or the test fails |
| Settings UI / Rust schema drift | L2 (TS check) + manual UI smoke | TS picks up the obvious mismatches but field-level drops (iter 10) need the running-binary GET to verify field round-trip |
| AR consistent-truncation on certain greetings | L4 — the GIVE UP diagnostic + the rig's duration floor | known-limitation; rig must keep passing AT 65% even as other sentences carry the turn |

## Skill: `/test-app`

The repository ships an invokable skill at
`.claude/skills/test-app/SKILL.md` that walks an agent through this
SOP layer-by-layer, halting if any layer fails. Use it instead of
running individual commands when you want the full sweep with
result aggregation.

## When a layer fails

1. **Don't skip up the stack.** If L2 fails, L4/L5 results are
   meaningless — fix L2 first.
2. **Don't disable the test.** If the test is wrong, document why and
   fix the test alongside the code. Memory file
   `feedback_test_before_handing_off` is non-negotiable on this.
3. **Don't bypass `clippy -D warnings`.** `#[allow]` only with a
   one-line `// reason: …` explaining why the rule doesn't apply.
4. **Don't trust `cargo build` as a test.** Building isn't testing —
   it's the floor before testing starts.
