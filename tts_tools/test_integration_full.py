"""L6e — Integration full pipeline.

End-to-end wiring assertions. The chat path runs companion-server →
rig zc shim → mock zeroclaw → mock TTS → WS broadcast; the voice-input
path runs frontend POST /api/avatar/asr → speech shim → text → /api/chat.

Target: 4/4 green.
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path

from tts_tools._rig_shim import (
    PORT_SPEECH_SHIM,
    PORT_ZC_SHIM,
    require_ports_free_with_wait,
    robust_http_get as http_get,
    robust_http_json as http_json,
    robust_http_post_json as http_post_json,
    start_shims,
    wait_for_shims,
)
from tts_tools._test_helpers import (
    CheckReporter,
    PORT_COMPANION,
    PORT_CONTROL,
    PORT_MOCK_ZEROCLAW,
    PORT_NMT,
    PORT_TTS,
    REPO_ROOT,
    managed_procs,
    python_exe,
    spawn,
    spawn_companion_server,
    wait_for_port,
    wait_for_url,
)

try:
    import websocket  # type: ignore
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False


def _config() -> dict:
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            "url": f"http://127.0.0.1:{PORT_ZC_SHIM}",
            "timeout_secs": 10,
        },
        "server": {"host": "127.0.0.1", "port": PORT_COMPANION},
        "avatar": {
            "enabled": True,
            "chat_language": "en",
            "tts": {
                "engine": "mock",
                "api_url": f"http://127.0.0.1:{PORT_TTS}",
                "port": PORT_TTS,
                "language": "en",
                "voice": "asuna",
                "speed": 1.0,
                "quality": "balanced",
                "auto_start": False,
                "close_with_companion": False,
                "streaming": True,
                "streaming_target_chars": 80,
            },
            "subagent": {"enabled": False},
            "speech": {
                "enabled": True,
                "port": PORT_SPEECH_SHIM,
                "api_url": f"http://127.0.0.1:{PORT_SPEECH_SHIM}",
                "launch_command": "",
                "auto_start": False,
                "close_with_companion": False,
                "verify_tts": False,
                "warmup": False,
            },
        },
        "pulse": {"enabled": False},
    }


def _collect_until(ws, deadline: float, types: set[str]) -> list[dict]:
    """Drain frames until we've seen at least one of every requested
    `types` OR `deadline` is hit. Returns the collected frames in
    chronological order."""
    out: list[dict] = []
    seen: set[str] = set()
    while time.time() < deadline:
        try:
            ws.settimeout(max(0.1, deadline - time.time()))
            raw = ws.recv()
        except Exception:
            return out
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        out.append(msg)
        if msg.get("type") in types:
            seen.add(msg["type"])
        if seen >= types:
            return out
    return out


def main() -> None:
    r = CheckReporter("test_integration_full")
    require_ports_free_with_wait(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        wait_s=12.0,
    )

    if not WS_AVAILABLE:
        # WS coverage is the entire point of this rig. If it's missing
        # we can still run the HTTP-only invariant (1/4) and document.
        pass

    scratch = Path(tempfile.mkdtemp(prefix="companion-l6e-"))
    log_dir = scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    zc_shim, speech_shim = start_shims()
    try:
        with managed_procs() as procs:
            mock = spawn(
                "mock-stack",
                [python_exe(), "-m", "tts_tools._mock_stack"],
                port=PORT_CONTROL, cwd=REPO_ROOT,
                log_path=log_dir / "mock-stack.log",
            )
            procs.append(mock)
            if not wait_for_port(PORT_CONTROL, timeout_s=15.0):
                r.check("mock stack up", False, "")
                r.summary_or_exit()
            for p in (PORT_TTS, PORT_NMT, PORT_MOCK_ZEROCLAW):
                wait_for_port(p, timeout_s=10.0)
            wait_for_shims(timeout_s=5.0)

            comp, _ = spawn_companion_server(
                _config(), port=PORT_COMPANION, log_dir=log_dir, scratch_dir=scratch,
            )
            procs.append(comp)
            if not wait_for_url(f"http://127.0.0.1:{PORT_COMPANION}/health", timeout_s=30.0):
                r.check("companion up", False, "")
                r.summary_or_exit()

            base = f"http://127.0.0.1:{PORT_COMPANION}"
            ws_url = f"ws://127.0.0.1:{PORT_COMPANION}/ws/avatar"

            if not WS_AVAILABLE:
                r.check("websocket-client available for integration", False,
                        "skipped — install websocket-client for full coverage")
                r.summary_or_exit()
                return

            # ── 1. Chat → WS Audio frame within 5s ────────────────
            ws = websocket.create_connection(ws_url, timeout=5.0)
            ws.settimeout(2.0)
            # Drain Connected + ModelInfo first.
            for _ in range(2):
                try:
                    ws.recv()
                except Exception:
                    break
            t0 = time.time()
            http_post_json(f"{base}/api/chat",
                           {"message": "integration ping with two sentences. another one!"},
                           timeout=15.0)
            frames = _collect_until(ws, deadline=t0 + 10.0, types={"Audio"})
            audio = [f for f in frames if f.get("type") == "Audio"]
            r.check("chat → WS Audio frame within 10s",
                    len(audio) >= 1 and (time.time() - t0) < 10.0,
                    f"frames={len(audio)}")
            if audio:
                af = audio[0]
                has_lip = "lip_sync" in af  # may be empty list ok
                valid_b64 = isinstance(af.get("audio"), str) and len(af["audio"]) > 0
                r.check("Audio frame has lip_sync (may be empty) + valid base64",
                        has_lip and valid_b64,
                        f"lip_sync_key={has_lip} b64_len={len(af.get('audio',''))}")
            else:
                r.check("Audio frame has lip_sync (may be empty) + valid base64",
                        False, "no audio frames")

            # ── 3. Streaming TTS invariants: seq contiguous, last=True
            #       only on final, all share turn_id. ───────────────
            # Use the audio frames we just gathered, then drain more to
            # ensure we have the whole turn.
            ws.settimeout(0.8)
            t_end = time.time() + 5.0
            while time.time() < t_end:
                try:
                    raw = ws.recv()
                    msg = json.loads(raw)
                    if msg.get("type") == "Audio":
                        audio.append(msg)
                        if msg.get("last"):
                            break
                except Exception:
                    break
            audio.sort(key=lambda f: f.get("seq", 0))
            seqs = [f.get("seq") for f in audio]
            turn_ids = {f.get("turn_id") for f in audio}
            last_flags = [f.get("last") for f in audio]
            contiguous = seqs == list(range(len(seqs))) if seqs else False
            only_final_is_last = (
                (not last_flags) or
                (last_flags[-1] is True and all(not x for x in last_flags[:-1]))
            )
            r.check("Audio frames seq=0..N-1 contiguous, last only on final, single turn_id",
                    contiguous and only_final_is_last and len(turn_ids) == 1 and bool(turn_ids),
                    f"seqs={seqs} last={last_flags} turn_ids={turn_ids}")

            # ── 2. Voice input loop: ASR → chat → reply ───────────
            audio_bytes = b"\x00" * 8000  # 1/4s @ 16k
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            status, body, _ = http_post_json(
                f"{base}/api/avatar/asr",
                {"audio": audio_b64, "language": "en"},
                timeout=10.0,
            )
            transcript = ""
            if status == 200:
                try:
                    transcript = json.loads(body.decode("utf-8")).get("text", "")
                except Exception:
                    pass
            status2, body2, _ = http_post_json(
                f"{base}/api/chat",
                {"message": transcript or "fallback msg"},
                timeout=10.0,
            )
            try:
                reply = json.loads(body2.decode("utf-8")).get("reply", "")
            except Exception:
                reply = ""
            r.check("voice loop: ASR → /api/chat → reply round-trips",
                    status == 200 and status2 == 200 and bool(reply),
                    f"asr_status={status} chat_status={status2} reply={reply!r}")

            ws.close()

            # ── 4. Two WS clients each see UserMessage echo ───────
            wa = websocket.create_connection(ws_url, timeout=5.0)
            wb = websocket.create_connection(ws_url, timeout=5.0)
            for w in (wa, wb):
                w.settimeout(2.0)
                for _ in range(2):
                    try:
                        w.recv()
                    except Exception:
                        break
            http_post_json(f"{base}/api/chat",
                           {"message": "fanout test"},
                           timeout=10.0)
            seen_a = seen_b = False
            for w, label in ((wa, "a"), (wb, "b")):
                w.settimeout(2.0)
                end = time.time() + 5.0
                while time.time() < end:
                    try:
                        raw = w.recv()
                        msg = json.loads(raw)
                        if msg.get("type") == "UserMessage":
                            if label == "a":
                                seen_a = True
                            else:
                                seen_b = True
                            break
                    except Exception:
                        break
            r.check("two WS clients both receive UserMessage echo",
                    seen_a and seen_b, f"a={seen_a} b={seen_b}")
            wa.close()
            wb.close()

        r.summary_or_exit()
    finally:
        try:
            zc_shim.shutdown()
        except Exception:
            pass
        try:
            speech_shim.shutdown()
        except Exception:
            pass
        try:
            shutil.rmtree(scratch, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
