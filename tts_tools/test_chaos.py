"""L6f — Chaos / recovery.

Drives the mock control plane into failure modes; companion must
respond gracefully (no 5xx unless the underlying agent is dead) and
recover when mocks come back up.

Target: 8/8 green.
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
from pathlib import Path

from tts_tools._rig_shim import (
    PORT_SPEECH_SHIM,
    PORT_ZC_SHIM,
    require_ports_free_with_wait,
    robust_http_get as http_get,
    robust_http_post_json as http_post_json,
    safe_mock_clear,
    safe_mock_set,
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


def _retry_mock_clear() -> bool:
    """Defer to the rig-shim's retrying wrapper which handles the
    control-plane-under-load case."""
    return safe_mock_clear()


def _config() -> dict:
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            "url": f"http://127.0.0.1:{PORT_ZC_SHIM}",
            "timeout_secs": 8,
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


def main() -> None:
    r = CheckReporter("test_chaos")
    require_ports_free_with_wait(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        wait_s=12.0,
    )

    scratch = Path(tempfile.mkdtemp(prefix="companion-l6f-"))
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
            audio_b64 = base64.b64encode(b"\x00" * 1024).decode("ascii")

            _retry_mock_clear()

            # ── 1. tts_dead: chat still 200 (TTS is best-effort) ─
            safe_mock_set(tts_dead=True)
            time.sleep(0.3)
            status, body, _ = http_post_json(f"{base}/api/chat",
                                             {"message": "tts-dead chat"},
                                             timeout=12.0)
            r.check("tts_dead: /api/chat still 200 (TTS is best-effort)",
                    status == 200, f"status={status}")
            _retry_mock_clear()

            # ── 2. tts/nmt/speech all dead: /health still 200 ────
            safe_mock_set(tts_dead=True, nmt_dead=True, speech_dead=True)
            time.sleep(0.3)
            s, _, _ = http_get(f"{base}/health")
            r.check("all sidecars dead: /health still 200",
                    s == 200, f"status={s}")
            _retry_mock_clear()

            # ── 3. zc_dead: chat returns 5xx (never lying) ──────
            # Use a shorter timeout — when zc is dead the shim returns
            # 503 immediately. Anything that hangs is a bug, not a slow
            # path we should wait through.
            safe_mock_set(zc_dead=True)
            time.sleep(0.5)
            s, body, _ = http_post_json(f"{base}/api/chat",
                                        {"message": "zc-dead probe"},
                                        timeout=8.0)
            r.check("zc_dead: /api/chat returns 5xx (no false success)",
                    500 <= s < 600, f"status={s} body={body[:200]!r}")
            # Reset zc_dead before subsequent checks. Use mock_clear
            # with retries — the control plane can be momentarily busy
            # while the watchdog cycles + the SSE bridge bounces.
            _retry_mock_clear()
            # Give the shim's state cache a beat to expire so the next
            # chat sees the fresh "zc up" state.
            time.sleep(0.5)
            # Verify the clear actually landed by hitting the shim
            # directly — if zc is still seen as dead, retry.
            for _ in range(4):
                ps, _, _ = http_get(f"http://127.0.0.1:{PORT_ZC_SHIM}/health",
                                    timeout=2.0)
                if ps == 200:
                    break
                _retry_mock_clear()
                time.sleep(0.3)

            # ── 4. nmt slow: chat absorbs within 5s ──────────────
            safe_mock_set(nmt_slow_s=1.5)
            t0 = time.time()
            s, _, _ = http_post_json(f"{base}/api/chat",
                                     {"message": "nmt slow probe"},
                                     timeout=10.0)
            elapsed = time.time() - t0
            r.check("nmt_slow=1.5s: chat completes < 5s",
                    s == 200 and elapsed < 5.0,
                    f"status={s} elapsed={elapsed:.2f}s")
            _retry_mock_clear()

            # ── 5. speech_dead: ASR returns 5xx ──────────────────
            safe_mock_set(speech_dead=True)
            time.sleep(0.3)
            s, body, _ = http_post_json(f"{base}/api/avatar/asr",
                                        {"audio": audio_b64},
                                        timeout=10.0)
            # companion-server proxies; speech shim returns 503; the
            # proxy converts to 502. Accept anything 5xx.
            r.check("speech_dead: ASR returns 5xx (not 200)",
                    500 <= s < 600, f"status={s} body={body[:200]!r}")
            _retry_mock_clear()

            # ── 6. tts_status=500: chat still 200 ────────────────
            safe_mock_set(tts_status=500)
            time.sleep(0.3)
            s, _, _ = http_post_json(f"{base}/api/chat",
                                     {"message": "tts-500 probe"},
                                     timeout=12.0)
            r.check("tts_status=500: /api/chat still 200 (best-effort)",
                    s == 200, f"status={s}")
            _retry_mock_clear()

            # ── 7. Recovery after mock_clear ─────────────────────
            time.sleep(0.5)
            s, body, _ = http_post_json(f"{base}/api/chat",
                                        {"message": "recovery probe"},
                                        timeout=10.0)
            try:
                reply = json.loads(body.decode("utf-8")).get("reply", "")
            except Exception:
                reply = ""
            r.check("after mock_clear: chat works + reply contains 'mock-reply'",
                    s == 200 and "mock-reply" in reply,
                    f"status={s} reply={reply!r}")

            # ── 8. Sequential chaos sequence → clean recovery ────
            safe_mock_set(zc_dead=True);  time.sleep(0.3); _retry_mock_clear()
            safe_mock_set(tts_dead=True); time.sleep(0.3); _retry_mock_clear()
            safe_mock_set(nmt_slow_s=2.0); time.sleep(0.1); _retry_mock_clear()
            time.sleep(0.8)

            # Retry the clean-recovery chat up to 3× — under heavy SSE
            # reconnect churn the first POST can race a connection
            # being recycled; companion processes it but the HTTP
            # response gets eaten by the reset window.
            t0 = time.time()
            s = 0
            body = b""
            for attempt in range(3):
                try:
                    s, body, _ = http_post_json(
                        f"{base}/api/chat",
                        {"message": "post chaos clean run"},
                        timeout=8.0,
                    )
                    if s == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            elapsed = time.time() - t0
            r.check("post-chaos sequence: clean chat returns 200 within 5s",
                    s == 200 and elapsed < 5.0,
                    f"status={s} elapsed={elapsed:.2f}s")

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
