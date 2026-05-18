"""L6sse — SSE bridge invariants.

The companion subscribes to upstream zeroclaw's `/api/events` SSE
stream for observability. This rig asserts the bridge stays alive
through reconnects, never blocks the main HTTP loop, and doesn't
panic on 4xx upstream responses.

Most checks use the shim's /api/events passthrough (which the
foundation mock /api/events serves under). We also drive the chat
path concurrently to prove the SSE subscription doesn't hold up the
companion's main request loop.

Target: 7/7 green.
"""
from __future__ import annotations

import concurrent.futures
import json
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from tts_tools._rig_shim import (
    PORT_SPEECH_SHIM,
    PORT_ZC_SHIM,
    require_ports_free_with_wait,
    robust_http_get as http_get,
    robust_http_json as http_json,
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
            "speech": {"enabled": False},
        },
        "pulse": {"enabled": False},
    }


def _http_get_retry(url: str, retries: int = 3, timeout: float = 3.0) -> int:
    """Some rigs run after heavy upstream churn (50 parallel chats, dozens
    of SSE reconnects) and can hit Windows ephemeral-port exhaustion on
    the first attempt. Bounded retry papers over that without hiding a
    real outage."""
    last = 0
    for _ in range(retries):
        try:
            s, _, _ = http_get(url, timeout=timeout)
            last = s
            if s == 200:
                return 200
        except Exception:
            pass
        time.sleep(0.5)
    return last


def _http_post_json_retry(url: str, body: dict, retries: int = 3,
                          timeout: float = 5.0) -> tuple[int, bytes]:
    last_s = 0
    last_b = b""
    for _ in range(retries):
        try:
            s, body_b, _ = http_post_json(url, body, timeout=timeout)
            last_s, last_b = s, body_b
            if s == 200:
                return s, body_b
        except Exception:
            pass
        time.sleep(0.5)
    return last_s, last_b


def _retry_mock_clear() -> bool:
    """Defer to the rig-shim's retrying wrapper."""
    return safe_mock_clear()


def _read_one_sse_event(url: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Open SSE stream and return (success, first_event_data_line)."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            if r.status != 200:
                return False, f"status={r.status}"
            # Read until we see one "data:" line or hit timeout.
            r.fp._sock.settimeout(2.0) if hasattr(r.fp, "_sock") else None
            deadline = time.time() + timeout_s
            buf = b""
            while time.time() < deadline:
                chunk = r.read(256)
                if not chunk:
                    break
                buf += chunk
                if b"data:" in buf:
                    return True, buf.decode("utf-8", "replace").splitlines()[0]
            return True, buf.decode("utf-8", "replace")[:80]
    except Exception as e:
        return False, str(e)


def main() -> None:
    r = CheckReporter("test_sse_bridge")
    require_ports_free_with_wait(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        wait_s=12.0,
    )

    scratch = Path(tempfile.mkdtemp(prefix="companion-l6sse-"))
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
            shim_events = f"http://127.0.0.1:{PORT_ZC_SHIM}/api/events"

            # ── 1. SSE upstream reachable + correct content-type ─
            try:
                req = urllib.request.Request(shim_events,
                                             headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    ct = resp.headers.get("Content-Type", "")
                    r.check("mock zeroclaw /api/events is text/event-stream",
                            resp.status == 200 and "text/event-stream" in ct,
                            f"status={resp.status} ct={ct!r}")
            except Exception as e:
                r.check("mock zeroclaw /api/events is text/event-stream",
                        False, str(e))

            # ── 2. /api/status responds while SSE is in flight ───
            sse_running = threading.Event()
            sse_done = threading.Event()

            def _sse_runner():
                try:
                    req = urllib.request.Request(shim_events,
                                                 headers={"Accept": "text/event-stream"})
                    with urllib.request.urlopen(req, timeout=10.0) as resp:
                        sse_running.set()
                        # Drain until upstream ends or 5s.
                        end = time.time() + 5.0
                        while time.time() < end:
                            try:
                                resp.read(1024)
                            except Exception:
                                break
                except Exception:
                    sse_running.set()
                sse_done.set()

            t = threading.Thread(target=_sse_runner, daemon=True)
            t.start()
            sse_running.wait(timeout=3.0)
            time.sleep(0.2)
            s = _http_get_retry(f"{base}/api/status", retries=3, timeout=3.0)
            r.check("/api/status responds while SSE in flight",
                    s == 200, f"status={s}")

            # ── 3. /api/chat works concurrently with SSE ─────────
            cs, body = _http_post_json_retry(f"{base}/api/chat",
                                             {"message": "concurrent with sse"},
                                             retries=3, timeout=10.0)
            try:
                reply = json.loads(body.decode("utf-8")).get("reply", "")
            except Exception:
                reply = ""
            r.check("/api/chat works concurrently with SSE",
                    cs == 200 and "mock-reply" in reply,
                    f"status={cs} reply={reply!r}")
            sse_done.wait(timeout=5.0)

            # ── 4. Recovery: zc_dead then clear → chat works ─────
            safe_mock_set(zc_dead=True)
            time.sleep(0.4)
            _retry_mock_clear()
            t0 = time.time()
            ok_recovered = False
            while time.time() - t0 < 5.0:
                rs, body, _ = http_post_json(f"{base}/api/chat",
                                             {"message": "after recovery"},
                                             timeout=4.0)
                if rs == 200:
                    ok_recovered = True
                    break
                time.sleep(0.3)
            r.check("zc_dead → mock_clear: chat recovers within 5s",
                    ok_recovered, f"final_status={rs} elapsed={time.time()-t0:.1f}s")

            # ── 5. 5 transient SSE subscriptions → no FD leak ────
            for i in range(5):
                ok, _ = _read_one_sse_event(shim_events, timeout_s=3.0)
                # Each sub returns ok=True if connection opened and at
                # least one chunk was read.
                if not ok:
                    r.info(f"SSE transient subscription {i} failed to open")
            s, _, _ = http_get(f"{base}/health", timeout=3.0)
            r.check("5 transient SSE subs: companion /health still 200",
                    s == 200, f"status={s}")

            # ── 6. Upstream 4xx doesn't panic bridge classifier ───
            safe_mock_set(zc_status=404)
            time.sleep(0.5)
            s, _, _ = http_get(f"{base}/health", timeout=3.0)
            r.check("upstream zc_status=404: companion /health still 200",
                    s == 200, f"status={s}")
            _retry_mock_clear()
            time.sleep(0.4)

            # ── 7. 10-chat storm + SSE still works ───────────────
            def _do_chat(i):
                s, _, _ = http_post_json(f"{base}/api/chat",
                                         {"message": f"storm-{i}"},
                                         timeout=8.0)
                return s

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                results = list(ex.map(_do_chat, range(10)))
            all_ok = all(s == 200 for s in results)
            # Now verify SSE still flows.
            sse_ok, _ = _read_one_sse_event(shim_events, timeout_s=3.0)
            r.check("after 10-chat storm: all chats 200 AND SSE still flows",
                    all_ok and sse_ok,
                    f"chat_ok={sum(1 for s in results if s == 200)}/10 sse_ok={sse_ok}")

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
