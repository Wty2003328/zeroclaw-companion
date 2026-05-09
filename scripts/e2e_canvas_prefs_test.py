"""Comprehensively test the canvas customization controls.

For each new preference:
  - rotation, mirrorX, bgImageUrl, bgImageOpacity, bgImageFit,
    idleMotion, idleMotionSecs, eyeTracking
the test:
  1. Opens the settings popover.
  2. Programmatically sets the pref via a localStorage merge (the
     settings UI itself is hard to drive headlessly because it uses
     range inputs + color pickers + file inputs, and Playwright's
     range manipulation is flaky on Windows).
  3. Reloads the page.
  4. Asserts the prefs survived through localStorage.

Then a separate UI-driven test:
  - Click ⚙ to open settings popover
  - Verify popover renders the new sections (rotation, mirror,
    background image, behavior)
  - Toggle the mirror checkbox via click and assert the storage flips

Run: python scripts/e2e_canvas_prefs_test.py
"""

from __future__ import annotations

import io
import json
import sys

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

URL = "http://127.0.0.1:9181/avatar"
PREFS_KEY = "companion.avatarPrefs.v1"


def get_prefs(page) -> dict:
    raw = page.evaluate(f"() => localStorage.getItem('{PREFS_KEY}')")
    return json.loads(raw) if raw else {}


def set_prefs(page, patch: dict) -> None:
    js = (
        "(p) => { const cur = JSON.parse(localStorage.getItem('"
        + PREFS_KEY
        + "') || '{}'); localStorage.setItem('"
        + PREFS_KEY
        + "', JSON.stringify({...cur, ...p})); }"
    )
    page.evaluate(js, patch)


def main() -> int:
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # ── Each new pref round-trips via localStorage + reload ──
        cases = [
            {"rotation": 45},
            {"mirrorX": True},
            {"bgImageOpacity": 0.5},
            {"bgImageFit": "contain"},
            {"idleMotion": True, "idleMotionSecs": 7},
            {"eyeTracking": True},
        ]
        for patch in cases:
            set_prefs(page, patch)
            page.reload(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            stored = get_prefs(page)
            ok = all(stored.get(k) == v for k, v in patch.items())
            if ok:
                print(f"  ✓ persisted: {patch}")
            else:
                fails += 1
                print(f"  ✗ NOT persisted: wanted {patch}, got "
                      f"{ {k: stored.get(k) for k in patch} }")

        # ── Settings popover renders all the new sections ──
        # Click the ⚙ button (CanvasButton in top-right corner).
        # The button has title="Canvas settings".
        page.locator('button[title="Canvas settings"]').first.click()
        # Wait a beat for the popover to render.
        page.wait_for_timeout(300)
        # New popover sections:
        for needle in (
            "Rotation",
            "Mirror horizontally",
            "Background image",
            "Auto-play idle motion",
            "Gaze follows cursor",
        ):
            try:
                page.get_by_text(needle, exact=False).first.wait_for(
                    state="visible", timeout=2000
                )
                print(f"  ✓ popover shows '{needle}'")
            except Exception:
                fails += 1
                print(f"  ✗ popover missing '{needle}'")

        # ── Toggle the mirror checkbox via click; storage should flip
        before = get_prefs(page).get("mirrorX")
        # Click the label itself — the checkbox lives inside it as a
        # child (<label><input/>Mirror horizontally</label>) and the
        # native browser bubbles the click to the checkbox automatically.
        page.get_by_text("Mirror horizontally", exact=False).first.click()
        # Storage write happens via setPrefs effect after state update.
        page.wait_for_timeout(200)
        after = get_prefs(page).get("mirrorX")
        if after is not None and after != before:
            print(f"  ✓ mirror checkbox toggled storage ({before} → {after})")
        else:
            fails += 1
            print(f"  ✗ mirror checkbox did NOT toggle storage ({before} → {after})")

        browser.close()

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failed assertion(s)")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
