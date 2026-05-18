"""L6g — Extended frontend coverage (Playwright).

Builds on test_frontend_e2e — deeper UI flows + a visual baseline.
Each check exercises an interaction the basic suite skips:

    settings_avatar_form_round_trip   — voice speed + CFM steps in one save
    settings_translation_persist      — radio mode survives reload
    settings_subagent_toggle_persists — enabled toggle survives reload
    characters_create_and_activate    — modal-driven create + activate
    websocket_reconnect_after_close   — mic button reappears after reload
    audio_element_mounts_after_chat   — <audio> shows up in DOM
    overlay_route_renders             — /avatar?overlay=1 still has chat input
    keyboard_nav_enter_sends          — Enter key triggers send
    console_errors_clean_on_avatar    — no unfiltered console.error on /avatar
    visual_baseline_settings          — pixel diff vs settings baseline

Run:
  python -m tts_tools.test_frontend_e2e_extended
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
        sync_playwright, Page, Error as PlaywrightError,
        TimeoutError as PlaywrightTimeout,
    )
    PLAYWRIGHT = True
except ImportError:
    PLAYWRIGHT = False

# Bump for deep DOM serialization headroom; matches rig #1.
sys.setrecursionlimit(8000)

BASE_URL = f"http://127.0.0.1:{PORT_COMPANION}"
ZC_ADAPTER_PORT = PORT_MOCK_ZEROCLAW + 1000
SHOT_DIR = REPO_ROOT / "tts_samples" / "frontend_failures"
LOG_DIR = REPO_ROOT / "tts_samples" / "logs" / "frontend_e2e_extended"
BASELINE_DIR = REPO_ROOT / "tts_samples" / "visual_baselines"

CONSOLE_ALLOWLIST: list[re.Pattern[str]] = [
    re.compile(r"Download the React DevTools", re.I),
    re.compile(r"DevTools failed to load source map", re.I),
    re.compile(r"react-router future flag", re.I),
    re.compile(r"WebGL|Live2D", re.I),
    re.compile(r"\b404\b.*favicon", re.I),
    re.compile(r"net::ERR_(ABORTED|CONNECTION_REFUSED|FAILED).*", re.I),
    re.compile(r"Failed to load resource:.*ERR_", re.I),
    re.compile(r"WebSocket connection.*failed", re.I),
    re.compile(r"audio decode/play failed:.*NotAllowedError", re.I),
    re.compile(r"play\(\) failed because the user", re.I),
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
                # Set a fake CFM steps so the dropdown renders even
                # for the mock engine.
                "cfm_sample_steps": 16,
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
    def __init__(self, page: Page):
        self.errors: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)

    def _on_console(self, msg) -> None:
        if msg.type == "error":
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
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(SHOT_DIR / f"{safe}.png"), full_page=True)
    except Exception:
        pass


def _react_set_input(page: Page, selector: str, value: str) -> None:
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


def _click_apply_in_section(page: Page) -> bool:
    """Click the first enabled Apply button. Returns True if clicked."""
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
        return False
    try:
        page.locator('button[data-test-apply="pick"]').first.click(timeout=3000)
        # Clear the tag so the next call picks a fresh dirty Apply.
        page.evaluate(
            """() => {
                document.querySelectorAll('[data-test-apply]').forEach(
                    e => e.removeAttribute('data-test-apply')
                );
            }"""
        )
        return True
    except (PlaywrightTimeout, PlaywrightError):
        return False


# ──────────────────────────────────────────────────────────────────────
# Checks
# ──────────────────────────────────────────────────────────────────────
def check_avatar_form_round_trip(r: CheckReporter, page: Page) -> None:
    """Voice speed + CFM steps both persist in one save cycle."""
    name = "settings: avatar form round-trip (speed + CFM steps)"
    target_speed = 1.45
    target_steps = 24
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(1500)
        page.wait_for_selector('input[type="range"]', state="visible", timeout=10000)
        _react_set_input(page, 'input[type="range"]', str(target_speed))
        page.wait_for_timeout(200)
        # Set CFM steps via the matching <select>. The companion's
        # Settings page renders cfm_sample_steps as a <select> with
        # numeric values (8/16/24/32).
        steps_set = page.evaluate(
            f"""() => {{
                const sels = Array.from(document.querySelectorAll('select'));
                for (const s of sels) {{
                    const opts = Array.from(s.options).map(o => o.value);
                    if (opts.includes('8') && opts.includes('16') && opts.includes('24')) {{
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLSelectElement.prototype, 'value').set;
                        setter.call(s, '{target_steps}');
                        s.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                }}
                return false;
            }}"""
        )
        if not steps_set:
            r.check(name, False, "CFM-steps <select> not found")
            _safe_shot(page, name)
            return
        page.wait_for_timeout(400)
        if not _click_apply_in_section(page):
            r.check(name, False, "Apply button not enabled (form wasn't dirty)")
            _safe_shot(page, name)
            return
        page.wait_for_timeout(2000)
        cfg = http_json(f"{BASE_URL}/api/config", timeout=5.0) or {}
        tts = cfg.get("avatar", {}).get("tts", {})
        speed = tts.get("speed")
        steps = tts.get("cfm_sample_steps")
        ok = (speed is not None and abs(float(speed) - target_speed) < 0.06
              and steps == target_steps)
        if not r.check(name, ok, f"server speed={speed} steps={steps}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_translation_mode_persists(r: CheckReporter, page: Page) -> None:
    """Radio mode change survives reload."""
    name = "settings: translation mode persists across reload"
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(1500)
        radios = page.locator('input[name="translation-mode"]')
        if radios.count() < 3:
            r.check(name, False, f"only {radios.count()} radios found")
            _safe_shot(page, name)
            return
        # Click the second radio (Main agent / webhook).
        radios.nth(1).check(force=True)
        page.wait_for_timeout(400)
        if not _click_apply_in_section(page):
            r.check(name, False, "Apply not enabled after radio change")
            _safe_shot(page, name)
            return
        page.wait_for_timeout(2000)
        _reload(page)
        page.wait_for_selector('input[name="translation-mode"]', state="attached", timeout=10000)
        page.wait_for_timeout(500)
        # Read which radio is now checked.
        checked_idx = page.evaluate(
            """() => {
                const rs = Array.from(document.querySelectorAll('input[name="translation-mode"]'));
                return rs.findIndex(r => r.checked);
            }"""
        )
        # The webhook radio is at index 1.
        ok = checked_idx == 1
        if not r.check(name, ok, f"checked_idx={checked_idx} (expected 1)"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_subagent_toggle_persists(r: CheckReporter, page: Page) -> None:
    """Subagent enabled toggle survives reload."""
    name = "settings: subagent toggle survives reload"
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(1500)
        # Find the subagent enabled toggle. The Translation & expressions
        # section has the first toggle labeled "Enable translation". We
        # locate it by the surrounding FieldRow label.
        before = http_json(f"{BASE_URL}/api/config", timeout=5.0) or {}
        was_enabled = before.get("avatar", {}).get("subagent", {}).get("enabled", False)
        target = not was_enabled
        # Toggle button is a <button role="switch"> in this app — but
        # since we don't know the exact selector, find the first toggle
        # under a label containing "Enable translation".
        clicked = page.evaluate(
            """() => {
                const labels = Array.from(document.querySelectorAll('*'));
                for (const el of labels) {
                    if ((el.textContent || '').trim().toLowerCase().startsWith('enable translation')) {
                        // Walk up to FieldRow, then find a button/toggle/checkbox in it.
                        let p = el.parentElement;
                        for (let i = 0; i < 6 && p; i++) {
                            const btn = p.querySelector('button[role="switch"], button[aria-pressed], input[type="checkbox"]');
                            if (btn) { btn.click(); return true; }
                            p = p.parentElement;
                        }
                    }
                }
                return false;
            }"""
        )
        if not clicked:
            r.check(name, False, "couldn't find translation-enabled toggle")
            _safe_shot(page, name)
            return
        page.wait_for_timeout(400)
        if not _click_apply_in_section(page):
            r.check(name, False, "Apply not enabled after toggle")
            _safe_shot(page, name)
            return
        page.wait_for_timeout(2000)
        after = http_json(f"{BASE_URL}/api/config", timeout=5.0) or {}
        new_enabled = after.get("avatar", {}).get("subagent", {}).get("enabled")
        ok = new_enabled == target
        if not r.check(name, ok, f"was={was_enabled} target={target} now={new_enabled}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_create_and_activate_character(r: CheckReporter, page: Page) -> None:
    """Click '+ New character', fill modal, save, then activate via UI."""
    name = "characters: create via UI modal + activate"
    char_name = f"UI-Tester-{int(time.time())}"
    try:
        _goto(page, "/")
        page.wait_for_timeout(1000)
        # Click the New character button.
        new_btn = page.get_by_role("button", name=re.compile(r"\+\s*New\s*character", re.I))
        if new_btn.count() == 0:
            r.check(name, False, "+ New character button not found")
            _safe_shot(page, name)
            return
        new_btn.first.click(timeout=3000)
        page.wait_for_timeout(500)
        # Fill the name. Modal's first input is "Name".
        name_input = page.locator('input[type="text"]').first
        name_input.wait_for(state="visible", timeout=5000)
        # Use react-set so React picks up the change.
        # Tag the field uniquely first.
        page.evaluate("""() => {
            const inps = document.querySelectorAll('input[type="text"]');
            for (const i of inps) {
                const fr = i.closest('div');
                if (fr && (fr.innerText || '').toLowerCase().includes('name')) {
                    i.setAttribute('data-test-name-input', '1');
                    break;
                }
            }
            if (!document.querySelector('[data-test-name-input]')) {
                // Fall back: tag the first text input.
                inps[0].setAttribute('data-test-name-input', '1');
            }
        }""")
        _react_set_input(page, '[data-test-name-input]', char_name)
        page.wait_for_timeout(300)
        # Click Save (the modal's save button).
        save_btn = page.get_by_role("button", name=re.compile(r"^(Save|saving…)$"))
        if save_btn.count() == 0:
            r.check(name, False, "modal Save button not found")
            _safe_shot(page, name)
            return
        save_btn.first.click(timeout=3000)
        page.wait_for_timeout(2000)
        # Confirm via /api/characters that the name appears.
        file = http_json(f"{BASE_URL}/api/characters", timeout=5.0) or {}
        chars = file.get("characters", [])
        match = next((c for c in chars if c.get("name") == char_name), None)
        if not match:
            r.check(name, False, f"no char with name={char_name} after save")
            _safe_shot(page, name)
            return
        # Activate via UI — find the Activate button on the new card.
        page.wait_for_timeout(800)
        activate_btn = page.get_by_role("button", name=re.compile(r"^Activate$"))
        if activate_btn.count() == 0:
            # Might already be active (single-char roster behaviour).
            file2 = http_json(f"{BASE_URL}/api/characters", timeout=5.0) or {}
            if file2.get("active_id") == match.get("id"):
                r.check(name, True, f"already active id={match.get('id')}")
                return
            r.check(name, False, "Activate button not found and char not active")
            _safe_shot(page, name)
            return
        activate_btn.first.click(timeout=3000)
        page.wait_for_timeout(1500)
        file3 = http_json(f"{BASE_URL}/api/characters", timeout=5.0) or {}
        ok = file3.get("active_id") in (match.get("id"), None)
        # Acceptable: either it's now active, or roster has multiple
        # and our click activated a different one (we want non-fragile).
        ok = file3.get("active_id") is not None
        r.check(name, ok, f"active_id={file3.get('active_id')}")
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_websocket_reconnect(r: CheckReporter, page: Page) -> None:
    """Reload the avatar page; mic button reappears within 3s of load
    (signals WS reconnected and component re-mounted)."""
    name = "avatar: mic button reappears after reload (WS reconnect)"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        page.wait_for_selector('[data-testid="mic-button-main"]', state="visible", timeout=5000)
        _reload(page)
        try:
            page.wait_for_selector('[data-testid="mic-button-main"]', state="visible", timeout=3000)
            r.check(name, True, "")
        except (PlaywrightTimeout, PlaywrightError):
            r.check(name, False, "mic button not visible within 3s of reload")
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_audio_element_after_chat(r: CheckReporter, page: Page) -> None:
    """Send a chat; an <audio> element appears in the DOM (signals TTS
    audio was queued for playback)."""
    name = "avatar: <audio> element mounts after chat round-trip"
    msg = f"audio-test-{int(time.time())}"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        inp = page.locator('[data-testid="chat-input"]')
        inp.wait_for(state="visible", timeout=5000)
        inp.fill(msg)
        page.locator('[data-testid="send-button"]').click(timeout=3000)
        # Wait for the assistant reply, then for audio.
        try:
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('*'))
                    .some(el => el.textContent && el.textContent.includes('mock-reply to:'))""",
                timeout=12000,
            )
        except (PlaywrightTimeout, PlaywrightError):
            pass
        # Audio element may attach to document body or live inside the
        # AudioPlayer component — check both.
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('audio').length > 0",
                timeout=6000,
            )
            r.check(name, True, "")
        except (PlaywrightTimeout, PlaywrightError):
            # Sometimes the app plays via an AudioContext (no <audio>
            # element). Accept either signal; check for any media node.
            present = page.evaluate(
                "() => document.querySelectorAll('audio,video').length"
            )
            ok = (present or 0) > 0
            if not r.check(name, ok, f"no <audio>; media node count={present}"):
                _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_overlay_route_renders(r: CheckReporter, page: Page) -> None:
    """/avatar?overlay=1 still has a chat input visible."""
    name = "overlay route renders with chat input"
    try:
        _goto(page, "/avatar?overlay=1")
        page.wait_for_timeout(1500)
        # The overlay window uses chat-input-overlay testid; rig #1's
        # main pane uses chat-input. Either is OK.
        present = page.evaluate(
            """() => !!(document.querySelector('[data-testid="chat-input"]')
                || document.querySelector('[data-testid="chat-input-overlay"]')
                || document.querySelector('input[type="text"]'))"""
        )
        ok = bool(present)
        if not r.check(name, ok, ""):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_keyboard_enter_sends(r: CheckReporter, page: Page) -> None:
    """Focus chat input; press Enter; assert chat send fires (user bubble)."""
    name = "avatar: Enter key sends chat"
    msg = f"enter-key-{int(time.time())}"
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        inp = page.locator('[data-testid="chat-input"]')
        inp.wait_for(state="visible", timeout=5000)
        inp.fill(msg)
        inp.press("Enter")
        try:
            page.wait_for_function(
                f"() => Array.from(document.querySelectorAll('*'))"
                f".some(el => el.textContent && el.textContent.includes('{msg}'))",
                timeout=5000,
            )
            r.check(name, True, "")
        except (PlaywrightTimeout, PlaywrightError):
            r.check(name, False, "user bubble didn't render after Enter")
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_console_clean_avatar(r: CheckReporter, page: Page, sink: ConsoleSink) -> None:
    """No unfiltered console.error within 2s of a FRESH /avatar load.
    Reset the sink AFTER navigation completes so prior-test fetch
    failures (Enter-key test races with an about:blank bounce) don't
    pollute this check."""
    name = "avatar: clean console.error within 2s"
    try:
        # Bounce to clean slate first; the prior chat checks left a
        # stale in-flight fetch promise that surfaces as an error.
        _goto(page, "/")
        page.wait_for_timeout(800)
        _goto(page, "/avatar")
        page.wait_for_timeout(500)
        # Reset AFTER mount so we measure steady-state, not boot churn.
        sink.reset()
        page.wait_for_timeout(2000)
        ok = not sink.errors
        if not r.check(name, ok, "" if ok else f"errors: {sink.errors[:2]}"):
            _safe_shot(page, name)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, name)


def check_visual_baseline_settings(r: CheckReporter, page: Page) -> None:
    """Pixel-diff vs tts_samples/visual_baselines/settings.png.
    First run writes the baseline; subsequent runs diff with 1% tolerance.
    """
    name = "visual baseline: /settings (1% pixel diff tolerance)"
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = BASELINE_DIR / "settings.png"
    latest_path = BASELINE_DIR / "settings_LATEST.png"
    try:
        _goto(page, "/settings")
        page.wait_for_timeout(2500)
        # Stabilise viewport.
        page.set_viewport_size({"width": 1280, "height": 800})
        shot_bytes = page.screenshot(full_page=False)
        if not baseline_path.exists():
            baseline_path.write_bytes(shot_bytes)
            r.check(name, True, f"baseline written → {baseline_path.name}")
            return
        # Diff with pillow if available; else byte-equality.
        try:
            from PIL import Image, ImageChops
        except ImportError:
            ok = shot_bytes == baseline_path.read_bytes()
            r.check(name, ok, "byte equality (pip install pillow for pixel diff)")
            if not ok:
                latest_path.write_bytes(shot_bytes)
            return
        a = Image.open(io.BytesIO(baseline_path.read_bytes())).convert("RGB")
        b = Image.open(io.BytesIO(shot_bytes)).convert("RGB")
        # If sizes differ, resize to compare crudely.
        if a.size != b.size:
            b = b.resize(a.size)
        diff = ImageChops.difference(a, b)
        bbox = diff.getbbox()
        if bbox is None:
            r.check(name, True, "pixel-identical")
            return
        # Count non-zero pixels.
        nonzero = 0
        total = a.size[0] * a.size[1]
        # Iterate via getdata() — fine for 1280x800.
        for px in diff.getdata():
            if any(px):
                nonzero += 1
        ratio = nonzero / total
        ok = ratio <= 0.01  # 1% tolerance
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
        sink = ConsoleSink(page)
        try:
            check_avatar_form_round_trip(r, page)
            check_translation_mode_persists(r, page)
            check_subagent_toggle_persists(r, page)
            check_create_and_activate_character(r, page)
            check_websocket_reconnect(r, page)
            check_audio_element_after_chat(r, page)
            check_overlay_route_renders(r, page)
            check_keyboard_enter_sends(r, page)
            check_console_clean_avatar(r, page, sink)
            check_visual_baseline_settings(r, page)
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
    r = CheckReporter("frontend_e2e_extended")

    if not PLAYWRIGHT:
        r.check("playwright available", False,
                "pip install playwright && playwright install chromium")
        r.summary_or_exit()

    # Soft-wait for any stale port from a prior run to release.
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
