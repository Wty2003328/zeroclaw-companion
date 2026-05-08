"""Capture and inspect TTS audio chunks for duplication.

Sends a chat, captures every Audio frame off the WS, decodes the WAV
bytes, computes:
- chunk hash (md5 of audio bytes) — identical hash means same audio,
  same words, repeated
- chunk duration (samples / sample_rate) — total speech time
- a 1024-sample fingerprint — to catch near-identical audio that's
  not byte-identical

If the same fingerprint shows up twice in one turn, that's the "she
said it twice" bug, and the cause is duplicate Audio frames being
emitted by process_speak (server-side bug) — not anything in the
frontend or rodio playback.

Run:
  python scripts/e2e_audio_inspect.py "what's the weather"
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import sys
import threading
import time
import wave
from urllib import request

import websocket

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

WS = "ws://127.0.0.1:9181/ws/avatar"
HTTP = "http://127.0.0.1:9181/api/chat"


def fingerprint(samples: bytes, n: int = 1024) -> str:
    """Cheap fingerprint: first/middle/last N samples, hashed.
    Two pieces of audio that are 'essentially identical' should match."""
    if len(samples) < 3 * n:
        return hashlib.md5(samples).hexdigest()
    mid = len(samples) // 2 - n // 2
    head = samples[:n]
    middle = samples[mid : mid + n]
    tail = samples[-n:]
    return hashlib.md5(head + middle + tail).hexdigest()


def analyze_wav(wav_bytes: bytes) -> dict:
    """Extract duration + fingerprint from a WAV blob."""
    if not wav_bytes:
        return {"empty": True}
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            n_frames = w.getnframes()
            rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            raw = w.readframes(n_frames)
    except Exception as e:
        return {"error": f"wave parse: {e}", "size": len(wav_bytes)}
    return {
        "size_bytes": len(wav_bytes),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": sampwidth,
        "frames": n_frames,
        "duration_s": round(n_frames / rate, 2) if rate else None,
        "fingerprint": fingerprint(raw),
        "md5_full": hashlib.md5(wav_bytes).hexdigest(),
    }


def run(message: str) -> int:
    audio_frames: list[dict] = []
    debug_frame: dict = {}
    text_frames: list[str] = []
    done = threading.Event()

    def on_message(_ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("type")
        if t == "Audio":
            audio_frames.append(msg)
        elif t == "Debug":
            debug_frame.update(msg)
        elif t == "Text":
            text_frames.append(msg.get("content", ""))
        elif t == "Idle":
            done.set()

    ws = websocket.WebSocketApp(
        WS,
        on_open=lambda w: w.send(json.dumps({"type": "Ready"})),
        on_message=on_message,
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()
    time.sleep(0.5)

    req = request.Request(
        HTTP,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"message": message}).encode(),
    )
    started = time.monotonic()
    with request.urlopen(req, timeout=180) as r:
        body = json.loads(r.read())

    done.wait(timeout=180)
    ws.close()

    print(f"\n=== {message!r} ===")
    print(f"elapsed: {round(time.monotonic() - started, 1)}s")
    print(f"raw_reply ({len(body.get('reply', ''))}c): {body.get('reply', '')[:200]!r}")
    print(f"clean_chat: {debug_frame.get('chat_text', '')[:200]!r}")
    print(f"spoken: {debug_frame.get('spoken_text', '')[:300]!r}")
    print(f"subagent_used: {debug_frame.get('subagent_used')}")
    print(f"text_frames: {len(text_frames)}: {[t[:80] for t in text_frames]}")
    print(f"audio_frames: {len(audio_frames)}")

    fingerprints: dict[str, list[int]] = {}
    md5s: dict[str, list[int]] = {}
    total_dur = 0.0
    for i, frame in enumerate(audio_frames):
        b64 = frame.get("audio") or ""
        wav = base64.b64decode(b64) if b64 else b""
        info = analyze_wav(wav)
        fp = info.get("fingerprint")
        md5 = info.get("md5_full")
        dur = info.get("duration_s") or 0
        total_dur += dur
        print(
            f"  [{i:2d}] seq={frame.get('seq')} last={frame.get('last')} "
            f"dur={dur}s size={info.get('size_bytes')} "
            f"md5={md5[:8] if md5 else '-'} fp={fp[:8] if fp else '-'}"
        )
        if fp:
            fingerprints.setdefault(fp, []).append(i)
        if md5:
            md5s.setdefault(md5, []).append(i)

    print(f"total speech duration: {round(total_dur, 1)}s")

    fails = 0
    dupes_md5 = {h: idxs for h, idxs in md5s.items() if len(idxs) > 1}
    dupes_fp = {h: idxs for h, idxs in fingerprints.items() if len(idxs) > 1}
    if dupes_md5:
        print(f"✗ DUPLICATE byte-identical audio at indices: {dupes_md5}")
        fails += 1
    elif dupes_fp:
        print(f"✗ DUPLICATE fingerprints (near-identical): {dupes_fp}")
        fails += 1
    else:
        print("✓ no duplicate audio chunks")

    return fails


def main() -> int:
    msgs = sys.argv[1:] or [
        "hello",
        "thank me kindly",
        "tell me about ramen",
    ]
    fails = sum(run(m) for m in msgs)
    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} duplications detected")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
