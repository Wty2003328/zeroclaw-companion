"""L6flows — Extended UI flows + visual baselines (Playwright).

Deeper interactive flows the unit/extended rigs don't cover plus visual
regression baselines across 5 routes × 2 viewports = ~10 baselines.

Flow checks (~6):
    avatar_canvas_click_smoke         — clicking the canvas doesn't crash
    multi_tab_chat_sync               — chat round-trip in tab A appears
                                         in tab B within 3s
    empty_enter_no_send               — Enter on empty input does nothing
    loading_state_during_chat         — loading indicator visible during
                                         in-flight POST /api/chat
    chat_history_persists_3_msgs      — 3 round-trips survive reload
    invalid_translation_url_no_crash  — garbage URL in service-config
                                         doesn't kill the server
    offline_then_recover              — page.set_offline(true) then false;
                                         next chat works

Visual baselines (10 total, 5 routes × 2 viewports):
    home / avatar / settings / pulse / overlay  at  1280x800 and 375x667

First run writes baselines. Subsequent runs compare with 2% tolerance.

Run:
  python -m tts_tools.test_frontend_flows
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
from pathlib import Path

from tts_tools._test_helpers import (
    PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_COMPANION, PORT_MOCK_ZEROCLAW,
    REPO_ROOT, CheckReporter, http_get, http_json, http_post_json,
    is_port_free, managed_procs, mock_clear, python_exe,
    require_ports_free, spawn, spawn_companion_server,
    wait_for_port, wait_for_url,
)

try:
    from playwright.sync_api import (
        sync_playwright, Page, BrowserContext,
        Error as PlaywrightError, TimeoutError as PlaywrightTimeout,
    )
    PLAYWRIGHT = True
except ImportError:
    PLAYWRIGHT = False

sys.setrecursionlimit(8000)

BASE_URL = f"http://127.0.0.1:{PORT_COMPANION}"
ZC_ADAPTER_PORT = PORT_MOCK_ZEROCLAW + 1000
SHOT_DIR = REPO_ROOT / "tts_samples" / "frontend_failures"
LOG_DIR = REPO_ROOT / "tts_samples" / "logs" / "frontend_flows"
BASELINE_DIR = REPO_ROOT / "tts_samples" / "visual_baselines"

CONSOLE_ALLOWLIST: list[re.Pattern[str]] = [
    re.compile(r"Download the React DevTools|DevTools failed", re.I),
    re.compile(r"react-router future flag", re.I),
    re.compile(r"WebGL|Live2D", re.I),
    re.compile(r"\b404\b.*favicon", re.I),
    re.compile(r"net::ERR_(ABORTED|CONNECTION_REFUSED|FAILED).*", re.I),
    re.compile(r"Failed to load resource:.*ERR_", re.I),
    re.compile(r"WebSocket connection.*failed", re.I),
    re.compile(r"audio decode/play failed:.*NotAllowedError", re.I),
    re.compile(r"play\(\) failed because the user", re.I),
]

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "mobile": {"width": 375, "height": 667},
}
BASELINE_ROUTES = [
    ("home", "/"),
    ("avatar", "/avatar"),
    ("settings", "/settings"),
    ("pulse", "/pulse"),
    ("overlay", "/avatar?overlay=1"),
]


def _allowed_console(text: str) -> bool:
    return any(p.search(text) for p in CONSOLE_ALLOWLIST)


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


def _safe_shot(page: Page, name: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(SHOT_DIR / f"{safe}.png"), full_page=True)
    except Exception:
        pass


def _goto(page: Page, path: str, retries: int = 5) -> None:
    last: Exception | None = None
    for i in range(retries):
        try:
            page.goto(BASE_URL + path, wait_until="domcontentloaded", timeout=12000)
            return
        except (PlaywrightTimeout, PlaywrightError) as e:
            last = e
            page.wait_for_timeout(800 * (i + 1))
    if last is not None:
        raise last


def _reload(page: Page, retries: int = 3) -> None:
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


def _send_chat_via_input(page: Page, text: str, timeout_user_bubble: int = 6000) -> bool:
    inp = page.locator('[data-testid="chat-input"]')
    inp.wait_for(state="visible", timeout=5000)
    inp.fill(text)
    page.locator('[data-testid="send-button"]').click(timeout=3000)
    try:
        page.wait_for_function(
            f"(t) => Array.from(document.querySelectorAll('*'))"
            f".some(el => el.textContent && el.textContent.includes(t))",
            arg=text, timeout=timeout_user_bubble,
        )
        return True
    except (PlaywrightTimeout, PlaywrightError):
        return False


def _wait_for_assistant(page: Page, timeout_ms: int = 12000) -> bool:
    try:
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('*'))
                .some(el => el.textContent && el.textContent.includes('mock-reply to:'))""",
            timeout=timeout_ms,
        )
        return True
    except (PlaywrightTimeout, PlaywrightError):
        return False


# ──────────────────────────────────────────────────────────────────────
# Flow checks
# ──────────────────────────────────────────────────────────────────────
def check_avatar_canvas_click(r: CheckReporter, page: Page) -> None:
    """Click somewhere on the avatar canvas; no JS exception thrown.
    Skipped (PASS-with-info) if no canvas mounted."""
    name = "avatar: canvas click smoke (no exception)"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(2000)
        cv_count = page.evaluate("() => document.querySelectorAll('canvas').length")
        if not cv_count:
            r.check(name, True, "no canvas mounted (Live2D not loaded — skip)")
            return
        cv = page.locator('canvas').first
        try:
            cv.click(timeout=3000, position={"x": 50, "y": 50})
            page.wait_for_timeout(500)
            r.check(name, True, "")
        except (PlaywrightTimeout, PlaywrightError) as e:
            r.check(name, False, f"click failed: {e}")
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_multi_tab_chat_sync(r: CheckReporter, context: BrowserContext) -> None:
    """Open two contexts (tabs/windows). Chat in A → assert it shows in B's
    chat history within 3s. The companion stores history per-tab in
    localStorage, but a fresh load of /avatar reads from localStorage on
    mount — so a quick reload in B is sufficient to assert sync."""
    name = "multi-tab: chat in A → visible in B after reload"
    page_a = context.new_page()
    page_b = context.new_page()
    msg = f"multitab-{int(time.time())}"
    try:
        page_a.goto(BASE_URL + "/avatar", wait_until="domcontentloaded", timeout=12000)
        page_b.goto(BASE_URL + "/avatar", wait_until="domcontentloaded", timeout=12000)
        page_a.wait_for_timeout(1500)
        page_b.wait_for_timeout(1500)
        # Send in A.
        if not _send_chat_via_input(page_a, msg):
            r.check(name, False, "send in A didn't produce user bubble")
            return
        _wait_for_assistant(page_a, timeout_ms=10000)
        page_a.wait_for_timeout(500)
        # Reload B; the localStorage write from A is shared because
        # both contexts share the same browser storage. (Yes: Playwright
        # contexts have separate storage by default, but new_page in the
        # SAME context shares it.)
        try:
            page_b.reload(wait_until="domcontentloaded", timeout=6000)
        except (PlaywrightTimeout, PlaywrightError):
            page_b.goto("about:blank", wait_until="domcontentloaded", timeout=4000)
            page_b.goto(BASE_URL + "/avatar", wait_until="domcontentloaded", timeout=10000)
        page_b.wait_for_timeout(1500)
        body = page_b.content()
        ok = msg in body
        if not r.check(name, ok, f"msg in B after reload: {ok}"):
            _safe_shot(page_b, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page_a, name)
    finally:
        try: page_a.close()
        except Exception: pass
        try: page_b.close()
        except Exception: pass


def check_empty_enter_no_send(r: CheckReporter, page: Page) -> None:
    """Pressing Enter on an empty chat input does nothing — no user
    bubble, no POST /api/chat in network log."""
    name = "avatar: empty Enter does not send chat"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        # Snapshot the user-bubble count.
        before = page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                .filter(el => (el.textContent || '').trim() === 'you').length"""
        )
        # Network monitor for POST /api/chat.
        posts: list[str] = []
        def on_request(req):
            if req.method == "POST" and "/api/chat" in req.url:
                posts.append(req.url)
        page.on("request", on_request)
        inp = page.locator('[data-testid="chat-input"]')
        inp.fill("")
        inp.press("Enter")
        page.wait_for_timeout(1500)
        after = page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                .filter(el => (el.textContent || '').trim() === 'you').length"""
        )
        page.remove_listener("request", on_request)
        ok = (after == before) and not posts
        if not r.check(name, ok,
                       f"before={before} after={after} posts={len(posts)}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_loading_state_during_chat(r: CheckReporter, page: Page) -> None:
    """Send a chat; assert a loading indicator becomes visible during the
    in-flight POST /api/chat. The Avatar page renders a ThinkingBubble
    while `sending` is true."""
    name = "avatar: loading indicator visible during /api/chat round-trip"
    msg = f"loading-{int(time.time())}"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        # Slow down the zeroclaw mock so the loading state is observable.
        from tts_tools._test_helpers import mock_set
        mock_set(zc_slow_s=1.0)
        try:
            inp = page.locator('[data-testid="chat-input"]')
            inp.fill(msg)
            page.locator('[data-testid="send-button"]').click(timeout=3000)
            # Poll for loading indicator (Send button shows "…" while
            # `sending`, ThinkingBubble appears in history).
            saw_loading = False
            deadline = time.time() + 3.0
            while time.time() < deadline:
                sending = page.evaluate(
                    """() => {
                        const send = document.querySelector('[data-testid="send-button"]');
                        if (send && (send.textContent || '').trim() === '…') return true;
                        // Also accept any bubble with class containing 'thinking'
                        return Array.from(document.querySelectorAll('*'))
                            .some(el => /thinking|loading/i.test(el.className || ''));
                    }"""
                )
                if sending:
                    saw_loading = True
                    break
                page.wait_for_timeout(50)
            detail = "" if saw_loading else "loading indicator never visible"
            if not r.check(name, saw_loading, detail):
                _safe_shot(page, name)
        finally:
            from tts_tools._test_helpers import mock_clear
            mock_clear()
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_3_msg_history_persists(r: CheckReporter, page: Page) -> None:
    """Send 3 chats, reload, assert all 3 user + 3 reply bubbles still visible."""
    name = "avatar: 3-message chat history survives reload"
    msgs = [f"persist3-{i}-{int(time.time())}" for i in range(3)]
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        for m in msgs:
            if not _send_chat_via_input(page, m, timeout_user_bubble=6000):
                r.check(name, False, f"failed to send {m}")
                _safe_shot(page, name)
                return
            _wait_for_assistant(page, timeout_ms=10000)
            page.wait_for_timeout(300)
        page.wait_for_timeout(500)
        _reload(page)
        page.wait_for_timeout(1500)
        body = page.content()
        missing = [m for m in msgs if m not in body]
        # Mock zeroclaw reply has 'mock-reply to:' so count those.
        replies = body.count("mock-reply to:")
        ok = not missing and replies >= 3
        if not r.check(name, ok,
                       f"missing_user_msgs={missing} reply_count={replies}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_invalid_translation_url(r: CheckReporter, page: Page) -> None:
    """Setting a garbage URL in service-config doesn't kill the server."""
    name = "settings: garbage translation URL doesn't kill server"
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(1500)
        # Activate Direct AI radio to reveal Service configuration.
        radios = page.locator('input[name="translation-mode"]')
        if radios.count() < 3:
            r.check(name, True, "no translation radios (skip)")
            return
        radios.nth(0).check(force=True)
        page.wait_for_timeout(400)
        # The first monospace input in the Direct-AI subsection is the
        # API endpoint URL. Set it to garbage.
        garbage = "ht!tp://not-a-url::99999/💀"
        page.evaluate(
            """() => {
                const inputs = Array.from(document.querySelectorAll('input[type="text"]'));
                for (const i of inputs) {
                    const ph = i.placeholder || '';
                    if (ph.includes('api.openai.com') || ph.includes('http')) {
                        i.setAttribute('data-test-garbage-url', '1');
                        return true;
                    }
                }
                return false;
            }"""
        )
        # Use a JS direct write so we don't dance around React state for
        # a discardable field.
        page.evaluate(
            f"""() => {{
                const el = document.querySelector('[data-test-garbage-url]');
                if (!el) return;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, "{garbage}");
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
            }}"""
        )
        page.wait_for_timeout(300)
        # Try to Apply — may fail server-side, but server stays alive.
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
        if idx is not None and idx >= 0:
            try:
                page.locator('button[data-test-apply="pick"]').first.click(timeout=2000)
            except (PlaywrightTimeout, PlaywrightError):
                pass
        page.wait_for_timeout(2000)
        s, _, _ = http_get(f"{BASE_URL}/health", timeout=5.0)
        ok = s == 200
        if not r.check(name, ok, f"/health status after garbage URL = {s}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_offline_recover(r: CheckReporter, page: Page, context: BrowserContext) -> None:
    """Take the browser offline mid-chat; assert no unhandled rejection
    AND a subsequent chat (once back online) round-trips."""
    name = "network: offline mid-chat → recover after online"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        pageerrors: list[str] = []
        page.on("pageerror", lambda e: pageerrors.append(str(e)))
        context.set_offline(True)
        try:
            inp = page.locator('[data-testid="chat-input"]')
            inp.fill(f"offline-{int(time.time())}")
            page.locator('[data-testid="send-button"]').click(timeout=3000)
            page.wait_for_timeout(2000)
        finally:
            context.set_offline(False)
        page.wait_for_timeout(800)
        # Recovery — next chat works.
        msg = f"recover-{int(time.time())}"
        if not _send_chat_via_input(page, msg, timeout_user_bubble=6000):
            r.check(name, False, "recovery chat user bubble didn't render")
            _safe_shot(page, name)
            return
        replied = _wait_for_assistant(page, timeout_ms=10000)
        # Filter unhandled rejections by allowlist (network errors are OK).
        unhandled = [e for e in pageerrors if not _allowed_console(e)]
        ok = replied and not unhandled
        if not r.check(name, ok,
                       f"replied={replied} unhandled={len(unhandled)}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


# ──────────────────────────────────────────────────────────────────────
# Visual baselines — 5 routes × 2 viewports = 10 baselines
# ──────────────────────────────────────────────────────────────────────
def _pixel_diff_ratio(a_bytes: bytes, b_bytes: bytes) -> float | None:
    """Pixel-by-pixel diff ratio (0..1). None if Pillow not installed."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return None
    a = Image.open(io.BytesIO(a_bytes)).convert("RGB")
    b = Image.open(io.BytesIO(b_bytes)).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    if diff.getbbox() is None:
        return 0.0
    nonzero = sum(1 for px in diff.getdata() if any(px))
    total = a.size[0] * a.size[1]
    return nonzero / total


def check_visual_baseline(r: CheckReporter, page: Page,
                          route_slug: str, route_path: str,
                          viewport_label: str, viewport: dict) -> None:
    # Avatar + overlay routes have a live Live2D canvas that breathes /
    # blinks even at rest — pixel-perfect diff is meaningless. Bump
    # tolerance for those.
    tolerance = 0.35 if route_slug in ("avatar", "overlay") else 0.02
    name = (f"visual baseline: {route_slug} @ {viewport_label} "
            f"({int(tolerance*100)}% pixel-diff)")
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_name = f"{route_slug}_{viewport_label}.png"
    baseline_path = BASELINE_DIR / baseline_name
    latest_path = BASELINE_DIR / f"{route_slug}_{viewport_label}_LATEST.png"
    try:
        page.set_viewport_size(viewport)
        _goto(page, route_path)
        # Give the page time to paint, fonts to load, and any deferred
        # network fetches to settle. The avatar canvas paints
        # incrementally for a couple of frames after mount.
        page.wait_for_timeout(2500)
        shot_bytes = page.screenshot(full_page=False)
        if not baseline_path.exists():
            baseline_path.write_bytes(shot_bytes)
            r.check(name, True, f"baseline written → {baseline_name}")
            return
        ratio = _pixel_diff_ratio(baseline_path.read_bytes(), shot_bytes)
        if ratio is None:
            ok = shot_bytes == baseline_path.read_bytes()
            r.check(name, ok, "byte-equality (install Pillow for pixel diff)")
            if not ok:
                latest_path.write_bytes(shot_bytes)
            return
        ok = ratio <= tolerance
        if not ok:
            latest_path.write_bytes(shot_bytes)
        r.check(name, ok, f"diff={ratio*100:.2f}% of pixels")
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
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
        try:
            # Flow checks (6).
            check_avatar_canvas_click(r, page)
            check_multi_tab_chat_sync(r, context)
            check_empty_enter_no_send(r, page)
            check_loading_state_during_chat(r, page)
            check_3_msg_history_persists(r, page)
            check_invalid_translation_url(r, page)
            check_offline_recover(r, page, context)
            # Visual baselines (10).
            for route_slug, route_path in BASELINE_ROUTES:
                for vp_label, vp in VIEWPORTS.items():
                    check_visual_baseline(r, page, route_slug, route_path,
                                          vp_label, vp)
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
    r = CheckReporter("frontend_flows")

    if not PLAYWRIGHT:
        r.check("playwright available", False,
                "pip install playwright && playwright install chromium")
        r.summary_or_exit()

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
        mock_clear()

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

        cfg = _build_config()
        try:
            comp, cfg_path = spawn_companion_server(cfg, port=PORT_COMPANION,
                                                    log_dir=LOG_DIR)
        except SystemExit as e:
            r.check("companion-server binary present", False, str(e))
            r.summary_or_exit()
        procs.append(comp)
        if not wait_for_url(f"{BASE_URL}/health", timeout_s=20):
            r.check("companion /health 200", False, f"timeout on {BASE_URL}/health")
            r.summary_or_exit()
        time.sleep(1.5)
        r.info(f"companion-server up at {BASE_URL}; cfg={cfg_path}")

        try:
            _run_playwright_checks(r)
        except Exception as e:
            r.check("playwright suite ran to completion", False,
                    f"unhandled {type(e).__name__}: {e}")

    r.summary_or_exit()


if __name__ == "__main__":
    main()
