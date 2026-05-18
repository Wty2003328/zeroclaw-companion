"""In-rig adapter — adds the `/webhook` shape that companion-server speaks
on top of the mock-stack's zeroclaw (which only ships `/api/chat`).

The frontend rigs need chat to actually round-trip end-to-end:
  companion-server.POST /api/chat
    → zeroclaw_client.send_chat_webhook
    → POST {zeroclaw.url}/webhook  ← mock-stack doesn't implement this

This shim binds the *same* PORT_MOCK_ZEROCLAW (42617) used by the
mock-stack — but the mock-stack thread for zc_app is started by
`_mock_stack.py`. We can't bind the same port twice. So instead, we
front the mock with a DIFFERENT port (PORT_MOCK_ZEROCLAW + 1000 by
default = 43617) and the rig points companion's `[zeroclaw] url` there.
`/webhook` is implemented here; `/api/events` and `/api/healthz` are
proxied to the mock so they keep working too.

Run:
  python -m tts_tools._zc_webhook_adapter
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

try:
    from fastapi import FastAPI, Request, Response, HTTPException
    from fastapi.responses import StreamingResponse, JSONResponse
    import uvicorn
except ImportError:
    print("[zc-adapter] missing deps: pip install fastapi uvicorn", file=sys.stderr)
    raise

UPSTREAM_PORT = int(os.environ.get("MOCK_ZC_UPSTREAM_PORT", "42617"))
ADAPTER_PORT = int(os.environ.get("MOCK_ZC_ADAPTER_PORT", "43617"))
UPSTREAM = f"http://127.0.0.1:{UPSTREAM_PORT}"

app = FastAPI(title="zc-webhook-adapter")


def _post_upstream(path: str, payload: dict) -> tuple[int, dict | None]:
    """Synchronous POST to the mock-stack (its handlers are FastAPI
    coroutines; urllib stays out of the asyncio loop)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        UPSTREAM + path, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as r:
            body = r.read()
            try:
                return r.status, json.loads(body.decode("utf-8"))
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 502, None


@app.post("/webhook")
async def webhook(request: Request):
    """Translate companion-server's `/webhook {message:...}` into the
    mock's `/api/chat`, then return its reply in zeroclaw's response
    shape (key="response")."""
    body = await request.json()
    msg = body.get("message", "")
    sid = request.headers.get("x-session-id")
    # Reuse mock's chat handler (handles forced statuses + slowdowns).
    payload = {"message": msg, "character_id": sid or "default"}
    # Offload sync urllib to a thread so we don't block uvicorn's loop.
    status, data = await asyncio.to_thread(_post_upstream, "/api/chat", payload)
    if status != 200 or not data:
        raise HTTPException(status, "upstream chat failed")
    # zeroclaw_client's webhook parser accepts response/reply/text/content/output.
    return JSONResponse({"response": data.get("reply", ""), "session_id": sid})


@app.get("/api/healthz")
async def healthz():
    try:
        with urllib.request.urlopen(UPSTREAM + "/api/healthz", timeout=2.0) as r:
            return JSONResponse(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, "upstream not healthy")
    except Exception:
        raise HTTPException(503, "upstream unreachable")


@app.get("/api/events")
async def events():
    """SSE keep-alive — yields a tick every 10s so companion-server's SSE
    bridge stays connected and doesn't reconnect-storm against a stream
    that closes after 3 ticks (which is what the mock's /api/events does).
    The frontend rigs drive chat directly, not via events, so the
    payloads here are filler."""
    async def stream():
        i = 0
        while True:
            yield f"event: tick\ndata: {json.dumps({'i': i})}\n\n"
            i += 1
            await asyncio.sleep(10)
    return StreamingResponse(stream(), media_type="text/event-stream")


def main() -> None:
    print(f"[zc-adapter] listening on :{ADAPTER_PORT} → {UPSTREAM}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=ADAPTER_PORT,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
