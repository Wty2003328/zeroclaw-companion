"""L3 — NMT wire-contract rig.

Asserts the translation-sidecar wire contract expected by
`crates/companion-avatar/src/translator.rs`. Runs against the NMT mock in
`_mock_stack`.

Checks:

  * `GET  /healthz`           → 200
  * `POST /translate` happy   → 200 + `{"translated": "...", source_lang, target_lang}`
  * Missing `source_lang`     → 400
  * Missing `target_lang`     → 400
  * `mock_set(nmt_slow_s=1.0)`→ /translate elapsed ≥ 0.9 s
  * `mock_set(nmt_dead=True)` → /translate returns 5xx
  * `mock_clear()` restores
  * `POST /shutdown`          → 200, port goes free within 5 s

Run directly:
    python -m tts_tools.test_nmt_e2e
"""
from __future__ import annotations

import json
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
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_port_free(port):
            return True
        time.sleep(0.2)
    return False


def main() -> None:
    r = CheckReporter("test_nmt_e2e")
    require_ports_free(PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW)

    with managed_procs() as procs:
        mock = spawn(
            "mock-stack",
            [python_exe(), "-m", "tts_tools._mock_stack"],
            port=PORT_CONTROL,
        )
        procs.append(mock)

        if not r.check(
            "mock stack control plane bound",
            wait_for_url(f"http://127.0.0.1:{PORT_CONTROL}/_state", timeout_s=15.0),
        ):
            r.summary_or_exit()
        if not r.check(
            "mock NMT port bound",
            wait_for_port(PORT_NMT, timeout_s=10.0),
        ):
            r.summary_or_exit()

        base = f"http://127.0.0.1:{PORT_NMT}"

        # --- /healthz --------------------------------------------------------
        status, body, _ = http_get(f"{base}/healthz", timeout=5.0)
        ok = status == 200
        detail = f"status={status}"
        if ok:
            try:
                j = json.loads(body.decode("utf-8"))
                ok = j.get("status") == "ok"
                detail = f"body={j}"
            except Exception as e:  # noqa: BLE001
                ok = False
                detail = f"non-JSON: {e}"
        r.check("GET /healthz returns 200 + status:ok", ok, detail)

        # --- happy translate -------------------------------------------------
        status, body, _ = http_post_json(
            f"{base}/translate",
            {"text": "hello", "source_lang": "en", "target_lang": "ja"},
            timeout=5.0,
        )
        ok = status == 200
        translated = None
        if ok:
            try:
                j = json.loads(body.decode("utf-8"))
                translated = j.get("translated")
                ok = translated == "[ja] hello"
            except Exception as e:  # noqa: BLE001
                ok = False
                translated = f"non-JSON: {e}"
        r.check(
            "POST /translate happy path returns {translated: '[ja] hello'}",
            ok,
            f"status={status} translated={translated!r}",
        )

        # --- missing source_lang → 400 --------------------------------------
        status, _, _ = http_post_json(
            f"{base}/translate",
            {"text": "hello", "target_lang": "ja"},
            timeout=5.0,
        )
        r.check(
            "missing source_lang → 400",
            status == 400,
            f"status={status}",
        )

        # --- missing target_lang → 400 --------------------------------------
        status, _, _ = http_post_json(
            f"{base}/translate",
            {"text": "hello", "source_lang": "en"},
            timeout=5.0,
        )
        r.check(
            "missing target_lang → 400",
            status == 400,
            f"status={status}",
        )

        # --- slow knob → elapsed bound --------------------------------------
        r.check("mock_set(nmt_slow_s=1.0) accepted", mock_set(nmt_slow_s=1.0))
        t0 = time.monotonic()
        status, _, _ = http_post_json(
            f"{base}/translate",
            {"text": "hello", "source_lang": "en", "target_lang": "ja"},
            timeout=5.0,
        )
        elapsed = time.monotonic() - t0
        r.check(
            "nmt_slow_s=1.0 → translate elapsed ≥ 0.9 s",
            status == 200 and elapsed >= 0.9,
            f"status={status} elapsed={elapsed:.2f}s",
        )
        r.check("mock_clear() after slow accepted", mock_clear())

        # --- dead knob → 5xx ------------------------------------------------
        r.check("mock_set(nmt_dead=True) accepted", mock_set(nmt_dead=True))
        status, _, _ = http_post_json(
            f"{base}/translate",
            {"text": "hello", "source_lang": "en", "target_lang": "ja"},
            timeout=3.0,
        )
        r.check(
            "nmt_dead=True → /translate returns 5xx",
            500 <= status < 600,
            f"status={status}",
        )

        # Also assert /healthz comes down with the dead knob.
        status, _, _ = http_get(f"{base}/healthz", timeout=3.0)
        r.check(
            "nmt_dead=True → /healthz returns 5xx",
            500 <= status < 600,
            f"status={status}",
        )

        r.check("mock_clear() restores accepted", mock_clear())
        status, _, _ = http_get(f"{base}/healthz", timeout=3.0)
        r.check(
            "after mock_clear → /healthz returns 200 again",
            status == 200,
            f"status={status}",
        )

        # --- /shutdown — kills the whole mock-stack process -----------------
        status, _, _ = http_post_json(f"{base}/shutdown", {}, timeout=3.0)
        r.check(
            "POST /shutdown returns 200",
            status == 200,
            f"status={status}",
        )
        r.check(
            "NMT port released within 5 s after /shutdown",
            _wait_for_port_free(PORT_NMT, timeout_s=5.0),
        )

    r.summary_or_exit()


if __name__ == "__main__":
    main()
