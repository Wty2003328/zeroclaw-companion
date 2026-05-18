"""Navigate to /settings, snap, and exercise the avatar override save.

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

def call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            return msg

def shoot(ws, mid, name):
    res = call(ws, mid, "Page.captureScreenshot", {"format": "png"})
    data = res.get("result", {}).get("data")
    if data:
        out = os.path.join(SCRIPT_DIR, f"_tauri_{name}.png")
        open(out, "wb").write(base64.b64decode(data))
        print("wrote", out)

def ev(ws, mid, expr):
    r = call(ws, mid, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("result", {}).get("value")

def main():
    target = find_main()
    if not target: sys.exit(2)
    ws = create_connection(target["webSocketDebuggerUrl"], timeout=10)
    call(ws, 1, "Runtime.evaluate", {"expression": "location.href = '/settings'"})
    time.sleep(3)
    shoot(ws, 2, "settings_v2")
    # Verify Live2D model picker is GONE.
    has_picker = ev(ws, 3, """
      Array.from(document.querySelectorAll('h2')).some(h => h.textContent.includes('Live2D model'))
    """)
    print('still has Live2D picker:', has_picker)
    # Check there's an "Avatar" + "Avatar subagent" section.
    sections = ev(ws, 4, "Array.from(document.querySelectorAll('h2')).map(h => h.textContent.trim())")
    print('sections:', sections)
    # Confirm there's a TTS speed slider.
    has_speed = ev(ws, 5, "document.querySelectorAll('input[type=\"range\"]').length")
    print('range inputs (speed sliders):', has_speed)
    # Save tts_speed = 1.5 directly via fetch to confirm endpoint works.
    save_result = ev(ws, 6, """
      fetch('/api/config/avatar', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tts_speed: 1.5})
      }).then(r => r.status + ' ' + r.statusText)
    """)
    print('avatar save status:', save_result)
    # Verify runtime.json has it.
    ws.close()

if __name__ == "__main__":
    main()
