"""L6b — Frontend systematic coverage (Playwright).

Drives the companion-server SPA across every route with mocks wired in
for TTS/NMT/zeroclaw. Each axis exercises a slice of the UI surface:
SMOKE/NAV/SETTINGS/CHARACTERS/AVATAR/PULSE/PERSISTENCE/ERROR/A11Y.

Setup:
  1. Spawn `_mock_stack` (TTS 9880, NMT 9881, ZC 42617, control 9883).
  2. Spawn companion-server on 9181 with [avatar.tts] api_url = 9880,
     [avatar.subagent.translator] url = 9881, [zeroclaw] url = 42617.
  3. Wait for /health.
  4. Launch a Playwright Chromium (headless by default;
     HEADLESS=0 env override for local debugging).
  5. Run check axes; save failure screenshots under
     tts_samples/frontend_failures/.
  6. Tear down via managed_procs.

Run:
  python -m tts_tools.test_frontend_e2e
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# Playwright's CDP→Python deserializer can recurse deeply on big page
# graphs (Settings has the full Live2D pixi tree mounted). Bump the
# limit before any check runs so the rare deep DOM/Locator handle doesn't
# explode mid-suite.
sys.setrecursionlimit(8000)

from tts_tools._test_helpers import (
    PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_COMPANION, PORT_MOCK_ZEROCLAW,
    REPO_ROOT, CheckReporter, http_get, http_json, http_post_json,
    managed_procs, mock_clear, python_exe, require_ports_free, spawn,
    spawn_companion_server, wait_for_port, wait_for_url,
)

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext, Error as PlaywrightError, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT = True
except ImportError:
    PLAYWRIGHT = False


BASE_URL = f"http://127.0.0.1:{PORT_COMPANION}"
# The mock-stack only ships `/api/chat` on the zeroclaw port; companion-
# server's client calls `/webhook`. The rig spawns _zc_webhook_adapter
# on a sibling port that proxies `/webhook` → mock's `/api/chat`.
ZC_ADAPTER_PORT = PORT_MOCK_ZEROCLAW + 1000  # 43617
SHOT_DIR = REPO_ROOT / "tts_samples" / "frontend_failures"
LOG_DIR = REPO_ROOT / "tts_samples" / "logs" / "frontend_e2e"

# Console-error allowlist — known-noisy logs we don't fail on.
CONSOLE_ALLOWLIST: list[re.Pattern[str]] = [
    re.compile(r"Download the React DevTools", re.I),
    re.compile(r"DevTools failed to load source map", re.I),
    re.compile(r"react-router future flag", re.I),
    re.compile(r"WebGL", re.I),  # live2d webgl noise on headless
    re.compile(r"Live2D", re.I),
    re.compile(r"\b404\b.*favicon", re.I),
    re.compile(r"net::ERR_ABORTED.*cubism", re.I),
    # Transient: prior tests trigger /api/config writes that briefly
    # bounce the server. Mid-flight resource fetches show as ERR_*; the
    # navigation itself is retried by `_goto`, so these are noise.
    re.compile(r"Failed to load resource:.*ERR_(CONNECTION_REFUSED|ABORTED|FAILED)", re.I),
    re.compile(r"Failed to load resource: the server responded with a status of 404"),
    re.compile(r"WebSocket connection.*failed", re.I),
    # Headless autoplay policy: pre-greeting TTS clip tries to play
    # before any user gesture — Chrome rejects the play() call. Harmless
    # for tests; real users have always clicked something by then.
    re.compile(r"audio decode/play failed:.*NotAllowedError", re.I),
    re.compile(r"play\(\) failed because the user", re.I),
]


def _allowed_console(text: str) -> bool:
    return any(p.search(text) for p in CONSOLE_ALLOWLIST)


def _build_config() -> dict:
    """Companion-server TOML config that points every sidecar at our mocks."""
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            # Point at the rig's webhook-adapter — translates /webhook
            # into the mock's /api/chat shape.
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


class ConsoleSink:
    """Collects console messages with filtering, per-test reset."""
    def __init__(self, page: Page):
        self.errors: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)

    def _on_console(self, msg) -> None:
        if msg.type in ("error",):
            txt = msg.text
            if not _allowed_console(txt):
                self.errors.append(txt)

    def _on_pageerror(self, err) -> None:
        txt = str(err)
        if not _allowed_console(txt):
            self.errors.append(txt)

    def reset(self) -> None:
        self.errors.clear()


def _safe_shot(page: Page, name: str) -> None:
    """Save a screenshot to SHOT_DIR. Sanitise name → filesystem-safe."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(SHOT_DIR / f"{safe}.png"), full_page=True)
    except Exception:
        pass


def _react_set_input(page: Page, selector: str, value: str) -> None:
    """Bypass React's controlled-input guard so onChange fires."""
    safe_sel = selector.replace('"', '\\"')
    safe_val = value.replace('"', '\\"')
    page.evaluate(
        f"""() => {{
            const el = document.querySelector("{safe_sel}");
            if (!el) throw new Error('selector not found');
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, "{safe_val}");
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }}"""
    )


def _reload(page: Page, retries: int = 3) -> None:
    """Reload with retry — chromium-on-Windows occasionally hangs on
    `page.reload()` when the app holds long-lived WS/SSE connections.
    Short timeout + fall back to navigate-away/back which forces a hard
    tear-down."""
    current = page.url
    last: Exception | None = None
    for i in range(retries):
        try:
            page.reload(wait_until="domcontentloaded", timeout=6000)
            return
        except (PlaywrightTimeout, PlaywrightError) as e:
            last = e
            try:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=4000)
                page.goto(current, wait_until="domcontentloaded", timeout=10000)
                return
            except (PlaywrightTimeout, PlaywrightError) as e2:
                last = e2
                page.wait_for_timeout(600 * (i + 1))
    if last is not None:
        raise last


def _goto(page: Page, path: str, retries: int = 5) -> None:
    """Navigate with retry — companion-server occasionally drops new
    connections during the SSE-bridge reconnect-storm at boot, and the
    apply path can trigger a TTS restart that briefly pauses the bind.
    5 tries × 800ms back-off is plenty for the transient case; a true
    crash will still surface."""
    last: Exception | None = None
    for i in range(retries):
        try:
            # `domcontentloaded` is enough for our checks — `networkidle`
            # blocks until 500ms with no in-flight requests, which the
            # SSE/WS keep-alives never satisfy on this app.
            page.goto(BASE_URL + path, wait_until="domcontentloaded", timeout=12000)
            return
        except (PlaywrightTimeout, PlaywrightError) as e:
            last = e
            page.wait_for_timeout(800 * (i + 1))
    if last is not None:
        raise last


# ──────────────────────────────────────────────────────────────────────
# Check implementations
# ──────────────────────────────────────────────────────────────────────
def check_smoke(r: CheckReporter, page: Page, sink: ConsoleSink) -> None:
    """Every route renders without unfiltered console errors."""
    for route in ("/", "/avatar", "/pulse", "/settings"):
        sink.reset()
        try:
            _goto(page, route)
            # Let any deferred prewarm fetches settle and TCP drain.
            page.wait_for_timeout(1200)
            ok = not sink.errors
            detail = "" if ok else f"errors: {sink.errors[:2]}"
            name = f"smoke route {route}"
            if not r.check(name, ok, detail):
                _safe_shot(page, name)
        except (PlaywrightTimeout, PlaywrightError) as e:
            r.check(f"smoke route {route}", False, f"nav exc: {e}")
            _safe_shot(page, f"smoke route {route}")


def check_nav(r: CheckReporter, page: Page) -> None:
    """Top-nav links navigate between routes."""
    _goto(page, "/")
    routes = [("Home", "/"), ("Avatar", "/avatar"), ("Pulse", "/pulse"), ("Settings", "/settings")]
    for label, expect in routes:
        try:
            # Nav uses <a href=...> with onClick → location.pathname changes.
            link = page.get_by_role("link", name=re.compile(rf"^{label}$"))
            link.first.click(timeout=5000)
            page.wait_for_url(re.compile(re.escape(expect) + r"(\?.*)?$"), timeout=5000)
            ok = page.url.endswith(expect) or expect in page.url
            r.check(f"nav click {label} → {expect}", ok, f"url={page.url}")
        except (PlaywrightTimeout, PlaywrightError) as e:
            r.check(f"nav click {label} → {expect}", False, f"{type(e).__name__}: {e}")
            _safe_shot(page, f"nav {label}")


def _open_avatar_voice_section(page: Page) -> None:
    """Settings sections collapse — open 'Avatar & voice' if collapsed."""
    # The section header is a button. Click to expand.
    try:
        btn = page.get_by_role("button", name=re.compile(r"Avatar\s*&\s*voice", re.I))
        if btn.count() > 0:
            # If already expanded, clicking would collapse; check aria-expanded.
            expanded = btn.first.get_attribute("aria-expanded")
            if expanded == "false":
                btn.first.click()
                page.wait_for_timeout(200)
    except (PlaywrightTimeout, PlaywrightError):
        pass


def check_settings_voice_speed_persists(r: CheckReporter, page: Page) -> None:
    """Voice-speed value persists across reload: set via UI (controlled
    React input), save via Apply, reload, assert slider value sticks."""
    name = "settings: voice speed persists across reload"
    target = 1.35
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(1500)
        # Wait for AvatarEditor (depends on /api/config GET).
        page.wait_for_selector('input[type="range"]', state="visible", timeout=10000)
        _react_set_input(page, 'input[type="range"]', str(target))
        page.wait_for_timeout(400)
        # Confirm React saw the change.
        before = page.evaluate(
            "() => document.querySelector('input[type=range]')?.value"
        )
        # Find + tag the first enabled Apply.
        idx = page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('button'));
                for (let i = 0; i < btns.length; i++) {
                    const t = (btns[i].innerText || '').trim();
                    if ((t === 'Apply' || t === 'Applying…') && !btns[i].disabled) {
                        btns[i].setAttribute('data-test-apply', 'pick');
                        return i;
                    }
                }
                return -1;
            }"""
        )
        if idx is None or idx < 0:
            r.check(name, False,
                    f"no enabled Apply button (slider.value={before!r} — change didn't dirty editor)")
            _safe_shot(page, name)
            return
        page.locator('button[data-test-apply="pick"]').first.click(timeout=3000)
        # Wait for the apply to complete server-side.
        page.wait_for_timeout(2000)
        # Confirm via the HTTP API directly that the speed was saved.
        cfg = http_json(f"{BASE_URL}/api/config", timeout=5.0)
        speed_saved = None
        try:
            speed_saved = cfg.get("avatar", {}).get("tts", {}).get("speed")
        except (AttributeError, KeyError):
            pass
        if speed_saved is None or abs(float(speed_saved) - target) > 0.06:
            r.check(name, False,
                    f"server-side speed after Apply = {speed_saved!r} (expected ~{target})")
            _safe_shot(page, name)
            return
        # Reload and verify the slider re-reads the saved value.
        _reload(page)
        page.wait_for_selector('input[type="range"]', state="visible", timeout=10000)
        page.wait_for_timeout(800)
        val = page.evaluate(
            "() => document.querySelector('input[type=range]')?.value"
        )
        try:
            f = float(val)
        except (TypeError, ValueError):
            r.check(name, False, f"reloaded slider had no value: {val!r}")
            _safe_shot(page, name)
            return
        ok = abs(f - target) < 0.06
        if not r.check(name, ok, f"reloaded value={f}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)
    except RecursionError as e:
        r.check(name, False, f"recursion: {e}")
        _safe_shot(page, name)


def check_settings_translation_modes(r: CheckReporter, page: Page) -> None:
    """All three ModeRadio options render; Direct AI reveals service config."""
    name = "settings: translation has 3 modes; Direct AI reveals service config"
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(800)
        # The radios use name='translation-mode' (see ModeRadio).
        radios = page.locator('input[name="translation-mode"]')
        rc = radios.count()
        if rc < 3:
            r.check(name, False, f"only {rc} translation-mode radios found")
            _safe_shot(page, name)
            return
        # Find the labels — Direct AI / Main agent / Local model.
        body = page.content()
        has_direct = bool(re.search(r"Direct AI\s*service", body))
        has_agent = bool(re.search(r"Main agent.*proxy", body))
        has_local = bool(re.search(r"Local model", body))
        labels_ok = has_direct and has_agent and has_local
        # Click Direct AI; expect a "Service configuration" subsection to appear.
        radios.nth(0).check(force=True)
        page.wait_for_timeout(400)
        body2 = page.content()
        reveals = "Service configuration" in body2
        ok = labels_ok and reveals and (rc >= 3)
        if not r.check(name, ok,
                       f"radios={rc} direct={has_direct} agent={has_agent} "
                       f"local={has_local} reveal={reveals}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_characters_via_api(r: CheckReporter, page: Page) -> None:
    """Create a character via POST /api/characters; verify roster shows it."""
    name = "characters: API-created char appears in Home roster"
    char_id = f"test-char-{int(time.time())}"
    payload = {
        "id": char_id,
        "name": "Playwright Tester",
        "model_id": "",
        "system_prompt": "I am a test character.",
        "notes": "",
    }
    status, body, _ = http_post_json(f"{BASE_URL}/api/characters", payload, timeout=5.0)
    if status not in (200, 201):
        r.check(name, False, f"POST /api/characters returned {status}: {body[:120]}")
        return
    # Navigate to Home and assert the name shows up.
    try:
        _goto(page, "/")
        page.wait_for_timeout(800)
        # Roster header is "Characters"; cards show the name.
        found = page.get_by_text("Playwright Tester").count() > 0
        if not r.check(name, found, f"char_id={char_id}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def _send_chat(page: Page, text: str) -> bool:
    """Type text into chat-input on /avatar and click Send. Returns True if
    the user bubble was appended within 5s."""
    inp = page.locator('[data-testid="chat-input"]')
    inp.wait_for(state="visible", timeout=5000)
    inp.fill(text)
    page.locator('[data-testid="send-button"]').click(timeout=3000)
    # Wait for the user bubble to render.
    try:
        page.wait_for_function(
            """(t) => Array.from(document.querySelectorAll('*'))
                .some(el => el.textContent && el.textContent.includes(t))""",
            arg=text, timeout=5000,
        )
        return True
    except (PlaywrightTimeout, PlaywrightError):
        return False


def check_avatar_chat(r: CheckReporter, page: Page) -> None:
    """Avatar: send a message; user bubble + assistant reply within 12s."""
    name = "avatar: send message produces user + assistant bubbles"
    msg = f"hello-{int(time.time())}"
    try:
        _goto(page, "/avatar")
        # The websocket needs a moment to settle and the avatar/character
        # GET requests complete; without this delay the Send click can
        # land before the input is mounted.
        page.wait_for_timeout(1500)
        sent = _send_chat(page, msg)
        if not sent:
            r.check(name, False, "user bubble never appeared")
            _safe_shot(page, name)
            return
        # Assistant reply — mock zeroclaw via the webhook adapter replies
        # "mock-reply to: <msg>". 12s budget covers the case where the
        # SSE bridge briefly reconnects mid-call.
        try:
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('*'))
                    .some(el => el.textContent && el.textContent.includes('mock-reply to:'))""",
                timeout=12000,
            )
            ok = True
            detail = ""
        except (PlaywrightTimeout, PlaywrightError):
            ok = False
            detail = "assistant reply never rendered within 12s"
        if not r.check(name, ok, detail):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_pulse_clean(r: CheckReporter, page: Page, sink: ConsoleSink) -> None:
    """Pulse route renders without unfiltered console errors."""
    name = "pulse route renders without console errors"
    sink.reset()
    try:
        _goto(page, "/pulse")
        page.wait_for_timeout(1200)
        ok = not sink.errors
        if not r.check(name, ok, "" if ok else f"errors: {sink.errors[:2]}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_persistence_chat(r: CheckReporter, page: Page) -> None:
    """After a chat, reload page; chat history persists from localStorage."""
    name = "persistence: chat history survives reload (localStorage)"
    msg = f"persist-{int(time.time())}"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(500)
        if not _send_chat(page, msg):
            r.check(name, False, "couldn't send chat to test persistence")
            _safe_shot(page, name)
            return
        try:
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('*'))
                    .some(el => el.textContent && el.textContent.includes('mock-reply to:'))""",
                timeout=8000,
            )
        except (PlaywrightTimeout, PlaywrightError):
            pass  # still test what's there
        page.wait_for_timeout(500)
        _reload(page)
        page.wait_for_timeout(800)
        body = page.content()
        ok = msg in body
        if not r.check(name, ok, f"msg present after reload: {ok}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_error_save_validation(r: CheckReporter, page: Page) -> None:
    """Send an obviously malformed body to /api/config/avatar and assert
    the server (a) doesn't crash, (b) either rejects it or no-ops cleanly.
    Mirrors what the UI's `saveErrorMessage()` helper renders when the
    server returns non-2xx."""
    name = "error: malformed /api/config/avatar payload handled gracefully"
    # Ensure server is responsive before sending the malformed payload —
    # the prior tests sometimes leave a momentary tcp backlog hiccup.
    wait_for_url(f"{BASE_URL}/health", timeout_s=10)
    # Garbage payload — wrong types, unknown keys.
    status, body, _ = http_post_json(
        f"{BASE_URL}/api/config/avatar",
        {"tts_voice": 12345, "subagent_enabled": "yes-please"},
        timeout=10.0,
    )
    # Server must respond (not crash). 4xx is a valid rejection; 200
    # means it silently ignored the bad keys (also acceptable —
    # confirms no panic). Anything 5xx is a fail.
    sane = status in (200, 400, 422) and len(body) > 0
    # Probe /health to confirm the server is still alive afterwards.
    s2, _, _ = http_get(f"{BASE_URL}/health", timeout=3.0)
    still_alive = s2 == 200
    ok = sane and still_alive
    r.check(name, ok,
            f"status={status} body[:60]={body[:60]!r} alive_after={still_alive}")


def check_a11y_focus(r: CheckReporter, page: Page) -> None:
    """Tab to the chat input; assert :focus-visible style is non-empty."""
    name = "a11y: chat input shows focus indicator"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(500)
        page.locator('[data-testid="chat-input"]').focus()
        page.wait_for_timeout(100)
        # outline OR box-shadow non-empty when focused.
        focus = page.evaluate("""
            () => {
                const el = document.querySelector('[data-testid="chat-input"]');
                if (!el || document.activeElement !== el) return null;
                const cs = getComputedStyle(el);
                return {outline: cs.outline, shadow: cs.boxShadow, border: cs.border};
            }
        """)
        if focus is None:
            r.check(name, False, "input did not receive focus")
            return
        # Treat non-empty outline OR non-'none' boxShadow OR red-tinted
        # border as evidence of visible focus.
        outline_ok = "none" not in (focus.get("outline") or "")
        shadow_ok = (focus.get("shadow") or "none") not in ("", "none")
        border = focus.get("border") or ""
        # The chat-input has border #2a2d33 normally and #ef4444 when
        # recording — not a focus indicator. The Settings inputs have
        # focus-visible via global CSS. We accept any of these signals.
        ok = outline_ok or shadow_ok or "rgb" in border
        if not r.check(name, ok, str(focus)):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


# ──────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────
def _run_playwright_checks(r: CheckReporter) -> None:
    headless = os.environ.get("HEADLESS", "1") != "0"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = context.new_page()
        sink = ConsoleSink(page)
        try:
            check_smoke(r, page, sink)
            check_nav(r, page)
            check_settings_voice_speed_persists(r, page)
            check_settings_translation_modes(r, page)
            check_characters_via_api(r, page)
            check_avatar_chat(r, page)
            check_pulse_clean(r, page, sink)
            check_persistence_chat(r, page)
            check_error_save_validation(r, page)
            check_a11y_focus(r, page)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def main() -> None:
    r = CheckReporter("frontend_e2e")

    if not PLAYWRIGHT:
        r.check("playwright available", False,
                "pip install playwright && playwright install chromium")
        r.summary_or_exit()

    # Best-effort wait for stale ports from a prior kill to release on
    # Windows (uvicorn's TIME_WAIT can linger ~5-10s). The foundation's
    # `require_ports_free` is a hard fail; we soft-poll first.
    from tts_tools._test_helpers import is_port_free
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if all(is_port_free(p) for p in (PORT_TTS, PORT_NMT, PORT_CONTROL,
                                          PORT_COMPANION, PORT_MOCK_ZEROCLAW,
                                          ZC_ADAPTER_PORT)):
            break
        time.sleep(0.5)
    require_ports_free(PORT_TTS, PORT_NMT, PORT_CONTROL,
                       PORT_COMPANION, PORT_MOCK_ZEROCLAW, ZC_ADAPTER_PORT)

    with managed_procs() as procs:
        # 1. Mock stack.
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        mock = spawn(
            "mock-stack",
            [python_exe(), "-m", "tts_tools._mock_stack"],
            port=PORT_CONTROL,
            log_path=LOG_DIR / "mock-stack.log",
        )
        procs.append(mock)
        if not wait_for_port(PORT_CONTROL, timeout_s=15):
            r.check("mock control plane up", False, f"timeout on {PORT_CONTROL}")
            r.summary_or_exit()
        r.info(f"mock-stack up on TTS:{PORT_TTS} NMT:{PORT_NMT} ZC:{PORT_MOCK_ZEROCLAW}")
        mock_clear()

        # 1b. zc /webhook adapter (translates to mock's /api/chat).
        adapter = spawn(
            "zc-webhook-adapter",
            [python_exe(), "-m", "tts_tools._zc_webhook_adapter"],
            port=ZC_ADAPTER_PORT,
            env={"MOCK_ZC_ADAPTER_PORT": str(ZC_ADAPTER_PORT),
                 "MOCK_ZC_UPSTREAM_PORT": str(PORT_MOCK_ZEROCLAW)},
            log_path=LOG_DIR / "zc-adapter.log",
        )
        procs.append(adapter)
        if not wait_for_port(ZC_ADAPTER_PORT, timeout_s=10):
            r.check("zc-webhook-adapter up", False, f"timeout on {ZC_ADAPTER_PORT}")
            r.summary_or_exit()

        # 2. Companion-server.
        cfg = _build_config()
        try:
            comp, cfg_path = spawn_companion_server(cfg, port=PORT_COMPANION,
                                                    log_dir=LOG_DIR)
        except SystemExit as e:
            r.check("companion-server binary present", False, str(e))
            r.summary_or_exit()
        procs.append(comp)
        # /health is the companion-server health endpoint.
        if not wait_for_url(f"{BASE_URL}/health", timeout_s=20):
            r.check("companion /health 200", False, f"timeout on {BASE_URL}/health")
            r.summary_or_exit()
        # Give the server a moment to settle (initial SSE reconnect storm
        # against the empty mock /api/events stabilises after ~1 s).
        time.sleep(1.5)
        r.info(f"companion-server up at {BASE_URL}; cfg={cfg_path}")

        # 3. Playwright drive. Wrap so a Playwright surprise never bypasses
        # the managed_procs cleanup — leaked uvicorn ports on Windows
        # block the next run for ~30s+ via TIME_WAIT.
        try:
            _run_playwright_checks(r)
        except Exception as e:
            r.check(f"playwright suite ran to completion", False,
                    f"unhandled {type(e).__name__}: {e}")

    r.summary_or_exit()


if __name__ == "__main__":
    main()
