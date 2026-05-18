"""Navigate the main Tauri window to /pulse and screenshot each tab.

Screenshots are written next to this script as `_tauri_*.png`.
"""
import json, base64, os, sys, time, urllib.request
from websocket import create_connection  # type: ignore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def find_main():
    ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
    for t in ts:
        if t.get("type") == "page" and "overlay=" not in t.get("url", ""):
            return t
    return None

def call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            return msg

def shoot(ws, mid, name):
    res = call(ws, mid, "Page.captureScreenshot", {"format": "png"})
    data = res.get("result", {}).get("data")
    if not data:
        print("no shot for", name); return
    out = os.path.join(SCRIPT_DIR, f"_tauri_{name}.png")
    open(out, "wb").write(base64.b64decode(data))
    print("wrote", out)

def main():
    target = find_main()
    if not target: sys.exit(2)
    ws = create_connection(target["webSocketDebuggerUrl"], timeout=10)
    # Navigate to /pulse
    call(ws, 1, "Runtime.evaluate", {"expression":
        "window.history.pushState({}, '', '/pulse'); window.dispatchEvent(new PopStateEvent('popstate'));"})
    time.sleep(2)
    shoot(ws, 2, "pulse_feed")
    # Try sources tab
    call(ws, 3, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button,[role=tab]')).find(e=>e.textContent.trim()==='Sources')?.click(); 1"})
    time.sleep(1)
    shoot(ws, 4, "pulse_sources")
    call(ws, 5, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button,[role=tab]')).find(e=>e.textContent.trim()==='Settings')?.click(); 1"})
    time.sleep(1)
    shoot(ws, 6, "pulse_settings")
    # Back to feed and click first item
    call(ws, 7, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button,[role=tab]')).find(e=>e.textContent.trim()==='Feed')?.click(); 1"})
    time.sleep(1)
    call(ws, 8, "Runtime.evaluate", {"expression":
        "(document.querySelector('[data-testid=\"feed-item\"]')||document.querySelector('article,li[data-id],[data-item-id]'))?.click(); 1"})
    time.sleep(1)
    shoot(ws, 9, "pulse_drawer")
    ws.close()

if __name__ == "__main__":
    main()
