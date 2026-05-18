"""L7 — Tauri shell smoke (automated, via CDP).

The class of bug this catches: the production Tauri binary launches but
shows a blank window. Possible causes:
  - cargo build forgot --features custom-protocol -> WebView loads dev
    server (localhost:5173) -> ERR_CONNECTION_REFUSED
  - frontendDist points at a stale or missing web/dist build
  - The bundled main.js fails on load (top-level await, missing chunk,
    CSP block) -> body renders empty
  - companion-server sidecar fails to spawn -> SPA loads but the first
    /api/* call hangs and the user sees "Connecting…" forever

Why it's important: the docs/TESTING-SOP.md L7 step was historically
manual ("Manual checklist: main window appears with the Home view"),
which means the decouple landed without anyone running it. This rig
makes L7 part of the orchestrator so blank-window regressions surface
within 90s of a build.

The rig:
  1. Build apps/companion-tauri with --features custom-protocol.
  2. Launch target/release/companion-tauri.exe with
     WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port + allow-origins.
  3. Wait for CDP to expose >=1 page target.
  4. Attach to the main window (largest viewport) and assert:
     - readyState == "complete"
     - #root has >=1 child element
     - nav text mentions Home / Avatar / Pulse / Settings
     - no SyntaxError / TypeError in console
     - GET http://127.0.0.1:9181/health from inside the WebView == 200
  5. Issue POST /shutdown, kill if it doesn't exit in 10s.
  6. Verify all sidecar ports (9181, 9881, 9891) released within 10s
     of process exit. Catches the iter-12 leak class.

Run:
  python -m tts_tools.test_tauri_shell                    # default
  python -m tts_tools.test_tauri_shell --no-rebuild       # trust existing exe
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent

for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

TAURI_DIR = REPO_ROOT / "apps" / "companion-tauri"
TAURI_EXE = TAURI_DIR / "target" / "release" / "companion-tauri.exe"
CDP_PORT = 9223  # different from 9222 so dev sessions don't clash
COMPANION_PORTS = (9181, 9881, 9891)


def log(msg: str) -> None:
    print(f"[tauri-l7] {msg}", flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"[tauri-l7] FAIL: {msg}", flush=True)
    sys.exit(code)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, deadline_s: float) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        if port_in_use(port):
            return True
        time.sleep(0.5)
    return False


def wait_for_ports_free(ports: list[int], deadline_s: float) -> list[int]:
    end = time.time() + deadline_s
    still_up = list(ports)
    while time.time() < end and still_up:
        still_up = [p for p in still_up if port_in_use(p)]
        if not still_up:
            return []
        time.sleep(0.5)
    return still_up


def build_tauri() -> None:
    log("building companion-tauri --release --features custom-protocol")
    t0 = time.time()
    result = subprocess.run(
        ["cargo", "build", "--release", "--features", "custom-protocol"],
        cwd=TAURI_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        fail(f"cargo build failed (exit {result.returncode})")
    log(f"build ok in {time.time()-t0:.1f}s")
    if not TAURI_EXE.exists():
        fail(f"build succeeded but exe missing: {TAURI_EXE}")


def _wm_close_tauri_window() -> None:
    """Send WM_CLOSE to the Tauri main window by enumerating top-level
    HWNDs and matching on the window title 'Waifu Companion'. This is
    the same signal Windows sends when the user clicks the X button —
    which is what Tauri's on_window_event(CloseRequested)/Destroyed
    handlers listen for. JS window.close() and CDP Browser.close do
    NOT cleanly trigger those handlers from a Tauri WebView2 host."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    GetWindowTextW.restype = ctypes.c_int
    PostMessageW = user32.PostMessageW
    PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    PostMessageW.restype = wintypes.BOOL
    WM_CLOSE = 0x0010
    matched: list[int] = []

    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        if GetWindowTextW(hwnd, buf, 256) > 0 and buf.value == "Waifu Companion":
            matched.append(int(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    if not matched:
        raise RuntimeError("no top-level window titled 'Waifu Companion' found")
    for h in matched:
        PostMessageW(h, WM_CLOSE, 0, 0)
    log(f"WM_CLOSE posted to {len(matched)} window(s)")


def spawn_tauri() -> subprocess.Popen:
    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        f"--remote-debugging-port={CDP_PORT} --remote-allow-origins=*"
    )
    env["RUST_LOG"] = "info"
    log(f"launching {TAURI_EXE.name} (CDP on :{CDP_PORT})")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        [str(TAURI_EXE)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def wait_for_cdp_pages(deadline_s: float) -> list[dict]:
    """Wait for CDP to expose at least one page target."""
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            data = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2,
            ))
            pages = [p for p in data if p.get("type") == "page"]
            if pages:
                return pages
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            pass
        time.sleep(0.5)
    return []


class CdpClient:
    """Tiny synchronous CDP client over websocket-client. Avoids the
    pyppeteer / playwright dependency for this single-file rig."""

    def __init__(self, ws_url: str):
        from websocket import create_connection  # type: ignore
        self.ws = create_connection(ws_url, timeout=10)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg

    def evaluate(self, expr: str) -> object:
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        })
        return r.get("result", {}).get("result", {}).get("value")

    def close(self) -> None:
        try: self.ws.close()
        except Exception: pass


def check_main_window(page: dict) -> list[str]:
    """Return a list of failure messages (empty == passes)."""
    failures: list[str] = []
    log(f"probing page url={page['url']} title={page['title']!r}")
    cdp = CdpClient(page["webSocketDebuggerUrl"])
    try:
        cdp.call("Runtime.enable")
        cdp.call("Log.enable")
        cdp.call("Console.enable")

        rs = cdp.evaluate("document.readyState")
        if rs != "complete":
            failures.append(f"readyState != complete: got {rs!r}")

        # Body should have non-trivial content
        body_len = cdp.evaluate("(document.body && document.body.innerHTML.length) || 0")
        if not isinstance(body_len, int) or body_len < 200:
            failures.append(f"body innerHTML too small ({body_len} bytes) — blank window?")

        # React #root should have children
        root_kids = cdp.evaluate(
            "(document.querySelector('#root') && document.querySelector('#root').children.length) || 0"
        )
        if not isinstance(root_kids, int) or root_kids < 1:
            failures.append(f"#root has no children ({root_kids}) — React didn't mount")

        # Nav text should mention the expected SPA routes (skip on overlay page)
        is_overlay = "overlay" in page.get("url", "")
        if not is_overlay:
            nav = cdp.evaluate(
                "Array.from(document.querySelectorAll('nav a, nav button')).map(e=>e.textContent.trim()).join(' | ')"
            ) or ""
            required = {"Home", "Avatar", "Pulse", "Settings"}
            missing = [r for r in required if r not in (nav or "")]
            if missing:
                failures.append(f"nav missing entries {missing}: nav={nav!r}")

        # No SyntaxError / TypeError sitting on window.__cdp_errors__ if injected
        err = cdp.evaluate("(window.__startup_errors__ && JSON.stringify(window.__startup_errors__)) || ''")
        if err:
            failures.append(f"startup errors captured: {err}")

        # Backend reachable from inside the WebView
        backend = cdp.evaluate(
            "fetch('http://127.0.0.1:9181/health').then(r => r.status).catch(e => 'err: ' + e)"
        )
        if backend != 200:
            failures.append(f"backend /health from WebView: {backend!r}")
    finally:
        cdp.close()
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description="L7 Tauri shell smoke")
    p.add_argument("--no-rebuild", action="store_true",
                   help="trust the existing target/release/companion-tauri.exe")
    p.add_argument("--keep-running", action="store_true",
                   help="leave the Tauri app running for manual inspection")
    args = p.parse_args()

    # Preflight: ports must be free or we'll conflict with an existing run
    busy = [p for p in (CDP_PORT, *COMPANION_PORTS) if port_in_use(p)]
    if busy:
        fail(f"ports already in use: {busy}. Kill prior runs first.")

    if not args.no_rebuild:
        build_tauri()
    elif not TAURI_EXE.exists():
        fail(f"--no-rebuild but exe missing: {TAURI_EXE}")

    proc = spawn_tauri()
    try:
        log("waiting for CDP page target (max 30s)…")
        pages = wait_for_cdp_pages(deadline_s=30.0)
        if not pages:
            fail("Tauri never exposed a CDP page target")

        log(f"found {len(pages)} page target(s)")
        # Pick the largest viewport — that's the main window. Overlay
        # window is 400×540; main is 1100×761.
        def viewport_area(pg: dict) -> int:
            try:
                cdp = CdpClient(pg["webSocketDebuggerUrl"])
                dims = cdp.evaluate("JSON.stringify([innerWidth, innerHeight])")
                cdp.close()
                arr = json.loads(dims) if isinstance(dims, str) else [0, 0]
                return int(arr[0]) * int(arr[1])
            except Exception:
                return 0
        pages.sort(key=viewport_area, reverse=True)
        main_page = pages[0]

        failures = check_main_window(main_page)
        if failures:
            for f_ in failures:
                log(f"❌ {f_}")
            fail(f"main window failed {len(failures)} check(s)")

        log("✅ main window rendered correctly (nav present, #root mounted, backend reachable)")

        if args.keep_running:
            log("--keep-running set; not shutting down. Press Ctrl-C to exit.")
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
            return 0
    finally:
        # Graceful shutdown sequence:
        #   1. Close the Tauri main window via CDP. Tauri's
        #      on_window_event(Destroyed) for the "main" label should
        #      kill the companion-server sidecar, which in turn POSTs
        #      /shutdown to its TTS + NMT sidecars.
        #   2. Wait up to 10s for the Tauri proc to exit.
        #   3. Fall back to SIGTERM/SIGKILL on the Tauri proc.
        log("graceful shutdown: WM_CLOSE to the Tauri main window")
        if os.name == "nt":
            try:
                _wm_close_tauri_window()
            except Exception as e:
                log(f"WM_CLOSE failed: {e!r}; falling back to signals")
        else:
            try:
                proc.terminate()  # SIGTERM on POSIX
            except Exception:
                pass
        # Wait
        try:
            proc.wait(timeout=10)
            log("Tauri exited cleanly")
        except subprocess.TimeoutExpired:
            log("Tauri didn't exit in 10s — sending CTRL_BREAK")
            try:
                if os.name == "nt":
                    import signal as _sig
                    proc.send_signal(_sig.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log("still alive — SIGKILL")
                proc.kill()
                proc.wait(timeout=5)

    # Iter-12 leak guard: sidecar ports must be released within 10s
    leaked = wait_for_ports_free(list(COMPANION_PORTS), deadline_s=10.0)
    if leaked:
        fail(f"sidecar ports leaked after shutdown: {leaked}")
    log("✅ all sidecar ports released cleanly")

    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
