"""Snap a screenshot of the main Tauri window via CDP and dump basic page state.

Screenshots are written next to this script as `_tauri_*.png`.
"""
import json, base64, os, sys, urllib.request
from websocket import create_connection  # type: ignore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def find_main():
    ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
    for t in ts:
        if t.get("type") == "page" and "overlay=" not in t.get("url", ""):
            return t
    return ts[0] if ts else None

def call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            return msg

def main():
    target = find_main()
    if not target:
        print("no target", file=sys.stderr); sys.exit(2)
    print("target:", target["url"])
    ws = create_connection(target["webSocketDebuggerUrl"], timeout=10)
    print("hash:", call(ws, 1, "Runtime.evaluate", {"expression": "location.hash + ' | ' + location.pathname"}))
    print("title:", call(ws, 2, "Runtime.evaluate", {"expression": "document.title"}))
    print("h1:", call(ws, 3, "Runtime.evaluate",
        {"expression": "Array.from(document.querySelectorAll('h1,h2,nav a')).slice(0,15).map(e=>e.textContent.trim()).join(' | ')"}))
    shot = call(ws, 4, "Page.captureScreenshot", {"format": "png"})
    data = shot.get("result", {}).get("data")
    if data:
        out = os.path.join(SCRIPT_DIR, "_tauri_main.png")
        open(out, "wb").write(base64.b64decode(data))
        print("screenshot:", out)
    ws.close()

if __name__ == "__main__":
    main()
