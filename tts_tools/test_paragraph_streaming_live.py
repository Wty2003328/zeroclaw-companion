"""L8 — End-to-end paragraph-streaming check, hitting the LIVE stack.

This is the rig that should have run BEFORE I claimed any fix worked.

Connects to a running companion-server via WebSocket /ws/avatar, sends
a multi-sentence chat through /api/chat, counts the number of Audio
frames the server broadcasts on the WS. If <2, the streaming chunker
is broken / regressed.

Run AGAINST AN ALREADY-RUNNING STACK:
  python -m tts_tools.test_paragraph_streaming_live

Does NOT spawn its own stack — you provide it via Tauri or by
launching companion-server manually. Reason: the actual user-facing
stack is Tauri-spawned, and we want to exercise that path.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Optional

from websocket import create_connection  # type: ignore

for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass


COMPANION_BASE = "http://127.0.0.1:9181"
COMPANION_WS = "ws://127.0.0.1:9181/ws/avatar"

# A 5-sentence single-paragraph EN message. The user sends this to the
# companion; zeroclaw replies; subagent translates EN→JA (or skips if
# chat_lang==tts_lang); the companion's chunker splits and emits N
# Audio frames. We assert N >= 2.
#
# Single-paragraph by design (no \n\n) — exactly the failure mode that
# the old `split("\n\n")` chunker collapsed to one chunk.
TEST_MESSAGE = (
    "请给我讲一个比较长的故事,要分成至少三个段落。\n\n"
    "第一段描述你早上做了什么,天气怎么样,看到了什么有趣的东西。"
    "第二段说你中午吃了什么,在哪里吃,跟谁一起。"
    "第三段讲晚上的事,以及一件让你感到惊讶的事情。\n\n"
    "每段请写至少两三句话,不要省略细节,我想听一个完整的、分段清晰的故事。"
)


def log(msg: str) -> None:
    print(f"[l8-stream] {msg}", flush=True)


def fail(msg: str, code: int = 1):
    print(f"[l8-stream] FAIL: {msg}", flush=True)
    sys.exit(code)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--timeout", type=float, default=120.0,
                   help="seconds to wait for the full reply + audio frames")
    p.add_argument("--min-frames", type=int, default=2,
                   help="fail if fewer than N Audio frames received")
    args = p.parse_args()

    # 0. Sanity: companion-server is up and healthy
    try:
        r = urllib.request.urlopen(f"{COMPANION_BASE}/health", timeout=5).read().decode()
    except Exception as e:
        fail(f"companion-server not reachable at {COMPANION_BASE}: {e!r}")
    if "ok" not in r.lower():
        fail(f"companion /health unhealthy: {r}")
    log(f"companion-server reachable: {r.strip()}")

    # 1. Open the WS and wait for Connected.
    log(f"connecting WS {COMPANION_WS}")
    ws = create_connection(COMPANION_WS, timeout=10)
    ws.send(json.dumps({"type": "Ready"}))

    # 2. Send a chat. Spawn in background so we can collect WS frames.
    log(f"POST /api/chat ({len(TEST_MESSAGE)} chars)")
    body = json.dumps({"message": TEST_MESSAGE}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMPANION_BASE}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    import threading
    chat_result = {"status": None, "body": None, "err": None}
    def send_chat():
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                chat_result["status"] = resp.status
                chat_result["body"] = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            chat_result["err"] = repr(e)
    thr = threading.Thread(target=send_chat, daemon=True)
    thr.start()

    # 3. Drain WS frames for up to `timeout` seconds. Count Audio frames.
    deadline = time.time() + args.timeout
    audio_frames = 0
    audio_seqs: list = []
    expression_frames = 0
    text_frame_chars: Optional[int] = None
    last_audio_at: Optional[float] = None
    ws.settimeout(2.0)

    while time.time() < deadline:
        try:
            msg = ws.recv()
        except Exception:
            # Timeout on recv — just keep looping until deadline
            # if chat hasn't returned yet, OR break if chat is done +
            # we've been idle for >5s after the last frame.
            if chat_result["status"] is not None and last_audio_at is not None:
                if time.time() - last_audio_at > 5.0:
                    log("idle >5s after last audio + chat returned — done")
                    break
            continue
        try:
            j = json.loads(msg)
        except Exception:
            continue
        t = j.get("type")
        if t == "Audio":
            audio_frames += 1
            audio_seqs.append(j.get("chunk_index", j.get("seq")))
            last_audio_at = time.time()
            log(f"  WS Audio frame #{audio_frames}: index={j.get('chunk_index')} bytes={len(j.get('audio',''))}")
            # Save the raw WAV to disk for amplitude inspection.
            import base64, os
            wav_b64 = j.get("audio", "")
            if wav_b64:
                try:
                    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tts_samples")
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, f"_live_chunk_{audio_frames}.wav")
                    with open(out_path, "wb") as f:
                        f.write(base64.b64decode(wav_b64))
                    log(f"    saved → {out_path}")
                except Exception as e:
                    log(f"    save failed: {e!r}")
        elif t == "Expression":
            expression_frames += 1
        elif t == "Text":
            text_frame_chars = len(j.get("content", ""))
        elif t == "Idle":
            log("  WS Idle — server says turn is done")
            # Give a small grace for any final Audio still in flight
            time.sleep(1.0)
            break

    ws.close()
    thr.join(timeout=5.0)

    log(f"chat result: status={chat_result['status']} err={chat_result['err']!r}")
    log(f"audio frames received: {audio_frames} (seqs={audio_seqs})")
    log(f"expression frames: {expression_frames}, text frame chars: {text_frame_chars}")

    if chat_result["err"]:
        fail(f"chat POST errored: {chat_result['err']}")
    if chat_result["status"] != 200:
        fail(f"chat POST returned status {chat_result['status']}")
    if audio_frames < args.min_frames:
        fail(f"only {audio_frames} Audio frames received, expected >={args.min_frames} "
             f"— the chunker collapsed to single-shot")
    log(f"✅ PASS — {audio_frames} Audio frames for a 5-sentence input")
    return 0


if __name__ == "__main__":
    sys.exit(main())
