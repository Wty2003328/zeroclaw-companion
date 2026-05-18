"""L6f-prop — Property-based fuzz / random walker.

Drives a random walk through the companion-server's REST + WS surface,
mixing chat / ASR / config / characters / pulse / status calls. Watches
for:

  * /health staying 200 throughout (poll every 20 ops),
  * no 5xx from server-side code (sidecar 502/503 ARE allowed — that's
    a documented gateway error path, not a server bug),
  * 20 WS connect/disconnect cycles → 20 distinct session_ids
    (no reuse, no leak),
  * 15 concurrent WS subscribers all see the UserMessage broadcast
    when one chat fires.

Use a seed for deterministic reproduction:
    python -m tts_tools.test_state_transitions
    python -m tts_tools.test_state_transitions --ops 500 --seed 42
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import random
import string
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import websocket

from tts_tools._test_helpers import (
    CheckReporter,
    PORT_COMPANION,
    PORT_CONTROL,
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
from tts_tools._rig_shim import PORT_ZC_SHIM, start_shims, wait_for_shims


def _build_config() -> dict:
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            # Rig shim bridges /webhook → mock's /api/chat.
            "url": f"http://127.0.0.1:{PORT_ZC_SHIM}",
            "timeout_secs": 15,
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
                "streaming": False,
                "streaming_target_chars": 80,
            },
            "subagent": {
                "enabled": False,
                "only_when_translating": True,
            },
            "speech": {"enabled": False},
        },
        "pulse": {"enabled": True},
        "pulse.database": {
            "path": "./pulse_fuzz.db",
            "retention_days": 30,
        },
        "pulse.collectors.rss": {"enabled": False, "interval": "30m"},
        "pulse.collectors.hackernews": {"enabled": False, "interval": "15m"},
    }


def _tiny_wav_b64() -> str:
    buf = io.BytesIO()
    n = int(0.1 * 16000)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * n)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _random_text(rng: random.Random, lo: int = 10, hi: int = 200) -> str:
    n = rng.randint(lo, hi)
    alphabet = string.ascii_letters + string.digits + "         .,!?'\""
    return "".join(rng.choice(alphabet) for _ in range(n))


def _random_lang(rng: random.Random) -> str:
    return rng.choice(["en", "ja", "zh", "es", "fr", "de", "ko"])


def _http_delete(url: str, timeout: float = 5.0) -> int:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ── Per-op handlers ─────────────────────────────────────────────────
# Each returns (op_label, server_5xx_observed)
# server_5xx means the SERVER itself (not the gateway) errored. We treat
# 502/503 as gateway errors (mock fault, not companion's fault).

def _op_chat(rng: random.Random, base: str) -> tuple[str, bool]:
    status, _, _ = http_post_json(
        f"{base}/api/chat",
        {"message": _random_text(rng)},
        timeout=15.0,
    )
    server_5xx = status in (500, 501, 504)
    return ("chat", server_5xx)


def _op_asr(rng: random.Random, base: str) -> tuple[str, bool]:
    status, _, _ = http_post_json(
        f"{base}/api/avatar/asr",
        {"audio": _tiny_wav_b64()},
        timeout=10.0,
    )
    # 503 = speech disabled (known); not a server bug.
    server_5xx = status in (500, 501, 504)
    return ("asr", server_5xx)


def _op_config_avatar(rng: random.Random, base: str) -> tuple[str, bool]:
    speed = rng.uniform(0.1, 5.0)
    status, _, _ = http_post_json(
        f"{base}/api/config/avatar",
        {"tts_speed": speed},
        timeout=5.0,
    )
    server_5xx = status in (500, 501, 504)
    return (f"config_avatar(speed={speed:.2f})", server_5xx)


def _op_config_subagent(rng: random.Random, base: str) -> tuple[str, bool]:
    status, _, _ = http_post_json(
        f"{base}/api/config/subagent",
        {"translator_nmt_tgt_lang": _random_lang(rng)},
        timeout=5.0,
    )
    server_5xx = status in (500, 501, 504)
    return ("config_subagent", server_5xx)


def _op_characters(rng: random.Random, base: str) -> tuple[str, bool]:
    char_id = "fuzz-" + "".join(rng.choices(string.ascii_lowercase, k=8))
    status, _, _ = http_post_json(
        f"{base}/api/characters",
        {
            "id": char_id,
            "name": _random_text(rng, 5, 30),
            "model_id": "",
            "system_prompt": _random_text(rng, 10, 100),
            "notes": "",
        },
        timeout=5.0,
    )
    server_5xx = status in (500, 501, 504)
    # Best-effort cleanup so the roster doesn't balloon.
    _http_delete(f"{base}/api/characters/{char_id}")
    return ("characters_crud", server_5xx)


def _op_pulse_feeds(rng: random.Random, base: str) -> tuple[str, bool]:
    feed_name = "fuzz-feed-" + "".join(rng.choices(string.ascii_lowercase, k=6))
    feed_url = f"https://example.invalid/{feed_name}.rss"
    status, _, _ = http_post_json(
        f"{base}/api/pulse/feeds",
        {"name": feed_name, "url": feed_url},
        timeout=5.0,
    )
    server_5xx = status in (500, 501, 504)
    # DELETE /api/pulse/feeds?url=...
    q = urllib.parse.urlencode({"url": feed_url})
    _http_delete(f"{base}/api/pulse/feeds?{q}")
    return ("pulse_feeds_crud", server_5xx)


def _op_status(_rng: random.Random, base: str) -> tuple[str, bool]:
    status, _, _ = http_get(f"{base}/api/status", timeout=5.0)
    return ("status", status in (500, 501, 504))


def _op_health(_rng: random.Random, base: str) -> tuple[str, bool]:
    status, _, _ = http_get(f"{base}/health", timeout=5.0)
    return ("health", status in (500, 501, 504))


def _op_get_config(_rng: random.Random, base: str) -> tuple[str, bool]:
    status, _, _ = http_get(f"{base}/api/config", timeout=5.0)
    return ("get_config", status in (500, 501, 504))


def _op_ws_quick(_rng: random.Random, base: str) -> tuple[str, bool]:
    """Open WS, receive one frame, close. Treat any connection-level
    failure as a 5xx-class problem since the WS path is server-owned.
    """
    url = f"ws://127.0.0.1:{PORT_COMPANION}/ws/avatar"
    try:
        ws = websocket.create_connection(url, timeout=5.0)
        try:
            ws.settimeout(3.0)
            try:
                ws.recv()
            except Exception:
                pass
        finally:
            ws.close()
        return ("ws_quick", False)
    except Exception:
        return ("ws_quick_failed", True)


ALL_OPS = [
    _op_chat,
    _op_asr,
    _op_config_avatar,
    _op_config_subagent,
    _op_characters,
    _op_pulse_feeds,
    _op_status,
    _op_health,
    _op_get_config,
    _op_ws_quick,
]


# ── Special checks ──────────────────────────────────────────────────
def _check_session_ids_distinct(r: CheckReporter, n: int = 20) -> None:
    """Open `n` WS connections sequentially; assert every session_id is
    unique. Server allocates a fresh uuid::Uuid::new_v4() per connect
    (see crates/companion-avatar/src/ws.rs:339), so reuse would be a real
    leak / cache bug.
    """
    url = f"ws://127.0.0.1:{PORT_COMPANION}/ws/avatar"
    ids: list[str] = []
    for _ in range(n):
        try:
            ws = websocket.create_connection(url, timeout=5.0)
            ws.settimeout(5.0)
            # First frame is Connected with session_id.
            got_sid: Optional[str] = None
            for _ in range(3):
                try:
                    msg = ws.recv()
                except Exception:
                    break
                try:
                    obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
                except Exception:
                    continue
                if obj.get("type") == "Connected" and obj.get("session_id"):
                    got_sid = obj["session_id"]
                    break
            try:
                ws.close()
            except Exception:
                pass
            if got_sid:
                ids.append(got_sid)
        except Exception:
            pass
    unique = len(set(ids))
    r.check(
        f"{n} WS connects → {n} distinct session_ids",
        unique == n,
        f"got={len(ids)} ids, {unique} unique",
    )


def _check_concurrent_broadcast(r: CheckReporter, base: str, n_subscribers: int = 15) -> None:
    """Open `n` WS clients in parallel, fire one /api/chat, assert every
    client receives the UserMessage broadcast.
    """
    url = f"ws://127.0.0.1:{PORT_COMPANION}/ws/avatar"
    ready = threading.Barrier(n_subscribers + 1)
    received: list[bool] = [False] * n_subscribers
    needle = "broadcast-needle-" + "".join(random.choices(string.ascii_lowercase, k=8))

    def _subscriber(idx: int):
        try:
            ws = websocket.create_connection(url, timeout=10.0)
            ws.settimeout(10.0)
            # Drain ModelInfo first so we're past the boot frames.
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
            ready.wait(timeout=15.0)
            # Now listen for the UserMessage echo with our needle.
            deadline = time.time() + 15.0
            while time.time() < deadline:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                try:
                    obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
                except Exception:
                    continue
                if obj.get("type") == "UserMessage" and needle in (obj.get("content") or ""):
                    received[idx] = True
                    break
            try:
                ws.close()
            except Exception:
                pass
        except Exception:
            try:
                ready.wait(timeout=1.0)
            except Exception:
                pass

    pool = ThreadPoolExecutor(max_workers=n_subscribers)
    futures = [pool.submit(_subscriber, i) for i in range(n_subscribers)]
    # Give subscribers a moment to attach + drain their boot frames,
    # then release them at the barrier.
    time.sleep(2.0)
    try:
        ready.wait(timeout=15.0)
    except threading.BrokenBarrierError:
        pass
    # Fire one chat — UserMessage echoes to every connected subscriber.
    http_post_json(
        f"{base}/api/chat",
        {"message": needle},
        timeout=15.0,
    )
    # Wait for subscribers to finish (they each have their own 15s ceiling).
    for f in as_completed(futures, timeout=30):
        try:
            f.result()
        except Exception:
            pass
    pool.shutdown(wait=False)
    got = sum(1 for ok in received if ok)
    r.check(
        f"{n_subscribers} concurrent WS subscribers see UserMessage broadcast",
        got == n_subscribers,
        f"got={got}/{n_subscribers}",
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="State-transition fuzz rig.")
    p.add_argument("--ops", type=int, default=200, help="Number of random ops (default 200).")
    p.add_argument("--seed", type=int, default=-1, help="Random seed (default: random).")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.seed < 0:
        seed = random.SystemRandom().randint(0, 2**31 - 1)
    else:
        seed = args.seed
    rng = random.Random(seed)

    r = CheckReporter("test_state_transitions")
    r.info(f"ops={args.ops} seed={seed}")

    require_ports_free(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        PORT_ZC_SHIM,
    )

    scratch = Path(tempfile.mkdtemp(prefix="companion-fuzz-"))
    log_dir = scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    with managed_procs() as procs:
        mock = spawn(
            "mock-stack",
            [python_exe(), "-m", "tts_tools._mock_stack"],
            port=PORT_CONTROL,
            cwd=REPO_ROOT,
            log_path=log_dir / "mock-stack.log",
        )
        procs.append(mock)
        if not wait_for_url(f"http://127.0.0.1:{PORT_CONTROL}/_state", timeout_s=20.0):
            r.check("mock stack control plane bound", False)
            r.summary_or_exit()
        for p in (PORT_TTS, PORT_NMT, PORT_MOCK_ZEROCLAW):
            wait_for_port(p, timeout_s=10.0)
        # Rig-shim: /webhook adapter so /api/chat round-trips.
        start_shims()
        if not wait_for_shims(timeout_s=5.0):
            r.check("rig shims bound", False)
            r.summary_or_exit()

        comp, _ = spawn_companion_server(
            _build_config(), port=PORT_COMPANION, log_dir=log_dir,
            scratch_dir=scratch,
        )
        procs.append(comp)
        if not wait_for_url(f"http://127.0.0.1:{PORT_COMPANION}/health", timeout_s=30.0):
            r.check("companion /health came up", False)
            r.summary_or_exit()

        base = f"http://127.0.0.1:{PORT_COMPANION}"

        # ── Random walk ─────────────────────────────────────────
        op_5xx_count = 0
        health_failures = 0
        for i in range(args.ops):
            op = rng.choice(ALL_OPS)
            try:
                _label, server_5xx = op(rng, base)
            except Exception:
                server_5xx = False
            if server_5xx:
                op_5xx_count += 1

            # Every 20 ops, probe /health. Never let it 5xx.
            if (i + 1) % 20 == 0:
                status, _, _ = http_get(f"{base}/health", timeout=5.0)
                if status != 200:
                    health_failures += 1

        r.check(
            "no server-side 5xx during random walk",
            op_5xx_count == 0,
            f"{op_5xx_count} ops returned 500/501/504",
        )
        r.check(
            "/health returns 200 throughout the walk",
            health_failures == 0,
            f"{health_failures} health probes failed",
        )

        # ── Targeted checks ────────────────────────────────────
        _check_session_ids_distinct(r, n=20)
        _check_concurrent_broadcast(r, base, n_subscribers=15)

        # ── Post-walk smoke: server still alive ────────────────
        status, _, _ = http_get(f"{base}/health", timeout=5.0)
        r.check("post-walk /health = 200", status == 200, f"status={status}")
        st_status, _, _ = http_get(f"{base}/api/status", timeout=5.0)
        r.check("post-walk /api/status = 200", st_status == 200, f"status={st_status}")

    r.summary_or_exit()


if __name__ == "__main__":
    main()
