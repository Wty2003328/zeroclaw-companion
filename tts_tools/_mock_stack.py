"""Mock stack — fake zeroclaw + TTS + NMT + speech servers with a unified
control plane that lets a test rig flip them into failure modes.

Architecture:
    Each mock is a FastAPI app served on its own port by uvicorn. They
    share a single in-memory `_State` dict and expose its knobs through a
    control-plane endpoint on PORT_CONTROL (9883). Tests POST
    `/_set {tts_dead:true, nmt_slow:1.5}` to inject failure scenarios;
    `/_clear` resets everything.

Spawn:
    The mocks run as ONE Python process (this file as `__main__`) so all
    four FastAPI apps + the control plane share state in-process. The
    process is started by `start_mock_stack()` and stopped on context exit.

Endpoints implemented:
    Mock zeroclaw (PORT_MOCK_ZEROCLAW)
        POST /api/chat      → {reply: str, character_id: str}
        POST /api/events    → SSE stream of fake events
        GET  /api/healthz   → {status:"ok"}

    Mock TTS (PORT_TTS)
        POST /v1/audio/speech → audio bytes (1 KiB of silence WAV, X-Sample-Rate:24000)
        POST /tts             → same (legacy alias)
        GET  /healthz         → {status:"ok"}
        POST /shutdown        → 200, then exit gracefully

    Mock NMT (PORT_NMT)
        POST /translate       → {translated: str}
        GET  /healthz
        POST /shutdown

    Control plane (PORT_CONTROL)
        POST /_set            → merge knobs into state
        POST /_clear          → reset to defaults
        GET  /_state          → dump current state

Run directly to start the stack manually:
    python -m tts_tools._mock_stack
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import struct
import sys
import threading
import time
import wave
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import StreamingResponse
    import uvicorn
except ImportError:
    print("[mock-stack] missing deps: pip install fastapi uvicorn", file=sys.stderr)
    raise

# Port constants — must match _test_helpers.py.
PORT_TTS = 9880
PORT_NMT = 9881
PORT_CONTROL = 9883
PORT_MOCK_ZEROCLAW = 42617


# ---------------------------------------------------------------------------- #
# Shared state — control-plane knobs
# ---------------------------------------------------------------------------- #
class _State:
    """Mutable container for failure-mode knobs. Defaults = healthy."""
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        # TTS knobs
        self.tts_dead = False         # True → bind dropped, simulating crash
        self.tts_status = 200         # forced HTTP status (200 = healthy)
        self.tts_slow_s = 0.0         # extra latency on every request
        # NMT knobs
        self.nmt_dead = False
        self.nmt_status = 200
        self.nmt_slow_s = 0.0
        # zeroclaw knobs
        self.zc_dead = False
        self.zc_status = 200
        self.zc_slow_s = 0.0
        # speech (ASR) knobs (forwarded by companion-server when wired)
        self.speech_dead = False
        self.speech_status = 200

    def patch(self, knobs: dict) -> None:
        for k, v in knobs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def snapshot(self) -> dict:
        return {k: v for k, v in vars(self).items() if not k.startswith("_")}


STATE = _State()


# ---------------------------------------------------------------------------- #
# Silence WAV builder — used by every TTS mock response
# ---------------------------------------------------------------------------- #
def _silence_wav(duration_s: float = 0.1, sample_rate: int = 24_000) -> bytes:
    buf = io.BytesIO()
    n_samples = int(duration_s * sample_rate)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit PCM
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


# ---------------------------------------------------------------------------- #
# Mock TTS app
# ---------------------------------------------------------------------------- #
tts_app = FastAPI(title="mock-tts")


@tts_app.get("/healthz")
async def tts_healthz():
    if STATE.tts_dead:
        raise HTTPException(503, "tts dead")
    return {"status": "ok", "engine": "mock", "voices_ready": True}


@tts_app.get("/health")
async def tts_health_legacy():
    return await tts_healthz()


@tts_app.post("/v1/audio/speech")
async def tts_speech(request: Request):
    if STATE.tts_dead:
        raise HTTPException(503, "tts dead")
    if STATE.tts_status != 200:
        raise HTTPException(STATE.tts_status, f"forced status {STATE.tts_status}")
    if STATE.tts_slow_s > 0:
        await asyncio.sleep(STATE.tts_slow_s)
    body = await request.json()
    text = body.get("input", "")
    # Roughly 50ms of audio per char so duration scales with input.
    duration = max(0.05, min(10.0, 0.05 * len(text)))
    audio = _silence_wav(duration_s=duration)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"X-Sample-Rate": "24000", "X-Channels": "1", "X-Format": "wav"},
    )


@tts_app.post("/tts")
async def tts_legacy(request: Request):
    return await tts_speech(request)


@tts_app.post("/shutdown")
async def tts_shutdown():
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting down"}


# ---------------------------------------------------------------------------- #
# Mock NMT app
# ---------------------------------------------------------------------------- #
nmt_app = FastAPI(title="mock-nmt")


@nmt_app.get("/healthz")
async def nmt_healthz():
    if STATE.nmt_dead:
        raise HTTPException(503, "nmt dead")
    return {"status": "ok"}


@nmt_app.get("/health")
async def nmt_health_legacy():
    return await nmt_healthz()


@nmt_app.post("/translate")
async def nmt_translate(request: Request):
    if STATE.nmt_dead:
        raise HTTPException(503, "nmt dead")
    if STATE.nmt_status != 200:
        raise HTTPException(STATE.nmt_status, f"forced status {STATE.nmt_status}")
    if STATE.nmt_slow_s > 0:
        await asyncio.sleep(STATE.nmt_slow_s)
    body = await request.json()
    text = body.get("text", "")
    src = body.get("source_lang", "")
    dst = body.get("target_lang", "")
    if not src or not dst:
        raise HTTPException(400, "missing source_lang or target_lang")
    # Trivial mock translation: prefix with target language tag.
    return {"translated": f"[{dst}] {text}", "source_lang": src, "target_lang": dst}


@nmt_app.post("/shutdown")
async def nmt_shutdown():
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting down"}


# ---------------------------------------------------------------------------- #
# Mock zeroclaw app
# ---------------------------------------------------------------------------- #
zc_app = FastAPI(title="mock-zeroclaw")


@zc_app.get("/api/healthz")
async def zc_healthz():
    if STATE.zc_dead:
        raise HTTPException(503, "zc dead")
    return {"status": "ok"}


@zc_app.post("/api/chat")
async def zc_chat(request: Request):
    if STATE.zc_dead:
        raise HTTPException(503, "zc dead")
    if STATE.zc_status != 200:
        raise HTTPException(STATE.zc_status, f"forced status {STATE.zc_status}")
    if STATE.zc_slow_s > 0:
        await asyncio.sleep(STATE.zc_slow_s)
    body = await request.json()
    msg = body.get("message", "")
    return {
        "reply": f"mock-reply to: {msg[:40]}",
        "character_id": body.get("character_id", "default"),
    }


@zc_app.get("/api/events")
async def zc_events():
    async def stream():
        for i in range(3):
            yield f"event: tick\ndata: {json.dumps({'i': i})}\n\n"
            await asyncio.sleep(0.1)
    return StreamingResponse(stream(), media_type="text/event-stream")


@zc_app.post("/shutdown")
async def zc_shutdown():
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting down"}


# ---------------------------------------------------------------------------- #
# Control plane
# ---------------------------------------------------------------------------- #
ctrl_app = FastAPI(title="mock-stack-control")


@ctrl_app.post("/_set")
async def ctrl_set(request: Request):
    knobs = await request.json()
    STATE.patch(knobs)
    return {"ok": True, "state": STATE.snapshot()}


@ctrl_app.post("/_clear")
async def ctrl_clear():
    STATE.reset()
    return {"ok": True, "state": STATE.snapshot()}


@ctrl_app.get("/_state")
async def ctrl_state():
    return STATE.snapshot()


@ctrl_app.post("/_shutdown")
async def ctrl_shutdown():
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting down"}


@ctrl_app.post("/shutdown")
async def ctrl_shutdown_alias():
    """Alias so `ManagedProc.stop` (which POSTs /shutdown universally)
    can cleanly stop the mock-stack process regardless of which port it
    labels with. The mock-stack runs all four apps in one process —
    hitting `/shutdown` on any of them triggers process-level exit.
    """
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting down"}


# ---------------------------------------------------------------------------- #
# Main — start all four servers in the same process
# ---------------------------------------------------------------------------- #
def _delayed_exit():
    # Give the HTTP response a moment to flush.
    time.sleep(0.2)
    os._exit(0)


def _serve(app: FastAPI, port: int) -> None:
    """Run uvicorn for one app on one port, in a thread. log_level=warning so
    the mock doesn't drown the rig's own output."""
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                         access_log=False)
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())


def main():
    threads = [
        threading.Thread(target=_serve, args=(tts_app, PORT_TTS), daemon=True),
        threading.Thread(target=_serve, args=(nmt_app, PORT_NMT), daemon=True),
        threading.Thread(target=_serve, args=(zc_app, PORT_MOCK_ZEROCLAW), daemon=True),
        threading.Thread(target=_serve, args=(ctrl_app, PORT_CONTROL), daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"[mock-stack] TTS:{PORT_TTS}  NMT:{PORT_NMT}  ZC:{PORT_MOCK_ZEROCLAW}  CTRL:{PORT_CONTROL}", flush=True)
    # Wait for any to die or signal.
    def _sig(*_a):
        os._exit(0)
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
