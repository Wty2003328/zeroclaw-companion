"""L3 — TTS wire-contract rig.

Asserts the OpenAI-compatible TTS Provider Spec v1 contract that lives in
the docstring of `crates/companion-avatar/src/tts_server.rs`. Runs against
the mock TTS in `_mock_stack` (NOT the real qwen3 sidecar — that's too
heavy for an L3 wire test; spec compliance is the same either way).

Checks (every PASS line maps to a clause in the spec docstring):

  * `GET  /healthz`            → 200 + `{"status":"ok",...}`
  * `GET  /health`             → 200 (legacy alias accepted during transition)
  * `POST /v1/audio/speech`    → 200, WAV bytes, X-Sample-Rate/Channels/Format headers
  * `POST /tts`                → 200 (legacy alias)
  * Empty input                → mock floors at 0.05 s of audio (still a valid WAV)
  * `mock_set(tts_dead=True)`  → /healthz returns 5xx
  * `mock_set(tts_dead=False)` → /healthz returns 200 again
  * `POST /shutdown`           → 200, process exits within 5 s (port goes free)

Run directly:
    python -m tts_tools.test_server_e2e
"""
from __future__ import annotations

import time

from tts_tools._test_helpers import (
    CheckReporter,
    PORT_CONTROL,
    PORT_MOCK_ZEROCLAW,
    PORT_NMT,
    PORT_TTS,
    http_get,
    http_post_json,
    is_port_free,
    managed_procs,
    mock_clear,
    mock_set,
    python_exe,
    require_ports_free,
    spawn,
    wait_for_port,
    wait_for_url,
)


def _wait_for_port_free(port: int, timeout_s: float = 8.0) -> bool:
    """Poll until 127.0.0.1:port is NOT bound. Inverse of helpers.wait_for_port."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_port_free(port):
            return True
        time.sleep(0.2)
    return False


def main() -> None:
    r = CheckReporter("test_server_e2e")

    # Never run against a stale stack.
    require_ports_free(PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW)

    with managed_procs() as procs:
        mock = spawn(
            "mock-stack",
            [python_exe(), "-m", "tts_tools._mock_stack"],
            port=PORT_CONTROL,
        )
        procs.append(mock)
        # Wait for control plane — last thread to come up is a reasonable
        # readiness signal that all four mock apps are bound.
        if not r.check(
            "mock stack control plane bound",
            wait_for_url(f"http://127.0.0.1:{PORT_CONTROL}/_state", timeout_s=15.0),
        ):
            r.summary_or_exit()
        # Also wait for the TTS port specifically — different uvicorn instance.
        if not r.check(
            "mock TTS port bound",
            wait_for_port(PORT_TTS, timeout_s=10.0),
        ):
            r.summary_or_exit()

        base = f"http://127.0.0.1:{PORT_TTS}"

        # --- /healthz (canonical) -------------------------------------------
        status, body, _ = http_get(f"{base}/healthz", timeout=5.0)
        ok = status == 200
        detail = f"status={status}"
        if ok:
            try:
                import json
                j = json.loads(body.decode("utf-8"))
                ok = j.get("status") == "ok"
                detail = f"body={j}"
            except Exception as e:  # noqa: BLE001
                ok = False
                detail = f"non-JSON body: {e}"
        r.check("GET /healthz returns 200 + status:ok", ok, detail)

        # --- /health (legacy alias) -----------------------------------------
        status, _, _ = http_get(f"{base}/health", timeout=5.0)
        r.check("GET /health (legacy alias) returns 200", status == 200, f"status={status}")

        # --- POST /v1/audio/speech (canonical) ------------------------------
        status, body, headers = http_post_json(
            f"{base}/v1/audio/speech",
            {
                "input": "Hello there, how are you doing today?",
                "voice": "asuna",
                "speed": 1.0,
                "response_format": "wav",
                "x_companion": {"language": "en", "quality": "balanced"},
            },
            timeout=10.0,
        )
        r.check(
            "POST /v1/audio/speech returns 200",
            status == 200,
            f"status={status} body_len={len(body)}",
        )
        # WAV magic — first 4 bytes "RIFF", bytes 8-12 "WAVE".
        is_wav = len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WAVE"
        r.check(
            "POST /v1/audio/speech body is a WAV file",
            is_wav,
            f"first12={body[:12]!r}",
        )
        # Case-insensitive header lookup — urllib normalizes mixed case.
        hdr = {k.lower(): v for k, v in headers.items()}
        r.check(
            "X-Sample-Rate header present",
            "x-sample-rate" in hdr,
            f"value={hdr.get('x-sample-rate')!r}",
        )
        r.check(
            "X-Channels header present",
            "x-channels" in hdr,
            f"value={hdr.get('x-channels')!r}",
        )
        r.check(
            "X-Format header present",
            "x-format" in hdr,
            f"value={hdr.get('x-format')!r}",
        )

        # --- POST /tts (legacy alias) ---------------------------------------
        status, body, _ = http_post_json(
            f"{base}/tts",
            {"input": "legacy path", "voice": "asuna"},
            timeout=10.0,
        )
        r.check(
            "POST /tts (legacy alias) returns 200 + WAV",
            status == 200
            and len(body) >= 12
            and body[:4] == b"RIFF"
            and body[8:12] == b"WAVE",
            f"status={status} body_len={len(body)}",
        )

        # --- Empty input → mock floors at 0.05 s ----------------------------
        status, body, _ = http_post_json(
            f"{base}/v1/audio/speech",
            {"input": "", "voice": "asuna"},
            timeout=5.0,
        )
        # WAV header is 44 bytes. 0.05 s × 24000 Hz × 2 bytes/sample = 2400
        # data bytes. So a minimal valid response is ≥ 44 bytes, and the
        # mock's floor pushes it well above that.
        r.check(
            "empty input still produces a minimum-length WAV",
            status == 200 and len(body) > 44,
            f"status={status} body_len={len(body)}",
        )

        # --- Failure injection: tts_dead → /healthz 5xx ---------------------
        r.check("mock_set(tts_dead=True) accepted", mock_set(tts_dead=True))
        status, _, _ = http_get(f"{base}/healthz", timeout=3.0)
        r.check(
            "tts_dead=True → /healthz returns 5xx",
            500 <= status < 600,
            f"status={status}",
        )

        r.check("mock_clear() accepted", mock_clear())
        status, _, _ = http_get(f"{base}/healthz", timeout=3.0)
        r.check(
            "after mock_clear → /healthz returns 200 again",
            status == 200,
            f"status={status}",
        )

        # --- POST /shutdown → 200, process exits ----------------------------
        status, _, _ = http_post_json(f"{base}/shutdown", {}, timeout=3.0)
        r.check(
            "POST /shutdown returns 200",
            status == 200,
            f"status={status}",
        )
        # _delayed_exit calls os._exit(0) — every thread (NMT, ZC, CTRL)
        # comes down with TTS. Wait for the TTS port to go free.
        r.check(
            "TTS port released within 5 s after /shutdown",
            _wait_for_port_free(PORT_TTS, timeout_s=5.0),
        )

    r.summary_or_exit()


if __name__ == "__main__":
    main()
