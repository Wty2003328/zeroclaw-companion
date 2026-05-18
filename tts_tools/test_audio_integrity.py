"""L4 — Multi-service audio integrity (GPU).

Drives 4 canned multi-sentence Japanese replies through the full
chat → translate → synthesize → WS-frame pipeline using the REAL TTS
sidecar (Qwen3-TTS or whichever production wire is bound to PORT_TTS).
Sums the decoded WAV durations per turn and asserts each turn produced
at least 65% of its expected duration (char_count * 0.12s).

Gate:
    TTS_REAL=1   — must be set to run; the rig refuses to test against
                   the mock stack (mock returns synthetic silence and
                   would give false-positive duration matches).

Pre-reqs when TTS_REAL=1:
    * A production TTS sidecar already listening on PORT_TTS (9880).
      We do NOT spawn it from this rig — the model load is too heavy
      for a generic test rig.
    * Companion-server's avatar.tts.api_url points at PORT_TTS.

Reference utterances (each ~30-40 chars → ~4-6s expected audio):
    * 今日はとてもいい天気ですね。一緒にお散歩しませんか？
    * おはようございます。今日もよろしくお願いします。
    * 今夜の夕食は何にしましょうか？お寿司はいかがですか。
    * 明日の会議は10時から始まります。資料の準備をお願いします。

Known limitation: certain Japanese greetings ("おはようございます",
"お散歩しませんか？") trigger AR-truncation at the per-segment level.
Those are documented in feedback notes and surface here via r.info,
not r.check — surrounding sentences carry the turn's duration.

Run directly:
    TTS_REAL=1 python -m tts_tools.test_audio_integrity
    python -m tts_tools.test_audio_integrity            # SKIPS without TTS_REAL=1
"""
from __future__ import annotations

import base64
import io
import json
import os
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

import websocket

from tts_tools._test_helpers import (
    CheckReporter,
    PORT_COMPANION,
    PORT_MOCK_ZEROCLAW,
    PORT_NMT,
    PORT_TTS,
    REPO_ROOT,
    http_get,
    http_post_json,
    managed_procs,
    python_exe,
    require_ports_free,
    spawn,
    spawn_companion_server,
    wait_for_port,
    wait_for_url,
)


# ── Canned utterances ────────────────────────────────────────────────
UTTERANCES = [
    "今日はとてもいい天気ですね。一緒にお散歩しませんか？",
    "おはようございます。今日もよろしくお願いします。",
    "今夜の夕食は何にしましょうか？お寿司はいかがですか。",
    "明日の会議は10時から始まります。資料の準備をお願いします。",
]


# Known per-segment AR-truncation hotspots (documented in
# feedback_python_env / project_tts_ar_truncation memory). Logged as
# r.info, not r.check — the turn-level 65% floor is the actual gate.
AR_TRUNC_HOTSPOTS = [
    "おはようございます",
    "お散歩しませんか",
]


def _expected_duration_s(text: str) -> float:
    """0.12s/char heuristic per the rig spec. Returns full expected
    duration (the 65% floor lives in the comparison)."""
    return len(text) * 0.12


def _build_config() -> dict:
    """Companion config wired against the REAL TTS sidecar on PORT_TTS,
    mock zeroclaw on PORT_MOCK_ZEROCLAW (so we deterministically get a
    reply with our canned text fed straight back). NMT subagent points
    at the mock NMT port — when the mock isn't running, the subagent
    falls through and we run JA-direct, which is what we want.
    """
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            "url": f"http://127.0.0.1:{PORT_MOCK_ZEROCLAW}",
            "timeout_secs": 60,
        },
        "server": {"host": "127.0.0.1", "port": PORT_COMPANION},
        "avatar": {
            "enabled": True,
            # Speak in Japanese — no subagent translation needed since
            # we'll feed JA replies straight through to TTS.
            "chat_language": "ja",
            "tts": {
                "engine": "qwen3-tts-1.7b",
                "api_url": f"http://127.0.0.1:{PORT_TTS}",
                "port": PORT_TTS,
                "language": "ja",
                "voice": "asuna",
                "speed": 1.0,
                "quality": "balanced",
                # Don't spawn — assume the user already has it running.
                "auto_start": False,
                "close_with_companion": False,
                "streaming": True,
                "streaming_target_chars": 80,
            },
            "subagent": {
                "enabled": False,
                "only_when_translating": True,
            },
            "speech": {"enabled": False},
        },
        "pulse": {"enabled": False},
    }


def _decode_wav_duration(wav_bytes: bytes) -> Optional[float]:
    """Parse a WAV header and return duration in seconds. None on failure."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate == 0:
                return None
            return frames / float(rate)
    except Exception:
        return None


def _pcm_duration(audio_bytes: bytes, sample_rate: int, channels: int = 1, bytes_per_sample: int = 2) -> float:
    """Compute duration from raw PCM byte length when format != wav."""
    if sample_rate == 0 or bytes_per_sample == 0 or channels == 0:
        return 0.0
    return len(audio_bytes) / float(sample_rate * channels * bytes_per_sample)


def _decode_audio_frame_duration(frame: dict) -> float:
    """Audio frame shape (see crates/companion-avatar/src/protocol.rs):
        {audio: base64, format: "wav"/"mp3"/"pcm", sample_rate: u32, ...}
    Return duration in seconds (0.0 on parse failure).
    """
    audio_b64 = frame.get("audio") or ""
    fmt = (frame.get("format") or "").lower()
    sr = int(frame.get("sample_rate") or 0)
    try:
        raw = base64.b64decode(audio_b64)
    except Exception:
        return 0.0
    if fmt == "wav":
        d = _decode_wav_duration(raw)
        if d is not None:
            return d
        # Header parse failed — fall back to PCM math.
        return _pcm_duration(raw, sr or 24000)
    if fmt == "pcm":
        return _pcm_duration(raw, sr or 24000)
    # mp3 or unknown — we can't decode without an external dep. Estimate
    # by length / sample_rate*2 (16-bit assumption). Surface this as
    # info, the duration won't be exact.
    return _pcm_duration(raw, sr or 24000)


def _drive_turn(base: str, utterance: str, timeout_s: float = 60.0) -> tuple[float, int, list[dict]]:
    """Open WS, send /api/chat with `utterance`, collect every Audio
    frame until the `last=true` chunk lands (or timeout). Returns
    (total_duration_s, num_audio_frames, frame_list).
    """
    url = f"ws://127.0.0.1:{PORT_COMPANION}/ws/avatar"
    ws = websocket.create_connection(url, timeout=10.0)
    ws.settimeout(15.0)
    # Drain Connected + ModelInfo.
    for _ in range(3):
        try:
            msg = ws.recv()
        except Exception:
            break
        try:
            obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
        except Exception:
            continue
        if obj.get("type") == "ModelInfo":
            break

    # Fire chat in a background thread.
    import threading
    chat_done = threading.Event()
    def _post():
        http_post_json(f"{base}/api/chat", {"message": utterance}, timeout=timeout_s)
        chat_done.set()
    t = threading.Thread(target=_post, daemon=True)
    t.start()

    frames: list[dict] = []
    total_duration = 0.0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            msg = ws.recv()
        except Exception:
            break
        try:
            obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
        except Exception:
            continue
        if obj.get("type") == "Audio":
            frames.append(obj)
            total_duration += _decode_audio_frame_duration(obj)
            if obj.get("last") is True:
                break
    try:
        ws.close()
    except Exception:
        pass
    chat_done.wait(timeout=10.0)
    return total_duration, len(frames), frames


def main() -> None:
    r = CheckReporter("test_audio_integrity")

    # ── Gate: TTS_REAL=1 required ───────────────────────────────
    if os.environ.get("TTS_REAL") != "1":
        r.check(
            "real TTS sidecar required",
            False,
            "L4 needs production TTS, not mock — set TTS_REAL=1 to confirm "
            "you have a real sidecar bound to PORT_TTS (9880)",
        )
        # SKIP via summary — exit 0 with one explanatory line.
        # CheckReporter.summary_or_exit returns non-zero on any fail, so
        # we override here: skipped is a clean SKIP, not a FAIL.
        print()
        print(f"  --- {r.suite}: SKIP (TTS_REAL!=1) ---")
        raise SystemExit(0)

    require_ports_free(PORT_COMPANION, PORT_MOCK_ZEROCLAW)
    # Confirm the real TTS is actually listening before we start.
    if not wait_for_port(PORT_TTS, timeout_s=2.0):
        r.check(
            f"real TTS sidecar bound on PORT_TTS={PORT_TTS}",
            False,
            "TTS_REAL=1 set but nothing listening — start qwen3 sidecar first",
        )
        r.summary_or_exit()
    # Probe its /healthz to make sure it's actually a TTS server, not a
    # leftover mock from a previous run.
    status, body, _ = http_get(f"http://127.0.0.1:{PORT_TTS}/healthz", timeout=5.0)
    if status != 200:
        r.check(
            f"real TTS /healthz responds 200",
            False,
            f"got status={status}",
        )
        r.summary_or_exit()

    scratch = Path(tempfile.mkdtemp(prefix="companion-l4-"))
    log_dir = scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    with managed_procs() as procs:
        # Mock zeroclaw only — we want a deterministic reply. NMT mock
        # not needed (chat_lang == tts.language so subagent is bypassed
        # even if it were enabled). TTS is the REAL sidecar (external).
        # Spawn just the mock zeroclaw via _mock_stack's full stack —
        # except _mock_stack also binds PORT_TTS which would collide
        # with the real sidecar. So we cannot reuse _mock_stack here;
        # we need a zeroclaw-only mock.
        #
        # Workaround: run companion-server pointed at a mock zeroclaw
        # that echoes the user message back as the reply. We use a
        # tiny inline HTTP mock spawn for that.
        zc_script = scratch / "zc_mock.py"
        # Companion (kind=zeroclaw) speaks /webhook, not /api/chat — see
        # crates/companion-core/src/zeroclaw.rs::send_chat_webhook. The
        # inline mock here implements BOTH paths so we don't need to
        # spawn the rig shim alongside.
        zc_script.write_text(
            "import asyncio, json\n"
            "from fastapi import FastAPI, Request\n"
            "import uvicorn\n"
            "app = FastAPI()\n"
            "@app.get('/api/healthz')\n"
            "async def hz(): return {'status':'ok'}\n"
            "@app.get('/health')\n"
            "async def h(): return {'status':'ok'}\n"
            "@app.post('/api/chat')\n"
            "async def chat(r: Request):\n"
            "    body = await r.json()\n"
            "    return {'reply': body.get('message',''), 'character_id': body.get('character_id','default')}\n"
            "@app.post('/webhook')\n"
            "async def webhook(r: Request):\n"
            "    body = await r.json()\n"
            "    # Echo user message as the agent reply so TTS speaks the\n"
            "    # canned Japanese utterance verbatim.\n"
            "    return {'response': body.get('message',''), 'model':'echo'}\n"
            f"uvicorn.run(app, host='127.0.0.1', port={PORT_MOCK_ZEROCLAW}, log_level='warning', access_log=False)\n",
            encoding="utf-8",
        )
        zc = spawn(
            "mock-zeroclaw-echo",
            [python_exe(), str(zc_script)],
            port=PORT_MOCK_ZEROCLAW,
            log_path=log_dir / "mock-zc.log",
        )
        procs.append(zc)
        if not wait_for_url(f"http://127.0.0.1:{PORT_MOCK_ZEROCLAW}/health", timeout_s=15.0):
            r.check("mock zeroclaw echoer bound", False)
            r.summary_or_exit()

        comp, _ = spawn_companion_server(
            _build_config(), port=PORT_COMPANION, log_dir=log_dir,
            scratch_dir=scratch,
        )
        procs.append(comp)
        if not wait_for_url(f"http://127.0.0.1:{PORT_COMPANION}/health", timeout_s=30.0):
            r.check("companion /health came up", False, "30s timeout")
            r.summary_or_exit()

        base = f"http://127.0.0.1:{PORT_COMPANION}"

        # ── Per-utterance run ───────────────────────────────────
        for idx, text in enumerate(UTTERANCES):
            expected = _expected_duration_s(text)
            floor = 0.65 * expected
            r.info(
                f"turn {idx+1}/4: chars={len(text)} expected≈{expected:.1f}s "
                f"floor≈{floor:.1f}s"
            )
            # AR-truncation hotspot warnings (advisory).
            for hot in AR_TRUNC_HOTSPOTS:
                if hot in text:
                    r.info(f"  contains AR-truncation hotspot: {hot!r}")
            try:
                duration, n_frames, _frames = _drive_turn(base, text, timeout_s=90.0)
            except Exception as e:  # noqa: BLE001
                r.check(
                    f"turn {idx+1} pipeline completed",
                    False,
                    f"exception: {e}",
                )
                continue
            r.info(f"  got {n_frames} Audio frames, total={duration:.2f}s")
            r.check(
                f"turn {idx+1} duration ≥ 65% of expected",
                duration >= floor,
                f"got {duration:.2f}s, floor {floor:.2f}s, expected {expected:.2f}s",
            )

    r.summary_or_exit()


if __name__ == "__main__":
    main()
