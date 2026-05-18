"""Test-side shims that bridge protocol gaps between the foundation mock
stack (`_mock_stack.py`) and what `companion-server` actually expects.

The foundation mock zeroclaw exposes `/api/chat` + `/api/healthz`, but
companion-server's ZeroclawClient (kind="zeroclaw") calls `/webhook` +
`/health`. This module spawns a tiny HTTP shim that:

  - Maps `GET /health`   → check mock /api/healthz (so companion's
    watchdog sees zc as up).
  - Maps `POST /webhook` → forward to mock /api/chat, rewrite `reply`
    field name to `response`.
  - Maps `GET /api/events` → reverse-proxy the mock's SSE stream.

The shim also surfaces a tiny faster-whisper / `/asr` mock so the
companion's voice-input proxy has somewhere to point. Both bind on
operator-chosen ports (we keep them in the 18xxx range so they don't
collide with the foundation's port grid). All endpoints are passive —
they only relay through to the real mock so the `mock_set` knobs flow
through transparently.

Used by:
  - test_backend_api_extended (needs working ASR + WS)
  - test_integration_full     (needs ASR for the voice-input loop)
  - test_chaos                (needs ASR for the speech_dead probe)
  - test_security             (uses shim to keep the chat path alive)
  - test_sse_bridge           (uses shim's /api/events passthrough)

Foundation files (`_test_helpers.py`, `_mock_stack.py`) are NOT touched
— this is a rig-side adapter that lives next to them.
"""
from __future__ import annotations

import base64
import io
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ports the foundation already owns — we only proxy through these
from tts_tools._test_helpers import PORT_CONTROL, PORT_MOCK_ZEROCLAW

# Rig-side ports. Anything outside the foundation grid is safe.
PORT_ZC_SHIM = 18790  # /health + /webhook + /api/events that companion talks to
PORT_SPEECH_SHIM = 9882  # /health + /asr — matches the speech sidecar default port


_STATE_CACHE: dict = {"t": 0.0, "v": {}}
_STATE_LOCK = threading.Lock()
# Short TTL: shim is on the SSE-bridge reconnect hot path where the
# control plane can choke if we hammer it; long TTL: rigs flip
# knobs aggressively and want the new state visible right away.
# 50 ms is a balance — chaos rigs sleep at least 0.3s between
# mock_set + the assertion, so 50ms staleness never confuses the test.
_STATE_TTL_S = 0.05


def _mock_state() -> dict:
    """Pull the current knob state from the foundation control plane,
    cached for `_STATE_TTL_S` seconds.

    Caching matters because the shim is hit on every SSE-bridge
    reconnect (and the bridge can reconnect 5-10× per second during
    failure-mode windows). Without it, the foundation mock-stack's
    single-asyncio-loop control plane saturates and `mock_set` /
    `mock_clear` from the rig start timing out at the 2s helper
    default. Foundation files are off-limits to fix — this is the
    rig-side compromise."""
    now = time.time()
    with _STATE_LOCK:
        if now - _STATE_CACHE["t"] < _STATE_TTL_S:
            return _STATE_CACHE["v"]
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT_CONTROL}/_state", timeout=0.5
        ) as r:
            v = json.loads(r.read().decode("utf-8"))
            with _STATE_LOCK:
                _STATE_CACHE["t"] = now
                _STATE_CACHE["v"] = v
            return v
    except Exception:
        # Still bump the cache stamp so we don't immediately retry —
        # otherwise a flapping control plane sees a request flood.
        with _STATE_LOCK:
            _STATE_CACHE["t"] = now
        return _STATE_CACHE.get("v") or {}


def safe_mock_set(**knobs) -> bool:
    """Wrapper around `mock_set` that retries with backoff so a
    momentarily-busy control plane (under heavy SSE-bridge reconnect
    churn) doesn't crash a chaos rig. The foundation `mock_set`'s 2s
    timeout is too short under stress; this layer absorbs that."""
    from tts_tools._test_helpers import http_post_json, PORT_CONTROL as _P
    url = f"http://127.0.0.1:{_P}/_set"
    last_status = 0
    for attempt in range(6):
        try:
            s, _, _ = http_post_json(url, knobs, timeout=2.0)
            last_status = s
            if s == 200:
                # Invalidate our cache so the next state read pulls fresh.
                with _STATE_LOCK:
                    _STATE_CACHE["t"] = 0.0
                return True
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    return False


def safe_mock_clear() -> bool:
    """Retrying wrapper around `mock_clear`."""
    from tts_tools._test_helpers import http_post_json, PORT_CONTROL as _P
    url = f"http://127.0.0.1:{_P}/_clear"
    for attempt in range(6):
        try:
            s, _, _ = http_post_json(url, {}, timeout=2.0)
            if s == 200:
                with _STATE_LOCK:
                    _STATE_CACHE["t"] = 0.0
                return True
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    return False


# ---------------------------------------------------------------------- #
# Retrying HTTP wrappers — absorb transient WinError 10054 (connection
# reset) and 10061 (connection refused) that show up under heavy
# concurrent load. The companion is actually responding; the TCP layer
# just refused/reset a single attempt. Bounded retry hides the noise
# without masking real outages (3 attempts × 0.4 s).
# ---------------------------------------------------------------------- #
def robust_http_get(url: str, timeout: float = 5.0, retries: int = 4) -> tuple[int, bytes, dict]:
    from tts_tools._test_helpers import http_get
    last_err: Exception | None = None
    for i in range(retries):
        try:
            return http_get(url, timeout=timeout)
        except Exception as e:
            last_err = e
            time.sleep(0.3 * (i + 1))
    if last_err:
        raise last_err
    return 0, b"", {}


def robust_http_post_json(url: str, payload, timeout: float = 10.0,
                          retries: int = 4) -> tuple[int, bytes, dict]:
    from tts_tools._test_helpers import http_post_json
    last_err: Exception | None = None
    for i in range(retries):
        try:
            return http_post_json(url, payload, timeout=timeout)
        except Exception as e:
            last_err = e
            time.sleep(0.3 * (i + 1))
    if last_err:
        raise last_err
    return 0, b"", {}


def robust_http_json(url: str, timeout: float = 5.0, retries: int = 4) -> dict | None:
    import json as _j
    s, body, _ = robust_http_get(url, timeout=timeout, retries=retries)
    if s != 200:
        return None
    try:
        return _j.loads(body.decode("utf-8"))
    except Exception:
        return None


def require_ports_free_with_wait(*ports: int, wait_s: float = 12.0) -> None:
    """Foundation `require_ports_free` is strict — raises SystemExit
    immediately if any port is bound. That makes sequential runs of
    different rigs flaky when the previous companion's listening
    socket hasn't released yet (TIME_WAIT, slow OS cleanup, etc.).

    This wrapper waits up to `wait_s` for the ports to release,
    re-checking every 0.5s. Same fail behavior at the end (SystemExit
    with the bound-ports list) so the rig fails loudly if anything
    actually held a port indefinitely."""
    import socket as _s
    deadline = time.time() + wait_s
    while time.time() < deadline:
        bound = []
        for p in ports:
            with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
                sk.settimeout(0.2)
                if sk.connect_ex(("127.0.0.1", p)) == 0:
                    bound.append(p)
        if not bound:
            return
        time.sleep(0.5)
    # One last check before declaring failure.
    final_bound = []
    for p in ports:
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
            sk.settimeout(0.2)
            if sk.connect_ex(("127.0.0.1", p)) == 0:
                final_bound.append(p)
    if final_bound:
        # Non-zero exit so the rig is treated as failed by run_all /
        # CI orchestration. The string is the same as the foundation
        # `require_ports_free` message format for parity.
        print(
            f"FAIL: port(s) bound after {wait_s:.0f}s wait — "
            f"stop them and re-run: {final_bound}",
            flush=True,
        )
        raise SystemExit(1)


def _silence_wav(duration_s: float = 0.1, sample_rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    n_samples = int(duration_s * sample_rate)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


# ---------------------------------------------------------------------- #
# Zeroclaw shim — bridges `/health`, `/webhook`, `/api/events`.
# ---------------------------------------------------------------------- #
class _ZcShimHandler(BaseHTTPRequestHandler):
    # Quiet log_message — drown the rig output otherwise.
    def log_message(self, *_args, **_kwargs):  # noqa: D401, N802
        return

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n > 0 else b""

    def do_GET(self):  # noqa: N802
        state = _mock_state()
        if self.path == "/health":
            if state.get("zc_dead"):
                self.send_response(503)
                self.end_headers()
                return
            self._send_json(200, {"status": "ok"})
            return
        if self.path.startswith("/api/events"):
            # SSE reverse-proxy. Open upstream connection and stream
            # chunks straight through.
            if state.get("zc_dead"):
                self.send_response(503)
                self.end_headers()
                return
            try:
                upstream = urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT_MOCK_ZEROCLAW}/api/events",
                    timeout=5.0,
                )
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
            return
        # Unknown GET — 404.
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        state = _mock_state()
        if self.path == "/webhook":
            if state.get("zc_dead"):
                self.send_response(503)
                self.end_headers()
                return
            zcs = state.get("zc_status", 200)
            if zcs != 200:
                self.send_response(zcs)
                self.end_headers()
                return
            slow = state.get("zc_slow_s", 0.0)
            if slow:
                time.sleep(slow)
            body = self._read_body()
            try:
                msg = json.loads(body or b"{}").get("message", "")
            except Exception:
                msg = ""
            # Forward to the foundation mock /api/chat and rename
            # `reply` → `response` to match the /webhook contract.
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{PORT_MOCK_ZEROCLAW}/api/chat",
                    data=json.dumps({"message": msg}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as r:
                    upstream = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.end_headers()
                return
            except Exception:
                self.send_response(502)
                self.end_headers()
                return
            self._send_json(
                200,
                {
                    "response": upstream.get("reply", ""),
                    "model": "mock",
                },
            )
            return
        self.send_response(404)
        self.end_headers()


# ---------------------------------------------------------------------- #
# Speech shim — `/health` + `/asr`. Faster-whisper-compatible JSON shape.
# ---------------------------------------------------------------------- #
class _SpeechShimHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):  # noqa: D401, N802
        return

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n > 0 else b""

    def do_GET(self):  # noqa: N802
        state = _mock_state()
        if self.path == "/health" or self.path == "/healthz":
            if state.get("speech_dead"):
                self.send_response(503)
                self.end_headers()
                return
            self._send_json(200, {"status": "ok"})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        state = _mock_state()
        if self.path == "/asr":
            if state.get("speech_dead"):
                self.send_response(503)
                self.end_headers()
                return
            ss = state.get("speech_status", 200)
            if ss != 200:
                self.send_response(ss)
                self.end_headers()
                return
            # Parse audio length so the duration field is somewhat real.
            try:
                req = json.loads(self._read_body() or b"{}")
            except Exception:
                req = {}
            audio_b64 = req.get("audio", "")
            try:
                raw = base64.b64decode(audio_b64 or "")
            except Exception:
                raw = b""
            dur = max(0.1, len(raw) / 32000.0)  # 16k * 2 bytes
            self._send_json(
                200,
                {
                    "text": "mock transcript",
                    "language": req.get("language") or "en",
                    "duration": dur,
                    "wall_ms": 50.0,
                    "segments": [],
                },
            )
            return
        if self.path == "/shutdown":
            self._send_json(200, {"status": "shutting down"})
            return
        self.send_response(404)
        self.end_headers()


# ---------------------------------------------------------------------- #
# Thin threaded HTTP servers
# ---------------------------------------------------------------------- #
class _ThreadingHttpServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # http.server prints tracebacks for benign client-side resets
        # (the companion's SSE reqwest aggressively closes its end on
        # reconnect, which fires ConnectionResetError on our recv).
        # Swallow those silently so test output stays readable.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start_shims(zc_port: int = PORT_ZC_SHIM, speech_port: int = PORT_SPEECH_SHIM) -> tuple:
    """Start both shims in background threads. Returns (zc_server,
    speech_server). Call .shutdown() on each to stop."""
    zc = _ThreadingHttpServer(("127.0.0.1", zc_port), _ZcShimHandler)
    sp = _ThreadingHttpServer(("127.0.0.1", speech_port), _SpeechShimHandler)
    t1 = threading.Thread(target=zc.serve_forever, daemon=True)
    t2 = threading.Thread(target=sp.serve_forever, daemon=True)
    t1.start()
    t2.start()
    return zc, sp


def wait_for_shims(zc_port: int = PORT_ZC_SHIM, speech_port: int = PORT_SPEECH_SHIM,
                   timeout_s: float = 5.0) -> bool:
    """Wait until both shims accept connections."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok_zc = ok_sp = False
        try:
            with socket.create_connection(("127.0.0.1", zc_port), timeout=0.5):
                ok_zc = True
        except Exception:
            pass
        try:
            with socket.create_connection(("127.0.0.1", speech_port), timeout=0.5):
                ok_sp = True
        except Exception:
            pass
        if ok_zc and ok_sp:
            return True
        time.sleep(0.1)
    return False
