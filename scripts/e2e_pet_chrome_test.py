"""Verify the desktop-pet overlay is chromeless by default and
reveals controls only on hover.

Pet mode requirements (per user feedback):
  - "transparent background, and not a window"
  - "we still want a message input box"

Translation:
  - The overlay window's background is transparent.
  - No visible chrome (corner buttons, chat bar) until hover.
  - On hover, the chat bar fades in.

We can't test "transparent window" from inside a headless Chromium
context — that's a Tauri WebView-2 behavior. But we CAN test the
React-side opacity gating: corner buttons + chat bar render with
opacity=0 by default and opacity=1 after a hover.

Run: python scripts/e2e_pet_chrome_test.py
"""

from __future__ import annotations

import io
import sys

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

OVERLAY = "http://127.0.0.1:9181/avatar?overlay=1"


def opacity(page, locator) -> float:
    """Returns the COMPUTED opacity of the element's nearest ancestor
    that has an inline opacity style. Buttons inside a fade-wrapper
    inherit visual transparency from the wrapper, but their own
    `getComputedStyle(...).opacity` is unaffected — checking the
    wrapper is what tells us "is this visually faded out."
    """
    return float(page.evaluate(
        """(el) => {
            let cur = el;
            while (cur && cur.style && !cur.style.opacity) cur = cur.parentElement;
            return parseFloat(getComputedStyle(cur ?? el).opacity);
        }""",
        locator.element_handle(),
    ))


def fire(page, event: str) -> None:
    """Trigger React's onMouseEnter / onMouseLeave on the canvas div.
    React 17+ polyfills mouseEnter/Leave via mouseover/mouseout on
    the React root, so we dispatch THOSE (they bubble). React then
    fires its synthetic mouseenter/mouseleave at the right target."""
    native = "mouseover" if event == "mouseenter" else "mouseout"
    page.evaluate(
        """(nativeEv) => {
            for (const el of document.querySelectorAll('div')) {
                if (el.querySelector('canvas') &&
                    el.querySelector('button[title=\"Canvas settings\"]')) {
                    el.dispatchEvent(new MouseEvent(nativeEv, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        relatedTarget: nativeEv === 'mouseout' ? document.body : null,
                    }));
                    return;
                }
            }
        }""",
        native,
    )


def main() -> int:
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 400, "height": 540})
        page = ctx.new_page()
        page.goto(OVERLAY, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # Force-leave so the initial state is "no hover" regardless of
        # where Playwright happened to put the cursor on load.
        canvas_wrap_initial = page.locator(
            '[data-tauri-drag-region=""]'
        ).first
        canvas_wrap_initial.dispatch_event("mouseleave")
        page.wait_for_timeout(400)

        gear = page.locator('button[title="Canvas settings"]').first
        chat_bar = page.locator('form').first

        gear_alpha_before = opacity(page, gear)
        chat_alpha_before = opacity(page, chat_bar)
        print(f"  pre-hover: gear opacity={gear_alpha_before}, chat opacity={chat_alpha_before}")
        if gear_alpha_before < 0.05:
            print("  ✓ corner buttons hidden by default")
        else:
            fails += 1
            print(f"  ✗ corner buttons NOT hidden — opacity {gear_alpha_before}")
        if chat_alpha_before < 0.05:
            print("  ✓ chat bar hidden by default")
        else:
            fails += 1
            print(f"  ✗ chat bar NOT hidden — opacity {chat_alpha_before}")

        # Trigger mouseenter on the canvas wrapper. Playwright's
        # dispatch_event API generates a properly-typed event that
        # React's synthetic system picks up; raw mouseover/mouseout
        # didn't reach React's listener in this layout.
        canvas_wrap = page.locator(
            '[data-tauri-drag-region=""]'
        ).first
        # React 17+ uses delegated event listeners on the root and
        # synthesizes mouseenter/leave from mouseover/mouseout (which
        # bubble). dispatch_event('mouseenter') doesn't always reach
        # React's listener; mouseover does because it bubbles up to
        # the React root where the listener lives.
        canvas_wrap.dispatch_event("mouseover")
        page.wait_for_timeout(500)

        gear_alpha_after = opacity(page, gear)
        chat_alpha_after = opacity(page, chat_bar)
        print(f"  hover: gear opacity={gear_alpha_after}, chat opacity={chat_alpha_after}")
        if gear_alpha_after > 0.95:
            print("  ✓ corner buttons revealed on hover")
        else:
            fails += 1
            print(f"  ✗ corner buttons did NOT reveal — opacity {gear_alpha_after}")
        if chat_alpha_after > 0.95:
            print("  ✓ chat bar revealed on hover")
        else:
            fails += 1
            print(f"  ✗ chat bar did NOT reveal — opacity {chat_alpha_after}")

        # mouseleave — same trick: dispatch mouseout which React
        # translates into onMouseLeave for the matching target.
        canvas_wrap.dispatch_event("mouseout")
        page.wait_for_timeout(500)
        gear_alpha_leave = opacity(page, gear)
        chat_alpha_leave = opacity(page, chat_bar)
        print(f"  leave: gear opacity={gear_alpha_leave}, chat opacity={chat_alpha_leave}")
        if gear_alpha_leave < 0.05 and chat_alpha_leave < 0.05:
            print("  ✓ chrome hides again after mouse leaves")
        else:
            fails += 1
            print("  ✗ chrome did NOT hide after mouse leaves")

        # Verify the overlay does NOT render the main "Chat history" panel.
        try:
            page.get_by_text("Chat history", exact=False).first.wait_for(
                state="visible", timeout=1500
            )
            fails += 1
            print("  ✗ overlay renders the main chat panel (should be hidden)")
        except Exception:
            print("  ✓ overlay correctly suppresses the main chat panel")

        browser.close()

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failed assertion(s)")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
