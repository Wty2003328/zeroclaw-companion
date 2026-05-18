"""Reload the main window and snap home + pulse drawer to verify Open button.

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
    # Reload to /
    call(ws, 1, "Runtime.evaluate", {"expression": "location.href = '/'"})
    time.sleep(3)
    shoot(ws, 2, "home_v2")
    # Click on "System status" to expand it
    call(ws, 3, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.includes('System status'))?.click(); 1"})
    time.sleep(0.5)
    call(ws, 4, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.includes('Server connection'))?.click(); 1"})
    time.sleep(0.5)
    shoot(ws, 5, "home_v2_expanded")
    # Try clicking Edit on first character to open modal
    call(ws, 6, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='Edit')?.click(); 1"})
    time.sleep(0.5)
    shoot(ws, 7, "home_v2_edit_modal")
    # Close modal — click the dimmed overlay
    call(ws, 8, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='Cancel')?.click(); 1"})
    time.sleep(0.5)
    # Navigate to pulse, open first item, screenshot drawer
    call(ws, 9, "Runtime.evaluate", {"expression":
        "window.history.pushState({}, '', '/pulse'); window.dispatchEvent(new PopStateEvent('popstate'));"})
    time.sleep(2)
    call(ws, 10, "Runtime.evaluate", {"expression":
        "(document.querySelector('article'))?.click(); 1"})
    time.sleep(0.8)
    shoot(ws, 11, "pulse_drawer_v2")
    # Now intercept the Tauri invoke — count how many times open_external_url is called when we click Open.
    call(ws, 12, "Runtime.evaluate", {"expression": """
        (function(){
          const w = window;
          const inv = w.__TAURI_INTERNALS__?.invoke;
          if (!inv) return 'NO TAURI INVOKE';
          w.__open_calls__ = [];
          w.__TAURI_INTERNALS__.invoke = function(cmd, args){
            if (cmd === 'open_external_url') w.__open_calls__.push(args);
            return inv.call(this, cmd, args);
          };
          return 'patched';
        })();
    """})
    # Click "Open ↗" button
    call(ws, 13, "Runtime.evaluate", {"expression":
        "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim().startsWith('Open'))?.click(); 1"})
    time.sleep(0.5)
    res = call(ws, 14, "Runtime.evaluate", {"expression":
        "JSON.stringify(window.__open_calls__ || [])"})
    print("open_external_url invocations:", res.get('result',{}).get('result',{}).get('value'))
    ws.close()

if __name__ == "__main__":
    main()
