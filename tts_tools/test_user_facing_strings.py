"""P3 — User-facing string lints (Playwright).

Scans every user-visible string in the UI for patterns that indicate
internals leaking into the user surface. Each pattern class is one
check; the rig drives the UI through specific error/edge states to
provoke the patterns before scanning.

Patterns (FAIL if any user-visible text matches):

  raw_http_code      ^\\[error \\d+\\]              (e.g. "[error 502]")
  windows_path       [A-Z]:[\\/]                    (e.g. "C:/Users/...")
  python_traceback   File "....py", line \\d+        (Python tracebacks)
  debug_labels       \\[debug\\] | \\[trace\\] | TODO | FIXME
  stack_fragment     "at <" | \\bThread\\b | "Traceback "
  shell_quoting      &&  ||  set\\s+\\w+=\\s*\\S+\\s*&&   (in input default values)

Sources scanned per state:
  - chat bubbles                .ws-bubble-enter, [class*="bubble"]
  - toasts / alerts             .toast, [role="alert"], [role="status"]
  - buttons + labels            button, label
  - input placeholders          input[placeholder]
  - input values (Settings)     input[value], select[value]

Provoked states:
  1. mock_set(zc_status=502)    — chat → check no "[error 502]" leaks
  2. mock_set(tts_status=503)   — chat → check no traceback/path leaks
  3. Open Settings → switch     — check no advanced field defaults
     between TTS engines           contain shell-quoting hell

Failure mode: each pattern class is a separate check. Screenshots saved
under tts_samples/string_lint_failures/<pattern>.png plus the offending
text in the failure detail.

Run:
  python -m tts_tools.test_user_facing_strings
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from tts_tools._test_helpers import (
    PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_COMPANION, PORT_MOCK_ZEROCLAW,
    REPO_ROOT, CheckReporter, http_get, http_json, http_post_json,
    is_port_free, managed_procs, mock_clear, mock_set, python_exe,
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
SHOT_DIR = REPO_ROOT / "tts_samples" / "string_lint_failures"
LOG_DIR = REPO_ROOT / "tts_samples" / "logs" / "user_facing_strings"


# ─── Forbidden patterns ────────────────────────────────────────────── #
# Each entry: (slug, description, pattern, scope)
#   scope=="chat" → only check chat-message + toast strings
#   scope=="ui"   → buttons/labels/placeholders (everywhere visible)
#   scope=="input"→ input default values (Settings)
PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    ("raw_http_code", "no `[error <code>]` in chat bubbles",
     re.compile(r"^\s*\[error\s+\d+\]", re.M | re.I), "chat"),
    ("windows_path", "no Windows paths in user-visible text",
     # `C:\` or `C:/` or `D:/...` — but exempt UI placeholder examples
     # like "C:/Users/.../python.exe" which are legitimate hints.
     re.compile(r"[A-Z]:[\\/](?:Users|Program|Windows|Temp|Documents|Desktop|companion|tts_lab)\b",
                re.I), "chat"),
    ("python_traceback", "no Python tracebacks in user-visible text",
     re.compile(r'File "[^"]+\.py", line \d+'), "chat"),
    ("debug_labels", "no [debug]/[trace]/TODO/FIXME in user-visible UI",
     re.compile(r"\[(?:debug|trace)\]|\bTODO\b|\bFIXME\b", re.I), "ui"),
    ("stack_fragment", "no stack-trace fragments in user-visible text",
     re.compile(r"\bat <[^>]+>|^Traceback\b|^\s+at\s+\w+\.\w+", re.M), "chat"),
    ("shell_quoting", "no shell-quoting hell in default field values",
     # We treat `&&` / `||` as red flags inside an input's default
     # value, but only when they appear with other shell syntax (avoid
     # false positives like "use && to chain"). Look for the classic
     # `set VAR=... &&` or `cmd && cmd` pattern.
     re.compile(r"(?:set\s+\w+=[^&|]*&&\s*\w)|(?:&&\s*\w+\s+[-/])"), "input"),
]


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


def _safe_shot(page: Page, slug: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", slug)[:80]
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


def _send_chat(page: Page, text: str, wait_assistant_ms: int = 8000) -> None:
    inp = page.locator('[data-testid="chat-input"]')
    inp.wait_for(state="visible", timeout=5000)
    inp.fill(text)
    page.locator('[data-testid="send-button"]').click(timeout=3000)
    page.wait_for_timeout(wait_assistant_ms)


# ─── Visible-text harvesters ──────────────────────────────────────── #
def _harvest_chat_visible(page: Page) -> list[str]:
    """Pull every visible chat-bubble + toast/alert text."""
    return page.evaluate(
        """() => {
            const out = [];
            const selectors = [
                '.ws-bubble-enter',
                '[class*="bubble" i]',
                '.toast',
                '[role="alert"]',
                '[role="status"]',
                '[data-testid="voice-error"]',
            ];
            const seen = new Set();
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                    if (seen.has(el)) return;
                    seen.add(el);
                    const t = (el.innerText || '').trim();
                    if (t) out.push(t);
                });
            }
            return out;
        }"""
    )


def _harvest_ui_visible(page: Page) -> list[str]:
    """Pull every visible button/label text + input placeholders."""
    return page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('button, label').forEach(el => {
                const t = (el.innerText || '').trim();
                if (t) out.push(t);
            });
            document.querySelectorAll('input[placeholder]').forEach(el => {
                const t = (el.placeholder || '').trim();
                if (t) out.push(t);
            });
            // Also section descriptions / hints.
            document.querySelectorAll('p, span').forEach(el => {
                // Avoid huge dumps — only short visible strings.
                const t = (el.innerText || '').trim();
                if (t && t.length < 400) out.push(t);
            });
            return out;
        }"""
    )


def _harvest_input_values(page: Page) -> list[str]:
    """Pull every input/textarea/select value on the page."""
    return page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('input, textarea').forEach(el => {
                const v = el.value;
                if (typeof v === 'string' && v.trim()) out.push(v);
            });
            document.querySelectorAll('select').forEach(el => {
                const v = el.value;
                if (typeof v === 'string' && v.trim()) out.push(v);
            });
            return out;
        }"""
    )


def _match_pattern(strings: list[str], pat: re.Pattern[str]) -> list[str]:
    """Return offending strings (up to 5 examples) that match `pat`."""
    hits = []
    for s in strings:
        if pat.search(s):
            hits.append(s.strip().split("\n")[0][:200])
            if len(hits) >= 5:
                break
    return hits


# ─── Check entry points ───────────────────────────────────────────── #
def provoke_zc_502_chat(page: Page) -> None:
    """Force zeroclaw to 502 then send a chat. Bubble should NOT show
    literal `[error 502]`."""
    mock_set(zc_status=502)
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        _send_chat(page, f"prov502-{int(time.time())}", wait_assistant_ms=6000)
    finally:
        mock_clear()


def provoke_tts_503_chat(page: Page) -> None:
    """Force TTS to 503 + send a chat. Path/traceback patterns should
    NOT surface in any visible UI text."""
    mock_set(tts_status=503)
    try:
        _goto(page, "/avatar")
        page.wait_for_timeout(1500)
        _send_chat(page, f"prov503-{int(time.time())}", wait_assistant_ms=6000)
    finally:
        mock_clear()


def open_settings_engine_switch(page: Page) -> None:
    """Open Settings; cycle through the engine <select> options so every
    engine's default fields render. The user's report flagged that
    GPT-SoVITS rendered shell-quoting hell in its launch_command field
    default."""
    _goto(page, "/settings")
    page.wait_for_timeout(1500)
    try:
        # Find the engine select and step through its options.
        engines = page.evaluate(
            """() => {
                const sels = Array.from(document.querySelectorAll('select'));
                for (const s of sels) {
                    const opts = Array.from(s.options).map(o => o.value);
                    if (opts.some(v => /gpt-?sovits|qwen3|openai|kokoro|edge-?tts|alltalk/i.test(v))) {
                        s.setAttribute('data-test-engine-select', '1');
                        return opts;
                    }
                }
                return [];
            }"""
        )
        if not engines:
            return
        for v in engines:
            if v == "__custom":
                continue
            page.evaluate(
                f"""() => {{
                    const el = document.querySelector('[data-test-engine-select]');
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLSelectElement.prototype, 'value').set;
                    setter.call(el, "{v}");
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}"""
            )
            page.wait_for_timeout(400)
    except (PlaywrightTimeout, PlaywrightError):
        pass


def check_pattern(r: CheckReporter, page: Page, slug: str, desc: str,
                  pat: re.Pattern[str], scope: str) -> None:
    """Harvest the right text source for `scope`, match against `pat`,
    report PASS if no hits; FAIL with example otherwise."""
    name = f"P3 lint: {desc}"
    try:
        if scope == "chat":
            strings = _harvest_chat_visible(page)
        elif scope == "ui":
            strings = _harvest_ui_visible(page)
        elif scope == "input":
            strings = _harvest_input_values(page)
        else:
            r.check(name, False, f"unknown scope: {scope}")
            return
        hits = _match_pattern(strings, pat)
        ok = not hits
        if not ok:
            _safe_shot(page, slug)
            detail = f"matched {len(hits)} → first: {hits[0]!r}"
        else:
            detail = f"scanned {len(strings)} strings, no matches"
        r.check(name, ok, detail)
    except (PlaywrightTimeout, PlaywrightError, RecursionError) as e:
        r.check(name, False, f"{type(e).__name__}: {e}")
        _safe_shot(page, slug)


# ─── Orchestration ─────────────────────────────────────────────────── #
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
            # State 1: zc 502 chat.
            provoke_zc_502_chat(page)
            for slug, desc, pat, scope in PATTERNS:
                if scope == "chat":
                    # raw_http_code, windows_path, python_traceback, stack_fragment
                    if slug in ("raw_http_code", "stack_fragment"):
                        check_pattern(r, page, slug, desc + " [zc 502]", pat, scope)
                    # other chat-scoped lints are duplicated under state 2;
                    # we keep them grouped.

            # State 2: tts 503 chat.
            provoke_tts_503_chat(page)
            for slug, desc, pat, scope in PATTERNS:
                if scope == "chat" and slug in ("windows_path", "python_traceback"):
                    check_pattern(r, page, slug, desc + " [tts 503]", pat, scope)

            # State 3: Settings + engine switching.
            open_settings_engine_switch(page)
            for slug, desc, pat, scope in PATTERNS:
                if scope == "ui":
                    check_pattern(r, page, slug, desc, pat, scope)
                if scope == "input":
                    check_pattern(r, page, slug, desc, pat, scope)
        finally:
            try: context.close()
            except Exception: pass
            try: browser.close()
            except Exception: pass


def main() -> None:
    r = CheckReporter("user_facing_strings")

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
