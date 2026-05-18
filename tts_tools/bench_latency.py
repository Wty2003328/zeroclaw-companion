"""L6h — Latency benchmarks with P2 SLA gates.

Spawns the mock stack + companion-server, then measures the latency of
every load-bearing endpoint (chat, ASR, status, WS handshake, WS time-to-
first-audio). Writes a CSV under tts_samples/bench/<timestamp>/ so the
L6perf regression rig has a history to compare against.

Hard SLA gates fire on the metrics that drive user-visible UX. Other
metrics are advisory only — printed via r.info but not failed-on.

Run directly:
    python -m tts_tools.bench_latency
    COMPANION_BENCH_NO_SLA=1 python -m tts_tools.bench_latency   # baseline
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import statistics
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

import websocket  # websocket-client; installed in the tts env

from tts_tools._test_helpers import (
    CheckReporter,
    PORT_COMPANION,
    PORT_CONTROL,
    PORT_MOCK_ZEROCLAW,
    PORT_NMT,
    PORT_TTS,
    REPO_ROOT,
    http_get,
    http_json,
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


# ── SLA contract ────────────────────────────────────────────────────
# Every metric here is a HARD gate. Exceeding it fails the rig unless
# COMPANION_BENCH_NO_SLA=1 is set (for the first-baseline run on a new
# machine where the local baseline is unknown).
SLA_MS: dict[str, float] = {
    "ws_ttfp_ms":             3000.0,
    "chat_p95_ms":            4000.0,
    "chat_p50_ms":            2000.0,
    "cold_companion_start_ms": 5000.0,
    "status_p95_ms":           500.0,
    "ws_handshake_ms":         2000.0,
    "asr_p95_ms":              3000.0,
}


def _build_config() -> dict:
    """Companion config wired against the mock stack. Avatar enabled so
    /ws/avatar + /api/avatar/asr exist; pulse off (not bench-relevant)."""
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            # Rig shim bridges /webhook → mock's /api/chat (mock has no
            # /webhook of its own). Point companion at the shim.
            "url": f"http://127.0.0.1:{PORT_ZC_SHIM}",
            "timeout_secs": 30,
        },
        "server": {
            "host": "127.0.0.1",
            "port": PORT_COMPANION,
        },
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
                # Streaming off — we time the FULL synthesis to first
                # audio frame; single-shot is the load-bearing path.
                "streaming": False,
                "streaming_target_chars": 80,
            },
            "subagent": {
                "enabled": False,
                "only_when_translating": True,
            },
            "speech": {
                # Speech off — we send a synthetic base64 payload and
                # let companion-server reject at the ASR proxy layer.
                # /api/avatar/asr is the load-bearing path; we measure
                # its handler latency, not the mock sidecar response.
                "enabled": False,
            },
        },
        "pulse": {"enabled": False},
    }


def _tiny_wav_b64(duration_s: float = 0.1, sample_rate: int = 16_000) -> str:
    """Build a tiny silence WAV and return base64. Companion's ASR proxy
    rejects empty payloads, so we feed it something well-formed even
    though [avatar.speech].enabled = false will short-circuit at 503."""
    buf = io.BytesIO()
    n = int(duration_s * sample_rate)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _percentile(values: list[float], q: float) -> float:
    """Approximate percentile via linear interpolation. statistics.quantiles
    won't give us p95 on a 30-sample list cleanly; do it ourselves."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * q
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ── Measurement primitives ──────────────────────────────────────────
def _measure_chat(base: str, n: int = 30) -> list[float]:
    samples: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        status, _, _ = http_post_json(
            f"{base}/api/chat", {"message": f"ping {i}"}, timeout=30.0,
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
        if status == 200:
            samples.append(elapsed)
    return samples


def _measure_asr(base: str, n: int = 20) -> list[float]:
    """ASR returns 503 (speech disabled) — we want to time the handler
    proxy path, which is the latency the UI sees regardless of result."""
    payload = _tiny_wav_b64()
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        # Any non-5xx-from-companion response counts: 503 from the
        # disabled-speech branch is a fast-path the user actually hits.
        http_post_json(f"{base}/api/avatar/asr", {"audio": payload}, timeout=10.0)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _measure_status(base: str, n: int = 30) -> list[float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        http_get(f"{base}/api/status", timeout=5.0)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _ws_url() -> str:
    return f"ws://127.0.0.1:{PORT_COMPANION}/ws/avatar"


def _measure_ws_handshake(n: int = 10) -> list[float]:
    """Open WS, wait for first ModelInfo frame, close. We accept
    Connected → ModelInfo (server sends both) and time-to-ModelInfo is
    what the frontend renders on."""
    samples: list[float] = []
    url = _ws_url()
    for _ in range(n):
        t0 = time.perf_counter()
        ws = websocket.create_connection(url, timeout=10.0)
        got_model_info = False
        try:
            for _ in range(5):
                msg = ws.recv()
                try:
                    obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
                except Exception:
                    continue
                if obj.get("type") == "ModelInfo":
                    got_model_info = True
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if got_model_info:
            samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _measure_ws_ttfp(base: str, n: int = 10) -> list[float]:
    """Time-to-first-Audio-frame: open WS, drain ModelInfo, send /api/chat,
    record elapsed when the first Audio frame arrives on the WS."""
    samples: list[float] = []
    url = _ws_url()
    for i in range(n):
        ws = websocket.create_connection(url, timeout=10.0)
        ws.settimeout(15.0)
        try:
            # Drain Connected + ModelInfo first.
            drained = 0
            while drained < 4:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                try:
                    obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
                except Exception:
                    continue
                if obj.get("type") in ("Connected", "ModelInfo"):
                    drained += 1
                if obj.get("type") == "ModelInfo":
                    break
            # Fire chat; spawn it on a thread is overkill — http_post_json
            # blocks until reply, but the WS Audio frame fires in
            # parallel from the server-side fan-out. Start timer just
            # before the POST so we capture network+server time.
            t0 = time.perf_counter()
            import threading
            done = threading.Event()
            def _post():
                http_post_json(
                    f"{base}/api/chat",
                    {"message": f"hello number {i}"},
                    timeout=30.0,
                )
                done.set()
            t = threading.Thread(target=_post, daemon=True)
            t.start()
            # Listen for the first Audio frame.
            got_audio_ms = None
            deadline = time.perf_counter() + 30.0
            while time.perf_counter() < deadline:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                try:
                    obj = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode())
                except Exception:
                    continue
                if obj.get("type") == "Audio":
                    got_audio_ms = (time.perf_counter() - t0) * 1000.0
                    break
            done.wait(timeout=10.0)
            if got_audio_ms is not None:
                samples.append(got_audio_ms)
        finally:
            try:
                ws.close()
            except Exception:
                pass
    return samples


# ── Public bench entrypoint — usable as a library ───────────────────
def run_bench(r: CheckReporter, base: str) -> dict[str, float]:
    """Run every measurement; return a flat metric→value_ms dict.

    Exported so test_perf_regression can call this directly without
    re-spawning the stack (it spawns its own and passes `base`).
    """
    metrics: dict[str, float] = {}

    chat_samples = _measure_chat(base, n=30)
    if chat_samples:
        metrics["chat_p50_ms"] = _percentile(chat_samples, 0.50)
        metrics["chat_p95_ms"] = _percentile(chat_samples, 0.95)
    else:
        r.info("chat: 0 successful samples — cannot compute p50/p95")

    asr_samples = _measure_asr(base, n=20)
    if asr_samples:
        metrics["asr_p50_ms"] = _percentile(asr_samples, 0.50)
        metrics["asr_p95_ms"] = _percentile(asr_samples, 0.95)

    status_samples = _measure_status(base, n=30)
    if status_samples:
        metrics["status_p50_ms"] = _percentile(status_samples, 0.50)
        metrics["status_p95_ms"] = _percentile(status_samples, 0.95)

    handshake_samples = _measure_ws_handshake(n=10)
    if handshake_samples:
        metrics["ws_handshake_ms"] = statistics.median(handshake_samples)
    else:
        r.info("ws_handshake: 0 successful samples")

    ttfp_samples = _measure_ws_ttfp(base, n=10)
    if ttfp_samples:
        metrics["ws_ttfp_ms"] = statistics.median(ttfp_samples)
    else:
        r.info("ws_ttfp: 0 successful samples")

    return metrics


def _write_csv(metrics: dict[str, float], timestamp: str) -> Path:
    out_dir = REPO_ROOT / "tts_samples" / "bench" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "results.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "metric", "value_ms"])
        for metric, value in sorted(metrics.items()):
            w.writerow([timestamp, metric, f"{value:.2f}"])
    return csv_path


def main() -> None:
    r = CheckReporter("bench_latency")
    require_ports_free(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        PORT_ZC_SHIM,
    )

    no_sla = os.environ.get("COMPANION_BENCH_NO_SLA") == "1"
    if no_sla:
        r.info("COMPANION_BENCH_NO_SLA=1 — SLA gates downgraded to advisory")

    scratch = Path(tempfile.mkdtemp(prefix="companion-bench-"))
    log_dir = scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    metrics: dict[str, float] = {}

    with managed_procs() as procs:
        # ── Mocks ───────────────────────────────────────────────
        mocks_t0 = time.perf_counter()
        mock = spawn(
            "mock-stack",
            [python_exe(), "-m", "tts_tools._mock_stack"],
            port=PORT_CONTROL,
            cwd=REPO_ROOT,
            log_path=log_dir / "mock-stack.log",
        )
        procs.append(mock)
        if not wait_for_url(f"http://127.0.0.1:{PORT_CONTROL}/_state", timeout_s=20.0):
            r.check("mock stack control plane bound", False, "control plane timeout")
            r.summary_or_exit()
        # Wait for all four mock ports — companion probes them at startup.
        for p in (PORT_TTS, PORT_NMT, PORT_MOCK_ZEROCLAW):
            wait_for_port(p, timeout_s=10.0)
        # Rig-shim: /webhook adapter so /api/chat actually round-trips.
        # In-process threaded HTTP server; no subprocess to clean up.
        start_shims()
        if not wait_for_shims(timeout_s=5.0):
            r.check("rig shims bound", False)
            r.summary_or_exit()
        mocks_elapsed_ms = (time.perf_counter() - mocks_t0) * 1000.0
        metrics["cold_mocks_start_ms"] = mocks_elapsed_ms
        r.info(f"cold_mocks_start_ms = {mocks_elapsed_ms:.0f}ms")

        # ── Companion ───────────────────────────────────────────
        comp_t0 = time.perf_counter()
        comp, _ = spawn_companion_server(
            _build_config(), port=PORT_COMPANION, log_dir=log_dir,
            scratch_dir=scratch,
        )
        procs.append(comp)
        if not wait_for_url(f"http://127.0.0.1:{PORT_COMPANION}/health", timeout_s=30.0):
            r.check("companion /health came up", False, "30s timeout")
            r.summary_or_exit()
        comp_elapsed_ms = (time.perf_counter() - comp_t0) * 1000.0
        metrics["cold_companion_start_ms"] = comp_elapsed_ms

        base = f"http://127.0.0.1:{PORT_COMPANION}"

        # ── Measurements ────────────────────────────────────────
        r.info("measuring chat / asr / status / ws / ttfp …")
        bench_metrics = run_bench(r, base)
        metrics.update(bench_metrics)

    # ── CSV ─────────────────────────────────────────────────────
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = _write_csv(metrics, timestamp)
    r.info(f"results.csv → {csv_path.relative_to(REPO_ROOT)}")

    # ── SLA gates ───────────────────────────────────────────────
    # Every measured metric is reported; SLA metrics gate, advisory ones
    # log via r.info.
    for metric, value in sorted(metrics.items()):
        sla = SLA_MS.get(metric)
        if sla is None:
            r.info(f"{metric} = {value:.0f}ms (no SLA)")
            continue
        ok = value <= sla
        msg = f"measured {value:.0f}ms"
        if no_sla:
            # In baseline mode, log result but never fail.
            r.info(f"{metric} <= {sla:.0f}ms? {'yes' if ok else 'NO'} ({msg})")
        else:
            r.check(f"{metric} <= {sla:.0f}ms", ok, msg)

    # SLA metrics missing from `metrics` (we couldn't measure) are a
    # silent gap — surface them so a regression doesn't slip through.
    for metric in SLA_MS:
        if metric not in metrics:
            if no_sla:
                r.info(f"{metric}: not measured (skipped)")
            else:
                r.check(f"{metric} measured", False, "no samples collected")

    r.summary_or_exit()


if __name__ == "__main__":
    main()
