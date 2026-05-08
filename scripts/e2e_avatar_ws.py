"""End-to-end avatar pipeline check.

Connects to companion's /ws/avatar, fires off /api/chat in a background
thread, then watches for the expected frame sequence:

    Connected → ModelInfo → (after chat fires) Expression → Text → Audio → Idle

If we see all four post-chat frames, the full pipeline is wired correctly.
The Audio frame's base64 payload size also tells us the real TTS produced
audio bytes for us.
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import requests
import websockets

# Force UTF-8 so the GLM emoji-laden replies don't crash the Windows GBK
# console encoder.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


COMPANION_HTTP = "http://127.0.0.1:9181"
COMPANION_WS = "ws://127.0.0.1:9181/ws/avatar"
TEST_MESSAGE = "Reply with one short cheerful sentence."


async def watch_and_chat() -> int:
    print(f"connecting to {COMPANION_WS}", flush=True)
    async with websockets.connect(COMPANION_WS) as ws:
        # Drain Connected + ModelInfo
        for _ in range(2):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            print(f"  ← {msg.get('type')}", flush=True)

        # Send Ready (companion expects it)
        await ws.send(json.dumps({"type": "Ready"}))

        # Now fire the chat. Use a thread because requests is blocking.
        def fire_chat():
            time.sleep(0.5)
            r = requests.post(
                f"{COMPANION_HTTP}/api/chat",
                json={"message": TEST_MESSAGE},
                timeout=120,
            )
            print(f"  /api/chat → {r.status_code}: {r.json().get('reply', '')[:80]}", flush=True)

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, fire_chat)

        # Watch frames for up to 90s
        seen = {"Expression": False, "Text": False, "Audio": False, "Idle": False}
        deadline = time.time() + 90
        while not all(seen.values()) and time.time() < deadline:
            try:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            except asyncio.TimeoutError:
                continue

            t = frame.get("type")
            if t in seen:
                seen[t] = True

            if t == "Expression":
                print(
                    f"  ← Expression: name={frame.get('name')}  intensity={frame.get('intensity')}",
                    flush=True,
                )
            elif t == "Text":
                content = frame.get("content", "")
                print(f"  ← Text: {content[:120]}", flush=True)
            elif t == "Audio":
                audio_b64 = frame.get("audio", "")
                fmt = frame.get("format")
                sr = frame.get("sample_rate")
                size = len(base64.b64decode(audio_b64)) if audio_b64 else 0
                print(
                    f"  ← Audio: {size} bytes  format={fmt}  sample_rate={sr}",
                    flush=True,
                )
                # Save for inspection
                out = Path(__file__).parent.parent / "e2e_audio.wav"
                if audio_b64:
                    out.write_bytes(base64.b64decode(audio_b64))
                    print(f"      saved → {out}", flush=True)
            elif t == "Idle":
                print("  ← Idle", flush=True)
            else:
                print(f"  ← {t}", flush=True)

        print()
        all_ok = all(seen.values())
        for k, v in seen.items():
            mark = "✓" if v else "✗"
            print(f"  {mark} {k}")
        return 0 if all_ok else 1


if __name__ == "__main__":
    rc = asyncio.run(watch_and_chat())
    sys.exit(rc)
