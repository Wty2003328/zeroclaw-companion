"""L_tts_long — long-input TTS stutter/repetition catch.

VITS-style models (SBV2) and AR-codec models (Chatterbox, CosyVoice)
both degrade when fed a long single-line input: attention drifts and
the duration predictor / codec LM can loop, producing audible
repetitions. We caught this on 2026-05-18: a 60s reply was sent as
one shot, SBV2 stuttered partway through, the user heard a sentence
play "a couple times" near the end.

Defense: each engine's sidecar pre-splits long input on sentence
punctuation before forwarding. This rig verifies the defense:
  1. Bring up the SBV2 sidecar via launch_tts.py.
  2. Feed it a long JA paragraph (no line breaks, multiple sentences).
  3. Pull the audio.
  4. ASR-verify via faster-whisper.
  5. Detect repetition by scanning the ASR transcript for the same
     4-gram appearing ≥3 times — that's the stutter fingerprint.
  6. Compare ASR transcript char-coverage against the prompt; below
     0.5 jaccard means content drift, not just stutter.

This rig catches the class of bug regardless of engine internals —
it's purely behavioural. Add a new engine + voice → register it
in ENGINES below and the same test applies.

Run:
  python -m tts_tools.test_tts_long_input            # SBV2 default
  python -m tts_tools.test_tts_long_input --engine sbv2-asuna-v2
  python -m tts_tools.test_tts_long_input --keep-running  # leave sidecar up for debug
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
TTS_LAB = REPO_ROOT.parent / "tts_lab"
LAUNCH_TTS = TTS_LAB / "launch_tts.py"

for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

DEFAULT_PORT = 9892
DEFAULT_ENGINE = "sbv2-asuna-v2"

# A long single-line JA paragraph — 6 sentences, ~260 chars total.
# Each sentence ends with a Japanese full stop (。). No line breaks.
# This is the failure mode users hit when an LLM emits a long reply
# without paragraph or line breaks.
LONG_JA_PROMPT = (
    "こんにちは、今日はとても良い天気ですね。"
    "公園を散歩していると、桜の花がきれいに咲いていて、心が癒されました。"
    "近くのカフェで温かいコーヒーを飲みながら、しばらく本を読んでいました。"
    "夕方になると、空がきれいなオレンジ色に染まり、思わず写真を撮りたくなりました。"
    "今夜は家で美味しい料理を作って、ゆっくり過ごす予定です。"
    "明日もきっと素敵な一日になりますように。"
)


def log(msg: str) -> None:
    print(f"[tts-long] {msg}", flush=True)


def fail(msg: str, code: int = 1):
    print(f"[tts-long] FAIL: {msg}", flush=True)
    sys.exit(code)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def spawn_sidecar(engine: str, port: int) -> subprocess.Popen:
    if not LAUNCH_TTS.exists():
        fail(f"launch_tts.py not found at {LAUNCH_TTS}. Adjust the TTS_LAB constant.")
    py = "E:/miniconda/envs/tts/python.exe"  # any python with sys.executable
    log(f"spawning launch_tts.py --engine {engine} --port {port}")
    # Don't capture stdout/stderr — let the sidecar log to the terminal
    # so debugging is straightforward when the synth hangs.
    return subprocess.Popen(
        [py, str(LAUNCH_TTS), "--engine", engine, "--port", str(port)],
        stdout=sys.stderr, stderr=sys.stderr,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )


def wait_for_healthz(port: int, deadline_s: float) -> bool:
    end = time.time() + deadline_s
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < end:
        try:
            r = urllib.request.urlopen(url, timeout=2)
            if r.status == 200:
                body = json.load(r)
                if body.get("ready"):
                    return True
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            pass
        time.sleep(0.5)
    return False


def synth(port: int, text: str) -> bytes:
    body = json.dumps({
        "input": text,
        "voice": "asuna_v2",
        "x_companion": {"language": "ja", "quality": "high"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/audio/speech",
        data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=240) as r:
        wav = r.read()
    log(f"synth: {len(wav)} bytes in {time.time()-t0:.1f}s "
        f"(sr={r.headers.get('X-Sample-Rate')}, ckpt={r.headers.get('X-Checkpoint')})")
    return wav


def asr_transcribe(wav_bytes: bytes) -> tuple[str, float]:
    """Returns (text, duration_s). Uses the `tts` env's faster-whisper."""
    import tempfile
    wav_path = Path(tempfile.gettempdir()) / "tts_long_input.wav"
    wav_path.write_bytes(wav_bytes)
    log("ASR via faster-whisper large-v3 (cuda)…")
    py = "E:/miniconda/envs/tts/python.exe"
    code = f"""
import json
from faster_whisper import WhisperModel
m = WhisperModel('large-v3', device='cuda', compute_type='float16')
segs, info = m.transcribe(r'{wav_path}', language='ja', beam_size=5)
text = ''.join(s.text for s in segs).strip()
print(json.dumps({{'text': text, 'duration': info.duration}}, ensure_ascii=False))
"""
    # Force the subprocess to write UTF-8 to stdout regardless of the
    # Windows console codec (default GBK here). Without this the
    # subprocess's print() of Japanese mojibakes into Latin-1 placeholders
    # and our stutter detector hits false positives on the noise bytes.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    out_bytes = subprocess.check_output([py, "-c", code], env=env)
    out = out_bytes.decode("utf-8", errors="replace")
    last_line = [l for l in out.strip().splitlines() if l.startswith("{")][-1]
    parsed = json.loads(last_line)
    return parsed["text"], float(parsed["duration"])


def detect_stutter(transcript: str, n: int = 4, min_runs: int = 3) -> Optional[str]:
    """Returns the offending n-gram if the same n-character window
    appears `min_runs` times *consecutively* — that's the actual
    stutter fingerprint, not just multiple occurrences across the
    text. Common JA verb endings (e.g. ました) appear 2-3 times
    spread across a paragraph and are NOT stutters.

    A stutter looks like: '心が癒されました癒されました癒されました'
    where the same fragment repeats back-to-back with no other text
    in between."""
    t = transcript.replace(" ", "").replace("　", "")
    if len(t) < n * min_runs:
        return None
    i = 0
    while i <= len(t) - n * min_runs:
        ng = t[i:i+n]
        runs = 1
        j = i + n
        while j <= len(t) - n and t[j:j+n] == ng:
            runs += 1
            j += n
        if runs >= min_runs:
            return f"{ng!r} appears {runs}× consecutively at offset {i}"
        i += 1
    return None


def char_jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb: return 1.0
    if not sa or not sb: return 0.0
    return len(sa & sb) / len(sa | sb)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--engine", default=DEFAULT_ENGINE)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--keep-running", action="store_true")
    p.add_argument("--reuse-sidecar", action="store_true",
                   help="trust that an existing sidecar is already healthy at --port")
    args = p.parse_args()

    proc: Optional[subprocess.Popen] = None
    if not args.reuse_sidecar:
        if port_in_use(args.port):
            fail(f"port {args.port} already in use; pass --reuse-sidecar or pick another --port")
        proc = spawn_sidecar(args.engine, args.port)
        log("waiting for /healthz ready (max 120s)…")
        if not wait_for_healthz(args.port, deadline_s=120.0):
            fail("sidecar never became healthy")

    try:
        log(f"prompt: {len(LONG_JA_PROMPT)} chars, {LONG_JA_PROMPT.count('。')} JA sentences")
        wav = synth(args.port, LONG_JA_PROMPT)
        if len(wav) < 1000:
            fail(f"synth returned tiny audio ({len(wav)} bytes)")

        text, dur = asr_transcribe(wav)
        log(f"asr ({dur:.1f}s audio): {text[:80]}…")

        stutter = detect_stutter(text)
        if stutter:
            fail(f"STUTTER detected — {stutter}\n  full asr: {text}")
        log("✅ no 4-gram stutter detected")

        jacc = char_jaccard(text, LONG_JA_PROMPT)
        log(f"char_jaccard(asr, prompt) = {jacc:.2f}")
        if jacc < 0.5:
            fail(f"content drift: jaccard {jacc:.2f} < 0.5 — model hallucinated")
        log("✅ content fidelity OK")

        if args.keep_running:
            log("--keep-running set; sidecar left up at :%d" % args.port)
            return 0
    finally:
        if proc and not args.keep_running:
            log("shutting down sidecar…")
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{args.port}/shutdown", data=b"", timeout=2,
                )
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
