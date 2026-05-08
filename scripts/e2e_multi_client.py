"""Reproduce the "she said it twice" bug in the abstract.

Connects TWO WebSocket clients at once (simulating Tauri's main +
avatar overlay windows, both running Avatar.tsx), sends ONE chat,
and counts how many Audio frames each client receives. Then verifies
that the (turn_id, seq) identity of each chunk is stable across the
two clients — that's the property the rodio worker relies on for
dedupe.

Without dedupe at the rodio worker, each client's `playAudioNative`
queues the same WAV bytes into the singleton sink, and rodio plays
the chunk twice → user hears the sentence twice.

Run: python scripts/e2e_multi_client.py
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from urllib import request

import websocket

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def collect(label: str, frames_out: list, done: threading.Event) -> websocket.WebSocketApp:
    def on_msg(_ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") in ("Audio", "Idle"):
            frames_out.append(msg)
        if msg.get("type") == "Idle":
            done.set()

    ws = websocket.WebSocketApp(
        "ws://127.0.0.1:9181/ws/avatar",
        on_open=lambda w: w.send(json.dumps({"type": "Ready"})),
        on_message=on_msg,
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()
    return ws


def main() -> int:
    a_frames: list[dict] = []
    b_frames: list[dict] = []
    a_done = threading.Event()
    b_done = threading.Event()
    ws_a = collect("A", a_frames, a_done)
    ws_b = collect("B", b_frames, b_done)
    time.sleep(0.5)  # let both connect

    req = request.Request(
        "http://127.0.0.1:9181/api/chat",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"message": "hello briefly please"}).encode(),
    )
    with request.urlopen(req, timeout=120) as r:
        json.loads(r.read())

    a_done.wait(timeout=120)
    b_done.wait(timeout=120)
    ws_a.close()
    ws_b.close()

    a_audio = [f for f in a_frames if f.get("type") == "Audio"]
    b_audio = [f for f in b_frames if f.get("type") == "Audio"]
    print(f"client A received: {len(a_audio)} audio frames")
    print(f"client B received: {len(b_audio)} audio frames")

    fails = 0
    if not (len(a_audio) > 0 and len(b_audio) > 0):
        print("✗ at least one client got no audio")
        return 1

    # Both clients should see the same turn_id and the same set of seq values.
    a_ids = [(f.get("turn_id"), f.get("seq")) for f in a_audio]
    b_ids = [(f.get("turn_id"), f.get("seq")) for f in b_audio]
    print(f"A chunk ids: {a_ids}")
    print(f"B chunk ids: {b_ids}")

    if a_ids == b_ids:
        print("✓ both clients receive identical (turn_id, seq) — dedupe key works")
    else:
        fails += 1
        print("✗ clients see different chunk identities; dedupe will misfire")

    a_turn = {f.get("turn_id") for f in a_audio}
    if len(a_turn) == 1:
        print(f"✓ single turn_id across A's frames: {next(iter(a_turn))}")
    else:
        fails += 1
        print(f"✗ A saw multiple turn_ids in one turn: {a_turn}")

    print(
        "\nWith two connected clients + native rodio, the dedupe-by-(turn_id,seq) "
        "in run_audio_worker drops the second arrival of each chunk so playback "
        "happens exactly once. Without dedupe, the same audio is appended to the "
        "Sink twice and you hear the sentence twice — what the user reported."
    )
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
