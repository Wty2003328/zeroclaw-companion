"""P4 — Real-app stress walker.

The most adversarial rig in the suite. Drives the live
companion-server + browser through a 5-minute random walk of user-like
interactions while simultaneously injecting subprocess failures
(`mock_set tts_dead`, `taskkill PORT_TTS`, NMT slowdown, etc.).

Where L5 is a deterministic single-scenario lifecycle test, P4 is a
chaos walker — random ops × random failure injection. Targets the bug
class "behavior under sequences L5 didn't enumerate":

    * Unhandled JavaScript errors / promise rejections
    * WebSocket reconnection bugs
    * Subprocess lifecycle bugs (orphans after exit, leaks under restart)
    * UI deadlocks (state machines getting stuck)
    * Memory / handle creep across thousands of ops

Determinism: every random choice uses `random.Random(seed)`, so a given
seed reproduces the same walk. Default seed = system random; pass
`--seed N` to lock it.

Walk operations (each step samples one by weight):
    chat_send (25)  nav_route (10)  change_setting (10)
    open_overlay (5)  reload_page (5)  settings_save_garbage (5)
    inject_tts_dead (8)  inject_zc_dead (8)  inject_nmt_slow (5)
    kill_tts_subprocess (4)  clear_chaos (10)  ws_reconnect (5)

Each op:
    * is time-budgeted (5s)
    * catches its own exceptions and continues the walk
    * is logged with start/end ts to <ts>/walk.log

Throughout the walk, every ~5s the rig polls:
    * companion /health 200 (track failure count; budget=2)
    * JS console errors with severity 'error' (allowlist applies)
    * unhandled promise rejections

Final checks (post-walk, post-chaos-clear):
    final_health_ok              — /health 200
    js_console_errors_within_budget — count ≤ 5
    unhandled_rejections_zero    — count == 0
    health_downtime_within_budget — # failed /health probes ≤ 2
    chat_recovery_works          — one round-trip < 5s
    no_orphaned_ports            — every spawned port free after teardown

Tauri mode (TAURI=1 env): try to drive the companion-tauri binary via
CDP instead of a plain Playwright Chromium. Falls back to browser mode
if the binary doesn't exist or doesn't expose CDP.

Run:
    # Smoke (60s)
    python -m tts_tools.test_real_app_stress --duration-s 60 --seed 42
    # Full (default 300s)
    python -m tts_tools.test_real_app_stress
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

# Bump recursion to match the other Playwright rigs — deep DOM handles
# from Settings/Live2D can blow the default during evaluate().
sys.setrecursionlimit(8000)

from tts_tools._test_helpers import (
    PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_COMPANION, PORT_MOCK_ZEROCLAW,
    REPO_ROOT, CheckReporter, http_get, http_json, http_post_json,
    is_port_free, managed_procs, mock_clear, mock_set, python_exe,
    require_ports_free, spawn, spawn_companion_server,
    wait_for_port, wait_for_url,
)

try:
    from playwright.sync_api import (
        sync_playwright, Page, BrowserContext, Browser,
        Error as PlaywrightError, TimeoutError as PlaywrightTimeout,
    )
    PLAYWRIGHT = True
except ImportError:
    PLAYWRIGHT = False


# ---------------------------------------------------------------------------- #
# Constants
# ---------------------------------------------------------------------------- #
BASE_URL = f"http://127.0.0.1:{PORT_COMPANION}"
# Adapter that translates /webhook → mock's /api/chat (matches other rigs).
ZC_ADAPTER_PORT = PORT_MOCK_ZEROCLAW + 1000  # 43617
WALK_ROOT = REPO_ROOT / "tts_samples" / "stress_walker"

CDP_PORT = 9222  # for Tauri mode


# Console-error allowlist — boot/devtools/headless noise we shouldn't
# fail on. The other Playwright rigs use a near-identical list.
CONSOLE_ALLOWLIST: list[re.Pattern[str]] = [
    re.compile(r"Download the React DevTools", re.I),
    re.compile(r"DevTools failed to load source map", re.I),
    re.compile(r"react-router future flag", re.I),
    re.compile(r"WebGL|Live2D", re.I),
    re.compile(r"\b404\b.*favicon", re.I),
    re.compile(r"net::ERR_(ABORTED|CONNECTION_REFUSED|FAILED)", re.I),
    re.compile(r"Failed to load resource:.*ERR_", re.I),
    # 5xx responses during chaos windows: the walker deliberately
    # injects `zc_dead`/`tts_dead` and the next chat_send correctly
    # receives 502/503/504. React's fetch hook logs these as
    # console.error. They are *signal* of the chaos-injection working,
    # not a bug. A real chat-pipeline regression manifests as
    # `chat_recovery_works` failing after chaos is cleared.
    re.compile(r"Failed to load resource:.*status of 5\d\d", re.I),
    # WS failures are EXPECTED during chaos injection windows — the
    # walker explicitly closes WSes and kills subprocesses. Real WS
    # bugs surface as no-reconnect (caught by final health check) or
    # unhandled-rejection events (caught separately).
    re.compile(r"WebSocket connection.*(failed|closed)", re.I),
    re.compile(r"audio decode/play failed:.*NotAllowedError", re.I),
    re.compile(r"play\(\) failed because the user", re.I),
    # Chat replies during chaos windows can 5xx; the React side logs
    # that as console.error. The walker injects chaos deliberately, so
    # these are signal-of-chaos not signal-of-bug. The final
    # chat_recovery_works check verifies the system fully recovers.
    re.compile(r"chat.*5\d\d", re.I),
    re.compile(r"fetch.*failed", re.I),
]


def _allowed_console(text: str) -> bool:
    return any(p.search(text) for p in CONSOLE_ALLOWLIST)


# ---------------------------------------------------------------------------- #
# Companion-server config (matches other Playwright rigs)
# ---------------------------------------------------------------------------- #
def _build_config() -> dict:
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            "url": f"http://127.0.0.1:{ZC_ADAPTER_PORT}",
            "timeout_secs": 30,
        },
        "server": {
            "host": "127.0.0.1",
            "port": PORT_COMPANION,
            "web_dist_dir": str(REPO_ROOT / "web" / "dist"),
        },
        "avatar": {
            "enabled": True,
            "chat_language": "en",
            "tts": {
                "engine": "mock",
                "api_url": f"http://127.0.0.1:{PORT_TTS}",
                "port": PORT_TTS,
                "language": "en",
                "voice": "asuna",
                "speed": 1.0,
                "quality": "balanced",
                "auto_start": False,
                "close_with_companion": False,
                "streaming": True,
                "streaming_target_chars": 80,
            },
            "subagent": {
                "enabled": False,
                "use_zeroclaw_webhook": False,
                "only_when_translating": True,
                "streaming": True,
                "timeout_secs": 3,
            },
        },
        "pulse": {"enabled": False},
    }


# ---------------------------------------------------------------------------- #
# Walk session state
# ---------------------------------------------------------------------------- #
class WalkState:
    """Mutable state shared across ops and probes."""
    def __init__(self, walk_dir: Path, rng: random.Random, ops_per_sec: float):
        self.walk_dir = walk_dir
        self.rng = rng
        self.ops_per_sec = ops_per_sec
        # Counters surfaced in final checks.
        self.js_console_errors: list[str] = []
        self.unhandled_rejections: list[str] = []
        self.health_failures = 0
        self.health_probes = 0
        # Step counter for screenshots / log lines.
        self.step = 0
        self.t_start = time.time()
        # Outstanding chaos state — set when injectors fire so the
        # `clear_chaos` op knows whether there's actually something to clear.
        self.chaos_active = False
        # The walk log handle. Opened in main(); flushed on every write.
        self.log_path = walk_dir / "walk.log"
        self._log_fh = open(self.log_path, "a", encoding="utf-8", buffering=1)

    def log(self, msg: str) -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {msg}"
        try:
            self._log_fh.write(line + "\n")
        except Exception:
            pass
        # Also stream a compact version to stdout so the operator sees progress.
        print(line, flush=True)

    def close(self) -> None:
        try:
            self._log_fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------- #
# Console / pageerror collectors (Playwright)
# ---------------------------------------------------------------------------- #
def _install_console_listeners(page: Page, state: WalkState) -> None:
    def _on_console(msg) -> None:
        if msg.type == "error":
            txt = msg.text
            if not _allowed_console(txt):
                state.js_console_errors.append(txt)
                state.log(f"  [console.error] {txt[:200]}")

    def _on_pageerror(err) -> None:
        txt = str(err)
        if not _allowed_console(txt):
            state.unhandled_rejections.append(txt)
            state.log(f"  [pageerror] {txt[:200]}")

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)


# ---------------------------------------------------------------------------- #
# Operation helpers
# ---------------------------------------------------------------------------- #
def _safe_shot(page: Page, walk_dir: Path, name: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    try:
        page.screenshot(path=str(walk_dir / f"{safe}.png"), full_page=False)
    except Exception:
        pass


def _goto(page: Page, path: str, retries: int = 2) -> None:
    last: Exception | None = None
    for i in range(retries):
        try:
            page.goto(BASE_URL + path, wait_until="domcontentloaded", timeout=8000)
            return
        except (PlaywrightTimeout, PlaywrightError) as e:
            last = e
            page.wait_for_timeout(400 * (i + 1))
    if last is not None:
        raise last


def _react_set_input(page: Page, selector: str, value: str) -> None:
    safe_sel = selector.replace('"', '\\"')
    safe_val = value.replace('"', '\\"')
    page.evaluate(
        f"""() => {{
            const el = document.querySelector("{safe_sel}");
            if (!el) throw new Error('selector not found');
            const proto = (el.tagName === 'SELECT')
              ? window.HTMLSelectElement.prototype
              : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, "{safe_val}");
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }}"""
    )


# ---------------------------------------------------------------------------- #
# Random ops
# Each op signature: (page, state) -> None. Each op MUST handle its own
# exceptions; never raise back to the walker (would abort the run).
# ---------------------------------------------------------------------------- #
UTTERANCES = [
    "hello there",
    "what is the weather",
    "tell me a story",
    "how are you doing",
    "show me a number",
    "translate something",
    "ping",
    "give me a fact",
    "tell me a joke",
    "let's chat",
]
ROUTES = ["/", "/avatar", "/pulse", "/settings"]


def op_chat_send(page: Page, state: WalkState) -> None:
    """Open /avatar if not there, type a random utterance, press Enter."""
    msg = state.rng.choice(UTTERANCES) + f" #{state.step}"
    try:
        if "/avatar" not in page.url:
            _goto(page, "/avatar")
            page.wait_for_timeout(400)
        inp = page.locator('[data-testid="chat-input"]')
        try:
            inp.wait_for(state="visible", timeout=2500)
        except (PlaywrightTimeout, PlaywrightError):
            state.log(f"    chat_send: chat-input not visible")
            return
        inp.fill(msg)
        try:
            inp.press("Enter")
        except (PlaywrightTimeout, PlaywrightError):
            # Fallback to send-button click.
            try:
                page.locator('[data-testid="send-button"]').click(timeout=1500)
            except (PlaywrightTimeout, PlaywrightError):
                pass
        # Wait briefly for the user bubble or assistant reply — don't
        # block the walker on a chaos-injected timeout.
        try:
            page.wait_for_function(
                """(t) => Array.from(document.querySelectorAll('*'))
                    .some(el => el.textContent && el.textContent.includes(t))""",
                arg=msg, timeout=3000,
            )
        except (PlaywrightTimeout, PlaywrightError):
            pass
    except Exception as e:
        state.log(f"    chat_send: {type(e).__name__}: {str(e)[:100]}")


def op_nav_route(page: Page, state: WalkState) -> None:
    route = state.rng.choice(ROUTES)
    try:
        _goto(page, route)
        page.wait_for_timeout(300)
    except Exception as e:
        state.log(f"    nav_route({route}): {type(e).__name__}: {str(e)[:100]}")


def op_change_setting(page: Page, state: WalkState) -> None:
    """Open /settings and tweak a random known field."""
    try:
        if "/settings" not in page.url:
            _goto(page, "/settings")
            page.wait_for_timeout(500)
        # Pick a setting to tweak.
        kind = state.rng.choice(["speed_api", "voice_api", "language_api"])
        if kind == "speed_api":
            speed = round(state.rng.uniform(0.5, 2.0), 2)
            http_post_json(f"{BASE_URL}/api/config/avatar",
                           {"tts_speed": speed}, timeout=3.0)
        elif kind == "voice_api":
            voice = state.rng.choice(["asuna", "default", "narrator"])
            http_post_json(f"{BASE_URL}/api/config/avatar",
                           {"tts_voice": voice}, timeout=3.0)
        else:
            lang = state.rng.choice(["en", "ja", "zh", "es"])
            http_post_json(f"{BASE_URL}/api/config/avatar",
                           {"chat_language": lang}, timeout=3.0)
    except Exception as e:
        state.log(f"    change_setting: {type(e).__name__}: {str(e)[:100]}")


def op_open_overlay(page: Page, state: WalkState) -> None:
    try:
        _goto(page, "/avatar?overlay=1")
        page.wait_for_timeout(400)
        _goto(page, "/avatar")
    except Exception as e:
        state.log(f"    open_overlay: {type(e).__name__}: {str(e)[:100]}")


def op_reload_page(page: Page, state: WalkState) -> None:
    try:
        page.reload(wait_until="domcontentloaded", timeout=5000)
        page.wait_for_timeout(500)
    except (PlaywrightTimeout, PlaywrightError) as e:
        # Reload often hangs on long-lived SSE; fallback to navigate.
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=3000)
            page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=5000)
        except (PlaywrightTimeout, PlaywrightError):
            state.log(f"    reload_page: {type(e).__name__}: {str(e)[:80]}")


def op_settings_save_garbage(page: Page, state: WalkState) -> None:
    """Send deliberately bad values to /api/config/avatar; server should
    reject (4xx) or no-op cleanly (200) — never 5xx, never crash."""
    bad_payloads = [
        {"tts_speed": "very_fast"},
        {"tts_voice": 99999},
        {"tts_speed": -1.0},
        {"chat_language": ["en", "ja"]},
        {"tts_streaming_target_chars": "lots"},
    ]
    payload = state.rng.choice(bad_payloads)
    try:
        status, body, _ = http_post_json(
            f"{BASE_URL}/api/config/avatar", payload, timeout=3.0,
        )
        # We don't fail the walker on a 5xx here (it would re-fire next
        # tick); we just note it. The chat_recovery_works final check
        # asserts the server didn't die.
        if status >= 500:
            state.log(f"    settings_save_garbage({payload}): 5xx={status}")
    except Exception as e:
        state.log(f"    settings_save_garbage: {type(e).__name__}: {str(e)[:80]}")


def op_inject_tts_dead(page: Page, state: WalkState) -> None:
    try:
        if mock_set(tts_dead=True):
            state.chaos_active = True
    except Exception as e:
        state.log(f"    inject_tts_dead: {type(e).__name__}: {str(e)[:80]}")


def op_inject_zc_dead(page: Page, state: WalkState) -> None:
    try:
        if mock_set(zc_dead=True):
            state.chaos_active = True
    except Exception as e:
        state.log(f"    inject_zc_dead: {type(e).__name__}: {str(e)[:80]}")


def op_inject_nmt_slow(page: Page, state: WalkState) -> None:
    try:
        if mock_set(nmt_slow_s=1.5):
            state.chaos_active = True
    except Exception as e:
        state.log(f"    inject_nmt_slow: {type(e).__name__}: {str(e)[:80]}")


def op_kill_tts_subprocess(page: Page, state: WalkState) -> None:
    """Find the PID bound to PORT_TTS and taskkill it. Simulates the
    'user crashed Python' case. On Windows the mock-stack is one
    process for ALL four mocks, so killing it via PORT_TTS would also
    take down NMT/ZC/CTRL. Skip if the listener's PID equals the
    mock-stack PID we tracked at spawn (would defeat the whole rig).
    """
    if os.name != "nt":
        # Best-effort POSIX: use lsof + kill. Mock-stack is one proc so
        # same warning applies; skip.
        return
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, errors="replace",
        )
    except Exception as e:
        state.log(f"    kill_tts_subprocess: netstat failed: {e}")
        return
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in parts:
            continue
        local = parts[1]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        if port == PORT_TTS:
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    if not pids:
        state.log("    kill_tts_subprocess: no listener on PORT_TTS")
        return
    # Skip if the listener is the mock-stack proc (we'd take down the
    # control plane and break the rest of the walk). The mock-stack PID
    # is recorded on the WalkState by main() before the walk begins.
    safe_pids = pids - getattr(state, "_protected_pids", set())
    if not safe_pids:
        state.log(f"    kill_tts_subprocess: skipping (only mock-stack pid {pids})")
        return
    for pid in safe_pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           check=False, capture_output=True, timeout=3.0)
            state.log(f"    kill_tts_subprocess: killed pid {pid}")
        except Exception as e:
            state.log(f"    kill_tts_subprocess: taskkill failed: {e}")


def op_clear_chaos(page: Page, state: WalkState) -> None:
    try:
        if mock_clear():
            state.chaos_active = False
    except Exception as e:
        state.log(f"    clear_chaos: {type(e).__name__}: {str(e)[:80]}")


def op_ws_reconnect(page: Page, state: WalkState) -> None:
    """Force-close any WebSocket inside the page via JS. The avatar
    component should auto-reconnect (useAvatarSocket.ts has the retry
    loop). We don't assert reconnection here — that's covered by the
    final health-check + console-rejection budget."""
    try:
        # Walk the page's known WS handles. We hooked the global
        # WebSocket constructor at boot (see _install_ws_tracker) to
        # collect every WebSocket ever created.
        closed = page.evaluate(
            """() => {
                const arr = window.__rig_ws_list || [];
                let n = 0;
                for (const ws of arr) {
                    try {
                        if (ws.readyState === WebSocket.OPEN ||
                            ws.readyState === WebSocket.CONNECTING) {
                            ws.close(4000, 'rig-forced-reconnect');
                            n++;
                        }
                    } catch (e) { /* ignore */ }
                }
                return n;
            }"""
        )
        state.log(f"    ws_reconnect: closed {closed} sockets")
    except Exception as e:
        state.log(f"    ws_reconnect: {type(e).__name__}: {str(e)[:80]}")


OPS: list[tuple[str, int, Callable[[Page, WalkState], None]]] = [
    ("chat_send", 25, op_chat_send),
    ("nav_route", 10, op_nav_route),
    ("change_setting", 10, op_change_setting),
    ("open_overlay", 5, op_open_overlay),
    ("reload_page", 5, op_reload_page),
    ("settings_save_garbage", 5, op_settings_save_garbage),
    ("inject_tts_dead", 8, op_inject_tts_dead),
    ("inject_zc_dead", 8, op_inject_zc_dead),
    ("inject_nmt_slow", 5, op_inject_nmt_slow),
    ("kill_tts_subprocess", 4, op_kill_tts_subprocess),
    ("clear_chaos", 10, op_clear_chaos),
    ("ws_reconnect", 5, op_ws_reconnect),
]


def _pick_op(rng: random.Random) -> tuple[str, Callable[[Page, WalkState], None]]:
    weights = [w for _, w, _ in OPS]
    idx = rng.choices(range(len(OPS)), weights=weights, k=1)[0]
    name, _, fn = OPS[idx]
    return name, fn


# ---------------------------------------------------------------------------- #
# WS tracker — install before any page load so every WebSocket the SPA
# creates is captured into window.__rig_ws_list for op_ws_reconnect.
# ---------------------------------------------------------------------------- #
def _install_ws_tracker(context: BrowserContext) -> None:
    context.add_init_script(
        """
        (function() {
            window.__rig_ws_list = [];
            const Orig = window.WebSocket;
            function TrackedWS(url, protocols) {
                const ws = protocols === undefined
                    ? new Orig(url)
                    : new Orig(url, protocols);
                try { window.__rig_ws_list.push(ws); } catch (e) {}
                return ws;
            }
            TrackedWS.prototype = Orig.prototype;
            TrackedWS.CONNECTING = Orig.CONNECTING;
            TrackedWS.OPEN = Orig.OPEN;
            TrackedWS.CLOSING = Orig.CLOSING;
            TrackedWS.CLOSED = Orig.CLOSED;
            window.WebSocket = TrackedWS;
        })();
        """
    )


# ---------------------------------------------------------------------------- #
# Health prober — runs at the same cadence as the walker, between ops.
# ---------------------------------------------------------------------------- #
def _probe_health(state: WalkState) -> None:
    state.health_probes += 1
    try:
        s, _, _ = http_get(f"{BASE_URL}/health", timeout=2.0)
        if s != 200:
            state.health_failures += 1
            state.log(f"  [health] {state.health_probes}: status={s}")
    except Exception as e:
        state.health_failures += 1
        state.log(f"  [health] {state.health_probes}: exc={type(e).__name__}")


# ---------------------------------------------------------------------------- #
# Tauri-mode helpers
# ---------------------------------------------------------------------------- #
def _tauri_binary() -> Path:
    return REPO_ROOT / "target" / "release" / "companion-tauri.exe"


def _maybe_spawn_tauri(state: WalkState, procs: list) -> tuple[Browser | None, BrowserContext | None]:
    """If TAURI=1, attempt to spawn companion-tauri + attach via CDP.
    Returns (browser, context) on success, (None, None) to fall back."""
    if os.environ.get("TAURI", "0") != "1":
        return None, None
    binary = _tauri_binary()
    if not binary.exists():
        state.log(f"[tauri] binary missing at {binary}; falling back")
        return None, None
    state.log(f"[tauri] spawning {binary} with CDP on :{CDP_PORT}")
    mp = spawn(
        "companion-tauri",
        [str(binary)],
        port=None,
        env={
            "WEBKIT_INSPECTOR_SERVER": f"127.0.0.1:{CDP_PORT}",
            "RUST_LOG": os.environ.get("RUST_LOG", "info"),
        },
        log_path=state.walk_dir / "companion-tauri.log",
    )
    procs.append(mp)
    # Wait for CDP port.
    if not wait_for_port(CDP_PORT, timeout_s=10.0):
        state.log("[tauri] CDP port did not bind; falling back")
        return None, None
    # We can't attach to the WebView from playwright until we know
    # the CDP endpoint exposes the right protocol. tauri-runtime-wry
    # does NOT actually honor WEBKIT_INSPECTOR_SERVER (that's a WebKit
    # var; wry on Windows uses WebView2 via Edge's --remote-debugging-
    # port flag, which Tauri does not set). So this path is best-effort
    # and almost always falls back. Try connect; if it 500s, bail.
    try:
        # NB: connect_over_cdp requires the caller to pass the http URL,
        # but we have to import sync_playwright inside main() to manage
        # the lifecycle. The actual attach happens in run_browser_loop.
        return "DEFER", None  # signal main to attempt attach
    except Exception as e:
        state.log(f"[tauri] CDP attach probe failed: {e}")
        return None, None


# ---------------------------------------------------------------------------- #
# Main walker
# ---------------------------------------------------------------------------- #
def _run_walk(r: CheckReporter, state: WalkState, page: Page,
              duration_s: float) -> None:
    state.log(f"=== walk start  duration={duration_s}s "
              f"ops_per_sec={state.ops_per_sec}  seed={state.rng.random()} ===")
    # Drain the throwaway sample above so the rng is fresh for ops.
    state.rng.random()  # noqa — discard

    deadline = state.t_start + duration_s
    next_health_probe = state.t_start + 5.0
    step_interval = 1.0 / max(0.1, state.ops_per_sec)

    while time.time() < deadline:
        step_start = time.time()
        state.step += 1
        op_name, op_fn = _pick_op(state.rng)
        op_t0 = time.time()
        try:
            # Time-budget the op: we don't have a per-thread timeout, so
            # rely on each op's own timeouts (≤5s by design).
            op_fn(page, state)
            ok = True
        except Exception as e:
            ok = False
            state.log(f"    [walk] step {state.step}  op={op_name}  EXC={type(e).__name__}: {str(e)[:120]}")
            _safe_shot(page, state.walk_dir, f"{op_name}-step{state.step}")
        op_elapsed = time.time() - op_t0
        state.log(f"[walk] step {state.step:4d}  op={op_name:22s}  "
                  f"elapsed={op_elapsed:5.2f}s  {'ok' if ok else 'FAIL'}")

        # Periodic health probe.
        if time.time() >= next_health_probe:
            _probe_health(state)
            next_health_probe = time.time() + 5.0

        # Pace the loop.
        spent = time.time() - step_start
        if spent < step_interval:
            time.sleep(step_interval - spent)

    state.log(f"=== walk end  steps={state.step}  "
              f"console_errors={len(state.js_console_errors)}  "
              f"page_errors={len(state.unhandled_rejections)}  "
              f"health_failures={state.health_failures}/{state.health_probes} ===")


def _final_checks(r: CheckReporter, state: WalkState, page: Page) -> None:
    # Make sure chaos is cleared.
    try:
        mock_clear()
        state.chaos_active = False
    except Exception:
        pass
    time.sleep(1.0)

    # final_health_ok
    s, _, _ = http_get(f"{BASE_URL}/health", timeout=3.0)
    r.check("final_health_ok", s == 200, f"status={s}")

    # js_console_errors_within_budget — fail at > 5
    budget = int(os.environ.get("STRESS_CONSOLE_BUDGET", "5"))
    js_count = len(state.js_console_errors)
    sample = state.js_console_errors[:3]
    r.check(
        "js_console_errors_within_budget",
        js_count <= budget,
        f"count={js_count} budget={budget} sample={sample}",
    )

    # unhandled_rejections_zero
    rej_count = len(state.unhandled_rejections)
    rej_sample = state.unhandled_rejections[:3]
    r.check(
        "unhandled_rejections_zero",
        rej_count == 0,
        f"count={rej_count} sample={rej_sample}",
    )

    # health_downtime_within_budget — fail at > 2
    health_budget = int(os.environ.get("STRESS_HEALTH_BUDGET", "2"))
    r.check(
        "health_downtime_within_budget",
        state.health_failures <= health_budget,
        f"failures={state.health_failures}/{state.health_probes} budget={health_budget}",
    )

    # chat_recovery_works — one final round-trip should complete < 5s.
    t0 = time.time()
    status = 0
    body = b""
    try:
        status, body, _ = http_post_json(
            f"{BASE_URL}/api/chat", {"message": "final-recovery-probe"},
            timeout=5.0,
        )
    except Exception as e:
        r.check("chat_recovery_works", False, f"exc={type(e).__name__}: {e}")
        return
    elapsed = time.time() - t0
    reply_ok = b"mock-reply" in body
    r.check(
        "chat_recovery_works",
        status == 200 and elapsed < 5.0 and reply_ok,
        f"status={status} elapsed={elapsed:.2f}s reply_ok={reply_ok}",
    )


def _check_no_orphaned_ports(r: CheckReporter, state: WalkState,
                              ports: list[int]) -> None:
    """After managed_procs teardown, every port must be free within 15s.
    The iter-12 class: subprocess survives parent exit."""
    deadline = time.time() + 15.0
    still_bound: list[int] = []
    while time.time() < deadline:
        still_bound = [p for p in ports if not is_port_free(p)]
        if not still_bound:
            break
        time.sleep(0.3)
    r.check(
        "no_orphaned_ports",
        not still_bound,
        f"still_bound={still_bound} (waited 15s after teardown)",
    )

    # Tauri-friendly final check.
    if os.environ.get("TAURI", "0") == "1" and os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq companion-tauri.exe"],
                text=True, errors="replace",
            )
            zombies = "companion-tauri.exe" in out
            r.check("tauri_friendly_no_orphan_process", not zombies,
                    "companion-tauri.exe still alive" if zombies else "")
        except Exception as e:
            r.info(f"tauri orphan check skipped: {e}")


# ---------------------------------------------------------------------------- #
# Top-level orchestration
# ---------------------------------------------------------------------------- #
def _resolve_mock_pids(mock_proc) -> set[int]:
    """Best-effort: include the mock-stack PID + any child PIDs so the
    kill-TTS op never accidentally kills the mock-stack itself."""
    pids: set[int] = set()
    if mock_proc is not None:
        try:
            pids.add(mock_proc.proc.pid)
        except Exception:
            pass
        try:
            import psutil
            p = psutil.Process(mock_proc.proc.pid)
            for c in p.children(recursive=True):
                pids.add(c.pid)
        except Exception:
            pass
    return pids


def main() -> None:
    parser = argparse.ArgumentParser(description="P4 — real-app stress walker")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed (default: system random)")
    parser.add_argument("--duration-s", type=float, default=300.0,
                        help="walk duration in seconds (default 300 = 5 min)")
    parser.add_argument("--ops-per-sec", type=float, default=2.0,
                        help="target ops per second (default 2)")
    args = parser.parse_args()

    r = CheckReporter("test_real_app_stress")

    if not PLAYWRIGHT:
        r.check("playwright available", False,
                "pip install playwright && playwright install chromium")
        r.summary_or_exit()

    seed = args.seed if args.seed is not None else random.SystemRandom().randint(0, 2**31 - 1)
    rng = random.Random(seed)

    # Soft-wait for stale ports from a prior run.
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if all(is_port_free(p) for p in (PORT_TTS, PORT_NMT, PORT_CONTROL,
                                          PORT_COMPANION, PORT_MOCK_ZEROCLAW,
                                          ZC_ADAPTER_PORT)):
            break
        time.sleep(0.5)
    require_ports_free(PORT_TTS, PORT_NMT, PORT_CONTROL,
                       PORT_COMPANION, PORT_MOCK_ZEROCLAW, ZC_ADAPTER_PORT)

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    walk_dir = WALK_ROOT / ts
    walk_dir.mkdir(parents=True, exist_ok=True)

    state = WalkState(walk_dir=walk_dir, rng=rng, ops_per_sec=args.ops_per_sec)
    state.log(f"seed={seed}  duration={args.duration_s}s  ops_per_sec={args.ops_per_sec}")
    state.log(f"walk_dir={walk_dir}")

    spawned_ports = [PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_COMPANION,
                     PORT_MOCK_ZEROCLAW, ZC_ADAPTER_PORT]

    try:
        with managed_procs() as procs:
            # 1. Mock stack.
            mock = spawn(
                "mock-stack",
                [python_exe(), "-m", "tts_tools._mock_stack"],
                port=PORT_CONTROL,
                log_path=walk_dir / "mock-stack.log",
            )
            procs.append(mock)
            if not wait_for_port(PORT_CONTROL, timeout_s=15):
                r.check("mock control plane up", False, "timeout")
                r.summary_or_exit()
            mock_clear()
            for p in (PORT_TTS, PORT_NMT, PORT_MOCK_ZEROCLAW):
                wait_for_port(p, timeout_s=10)

            # Record mock-stack PIDs so op_kill_tts_subprocess doesn't
            # accidentally kill our control plane.
            state._protected_pids = _resolve_mock_pids(mock)
            state.log(f"protected mock-stack pids: {state._protected_pids}")

            # 2. webhook adapter.
            adapter = spawn(
                "zc-webhook-adapter",
                [python_exe(), "-m", "tts_tools._zc_webhook_adapter"],
                port=ZC_ADAPTER_PORT,
                env={"MOCK_ZC_ADAPTER_PORT": str(ZC_ADAPTER_PORT),
                     "MOCK_ZC_UPSTREAM_PORT": str(PORT_MOCK_ZEROCLAW)},
                log_path=walk_dir / "zc-adapter.log",
            )
            procs.append(adapter)
            if not wait_for_port(ZC_ADAPTER_PORT, timeout_s=10):
                r.check("zc-webhook-adapter up", False, "timeout")
                r.summary_or_exit()

            # 3. companion-server.
            try:
                comp, cfg_path = spawn_companion_server(
                    _build_config(), port=PORT_COMPANION,
                    log_dir=walk_dir,
                )
            except SystemExit as e:
                r.check("companion-server binary present", False, str(e))
                r.summary_or_exit()
            procs.append(comp)
            if not wait_for_url(f"{BASE_URL}/health", timeout_s=30):
                r.check("companion /health 200", False, "30s timeout")
                r.summary_or_exit()
            time.sleep(1.5)  # SSE bridge settle
            state.log(f"companion-server up; cfg={cfg_path}")

            # 4. Playwright. (Tauri mode is best-effort — tauri-runtime-
            # wry doesn't reliably honor remote-debugging env vars on
            # Windows, so we fall back to plain Chromium-against-
            # companion-server in nearly every real run.)
            headless = os.environ.get("HEADLESS", "1") != "0"
            tauri_requested = os.environ.get("TAURI", "0") == "1"
            with sync_playwright() as pw:
                browser: Browser | None = None
                context: BrowserContext | None = None

                if tauri_requested:
                    # Try to spawn tauri + attach CDP.
                    binary = _tauri_binary()
                    if not binary.exists():
                        r.info("tauri mode requested but binary missing; falling back to browser")
                    else:
                        state.log(f"[tauri] spawning {binary}")
                        # Some wry builds respect this; most don't.
                        mp = spawn(
                            "companion-tauri", [str(binary)],
                            env={
                                "WEBKIT_INSPECTOR_SERVER": f"127.0.0.1:{CDP_PORT}",
                                "RUST_LOG": os.environ.get("RUST_LOG", "info"),
                            },
                            log_path=walk_dir / "companion-tauri.log",
                        )
                        procs.append(mp)
                        time.sleep(2.0)
                        if wait_for_port(CDP_PORT, timeout_s=8):
                            try:
                                browser = pw.chromium.connect_over_cdp(
                                    f"http://127.0.0.1:{CDP_PORT}")
                                context = browser.contexts[0] if browser.contexts \
                                    else browser.new_context()
                                state.log("[tauri] attached via CDP")
                            except Exception as e:
                                state.log(f"[tauri] CDP attach failed: {e}; falling back")
                                browser = None
                                context = None
                        else:
                            state.log("[tauri] CDP port never bound; falling back")

                if browser is None:
                    browser = pw.chromium.launch(headless=headless)
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        ignore_https_errors=True,
                    )

                _install_ws_tracker(context)
                page = context.pages[0] if context.pages else context.new_page()
                _install_console_listeners(page, state)

                # Initial navigation.
                try:
                    _goto(page, "/")
                    page.wait_for_timeout(1200)
                except Exception as e:
                    r.check("initial page load", False,
                            f"{type(e).__name__}: {e}")
                    r.summary_or_exit()
                r.check("initial page load", True)

                # Run the walk. The walker itself never raises out — its
                # ops catch their own exceptions.
                try:
                    _run_walk(r, state, page, duration_s=args.duration_s)
                except KeyboardInterrupt:
                    state.log("walk interrupted by user")

                # Final checks while the stack is still up.
                _final_checks(r, state, page)

                # Close browser before tearing down servers.
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        # managed_procs has now stopped companion-server + adapter + mock.
        # Verify no orphaned ports — the iter-12 leak class.
        _check_no_orphaned_ports(r, state, spawned_ports)

    finally:
        state.close()

    r.summary_or_exit()


if __name__ == "__main__":
    main()
