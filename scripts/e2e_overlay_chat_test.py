"""Verify the desktop-pet overlay can talk to Asuna AND the main
window records the user turn from it.

Setup mirrors Tauri's two windows:
  - Page A (main): http://127.0.0.1:9181/avatar
  - Page B (overlay): http://127.0.0.1:9181/avatar?overlay=1

User types in the OVERLAY (small chat input). /api/chat fires; the
companion-server broadcasts a UserMessage WS frame to all clients;
the main window's onUserMessage handler appends the user turn to
its history.

Asserts:
  - Overlay's compact chat bar is visible.
  - Main window receives the user turn (visible bubble + localStorage).
  - Assistant turn also lands in main's history.

Run: python scripts/e2e_overlay_chat_test.py
"""

from __future__ import annotations

import io
import json
import sys

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MAIN = "http://127.0.0.1:9181/avatar"
OVERLAY = "http://127.0.0.1:9181/avatar?overlay=1"
MSG = "ping-from-overlay-chatbar"


def history_role_counts(page) -> dict:
    raw = page.evaluate("() => localStorage.getItem('companion.chatHistory.v1')")
    parsed = json.loads(raw) if raw else None
    counts: dict[str, int] = {}
    if isinstance(parsed, list):
        for t in parsed:
            r = t.get("role")
            counts[r] = counts.get(r, 0) + 1
    return counts


def main() -> int:
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        boot = ctx.new_page()
        boot.goto(MAIN, wait_until="domcontentloaded", timeout=30000)
        boot.evaluate("() => localStorage.clear()")
        boot.close()

        page_main = ctx.new_page()
        page_overlay = ctx.new_page()
        page_main.goto(MAIN, wait_until="domcontentloaded", timeout=30000)
        page_overlay.goto(OVERLAY, wait_until="domcontentloaded", timeout=30000)
        page_main.wait_for_load_state("networkidle", timeout=15000)
        page_overlay.wait_for_load_state("networkidle", timeout=15000)

        # Find the compact chat bar in the overlay. There's no big
        # "Chat history" panel in the overlay; the only input element
        # is the compact one we render at the bottom.
        overlay_input = page_overlay.locator('input[type="text"]').first
        try:
            overlay_input.wait_for(state="attached", timeout=10000)
            print("✓ overlay rendered a compact chat input")
        except Exception as e:
            fails += 1
            print(f"✗ overlay has no chat input attached: {e}")
            browser.close()
            return 1

        # Reveal the chat bar (it has pointer-events:none until hover).
        # In real Tauri the cursor naturally hovers when the user moves
        # toward the pet; in headless we dispatch the bubbling mouseover
        # that React's polyfilled onMouseEnter listens for.
        page_overlay.locator('[data-tauri-drag-region=""]').first.dispatch_event(
            "mouseover"
        )
        page_overlay.wait_for_timeout(300)

        # Type + submit from the OVERLAY page.
        overlay_input.click(force=True)
        overlay_input.fill(MSG)
        overlay_input.press("Enter")

        # Main window should get the user bubble via the WS broadcast.
        try:
            page_main.get_by_text(MSG, exact=False).first.wait_for(
                state="visible", timeout=10000
            )
            print("✓ main window received user turn from overlay-typed message")
        except Exception as e:
            fails += 1
            print(f"✗ main window did NOT show overlay-typed user message: {e}")

        # Wait for assistant
        try:
            page_main.locator("text=/^asuna ·/i").first.wait_for(
                state="visible", timeout=120000
            )
            print("✓ main window also received assistant reply")
        except Exception as e:
            fails += 1
            print(f"✗ main window did NOT show assistant reply: {e}")

        # Check storage for both turns
        main_counts = history_role_counts(page_main)
        overlay_counts = history_role_counts(page_overlay)
        print(f"main storage role counts: {main_counts}")
        print(f"overlay storage role counts (should be empty if overlay never writes): {overlay_counts}")

        if main_counts.get("user", 0) >= 1 and main_counts.get("assistant", 0) >= 1:
            print("✓ main has user+assistant turns")
        else:
            fails += 1
            print("✗ main missing turns")

        browser.close()

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failed assertion(s)")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
