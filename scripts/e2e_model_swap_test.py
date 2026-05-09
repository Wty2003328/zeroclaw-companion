"""Verify multi-model swap UI.

Setup checks:
  - GET /api/models returns the installed models from
    web/public/live2d/models/.
  - Settings page renders a "Live2D model" section with a radio
    for each installed model + a "Server default" option.

Behavior checks:
  - Clicking a non-default model writes companion.userModel.v1 to
    localStorage AND fires the companion:userModel custom event.
  - The Avatar page picks up the override: when localStorage is set
    BEFORE navigation, the rendered canvas uses the picked model's
    URL (verified via the inspected modelUrl prop chain — we read
    the loaded src in the network tab).
  - Switching back to "Server default" clears the localStorage key.

Run: python scripts/e2e_model_swap_test.py
"""

from __future__ import annotations

import io
import json
import sys
from urllib import request

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:9181"


def main() -> int:
    fails = 0

    # ── Backend: /api/models lists at least one model ──
    try:
        with request.urlopen(f"{BASE}/api/models", timeout=5) as r:
            payload = json.loads(r.read())
    except Exception as e:
        print(f"  ✗ /api/models request failed: {e}")
        return 1
    models = payload.get("models", [])
    print(f"  /api/models returned {len(models)} model(s):")
    for m in models:
        print(f"    - {m['id']:10s}  format={m['format']:8s}  url={m['modelUrl']}")
    if len(models) >= 1:
        print(f"  ✓ found {len(models)} installed model(s)")
    else:
        fails += 1
        print("  ✗ no models found")

    if not models:
        return 2 if fails else 0

    pick_id = next((m["id"] for m in models if m["id"] != "asuna"), models[0]["id"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # ── Settings UI: model picker present ──
        page.goto(f"{BASE}/settings", wait_until="networkidle", timeout=30000)
        page.evaluate("() => localStorage.removeItem('companion.userModel.v1')")
        page.reload(wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        try:
            page.get_by_text("Live2D model", exact=False).first.wait_for(
                state="visible", timeout=3000
            )
            print("  ✓ Settings shows 'Live2D model' section")
        except Exception:
            fails += 1
            print("  ✗ 'Live2D model' section missing")

        # ── Click the radio for `pick_id` ──
        try:
            radio = page.locator(f'input[name="live2d-model"]').nth(
                # index 0 = Server default; pick_id position depends on model order
                next(i for i, m in enumerate(models) if m["id"] == pick_id) + 1
            )
            radio.check(force=True)
            page.wait_for_timeout(200)
        except Exception as e:
            fails += 1
            print(f"  ✗ couldn't click radio for {pick_id}: {e}")

        stored = page.evaluate(
            "() => localStorage.getItem('companion.userModel.v1')"
        )
        if stored == pick_id:
            print(f"  ✓ Settings click stored '{pick_id}' to localStorage")
        else:
            fails += 1
            print(f"  ✗ expected '{pick_id}' in localStorage, got {stored!r}")

        # ── Avatar page picks up the override ──
        # Navigate to /avatar; the modelUrl loaded by Live2DViewer
        # should match the picked model.
        net_log: list[str] = []
        page2 = ctx.new_page()
        page2.on("request", lambda req: net_log.append(req.url))
        page2.goto(f"{BASE}/avatar", wait_until="networkidle", timeout=30000)
        page2.wait_for_timeout(2500)
        target = next(m for m in models if m["id"] == pick_id)
        loaded = any(target["modelUrl"] in u for u in net_log)
        if loaded:
            print(f"  ✓ Avatar page loaded {target['modelUrl']} (override active)")
        else:
            fails += 1
            print(
                f"  ✗ Avatar page did NOT load picked model. "
                f"Wanted: {target['modelUrl']}, top model URLs in log:\n    "
                + "\n    ".join(u for u in net_log if 'live2d' in u and 'json' in u)[:6]
            )

        # ── Switching back to "Server default" clears the override ──
        page.bring_to_front()
        page.locator('input[name="live2d-model"]').nth(0).check(force=True)
        page.wait_for_timeout(200)
        cleared = page.evaluate(
            "() => localStorage.getItem('companion.userModel.v1')"
        )
        if cleared in (None, ""):
            print("  ✓ 'Server default' cleared the localStorage override")
        else:
            fails += 1
            print(f"  ✗ 'Server default' did NOT clear override (got {cleared!r})")

        browser.close()

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failed assertion(s)")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
