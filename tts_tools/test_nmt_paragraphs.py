"""L3-para — NMT translator must preserve \\n\\n paragraph breaks.

The companion's paragraph-wise TTS streamer fires per paragraph,
delimited by `\\n\\n` in the translated text. If the NMT collapses
those breaks, the streamer sees one paragraph and synthesizes the
entire reply as a single audio chunk — the user hears only one
paragraph's worth of audio, the rest "disappears".

This is the bug we hit on 2026-05-18: multi-paragraph Asuna replies
played only paragraph one. Root cause was the NMT sidecar feeding
the whole multi-paragraph input directly to NLLB, which stripped
the whitespace.

This rig sends a known 3-paragraph input and asserts the output
has exactly 3 `\\n\\n`-separated paragraphs, each non-empty.

Run:
  python -m tts_tools.test_nmt_paragraphs              # default :9881
  python -m tts_tools.test_nmt_paragraphs --reuse      # use running sidecar
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
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
NMT_SIDECAR = REPO_ROOT / "tools" / "avatar" / "nmt_translator_server.py"

for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

DEFAULT_PORT = 9881

# A 3-paragraph EN input. Each paragraph has 1-3 sentences. The
# translator MUST preserve the two `\n\n` breaks so the companion's
# paragraph-wise TTS streamer can fire 3 separate synth calls.
MULTI_PARA_INPUT = (
    "Hello, how are you today? The weather is really nice.\n\n"
    "Let me tell you a story about a small cat named Mochi.\n\n"
    "Mochi lived by the seaside and loved to chase butterflies all day long."
)


def log(msg: str) -> None:
    print(f"[nmt-para] {msg}", flush=True)


def fail(msg: str, code: int = 1):
    print(f"[nmt-para] FAIL: {msg}", flush=True)
    sys.exit(code)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_health(port: int, deadline_s: float) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            pass
        time.sleep(0.5)
    return False


def spawn_sidecar(port: int) -> subprocess.Popen:
    py = "E:/miniconda/envs/tts/python.exe"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["NMT_PORT"] = str(port)
    env["NMT_SRC_LANG"] = "en"
    env["NMT_TGT_LANG"] = "ja"
    env["NMT_QUALITY_PRESET"] = "balanced"  # smaller model = faster startup
    log(f"spawning nmt_translator_server.py on :{port}")
    return subprocess.Popen(
        [py, str(NMT_SIDECAR)],
        cwd=REPO_ROOT,
        env=env,
        stdout=sys.stderr, stderr=sys.stderr,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )


def translate(port: int, text: str, src: str = "en", tgt: str = "ja") -> str:
    body = json.dumps({"text": text, "src_lang": src, "tgt_lang": tgt}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/translate",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body["text"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--reuse", action="store_true",
                   help="trust that an NMT sidecar is already running at --port")
    args = p.parse_args()

    proc: Optional[subprocess.Popen] = None
    if not args.reuse:
        if port_in_use(args.port):
            fail(f"port {args.port} in use; pass --reuse or pick another --port")
        proc = spawn_sidecar(args.port)
        log("waiting for /health (max 120s for NLLB load)…")
        if not wait_for_health(args.port, deadline_s=120.0):
            fail("NMT sidecar never became healthy")

    try:
        expected_paras = MULTI_PARA_INPUT.count("\n\n") + 1
        log(f"input: {len(MULTI_PARA_INPUT)} chars, {expected_paras} paragraphs")

        t0 = time.time()
        out = translate(args.port, MULTI_PARA_INPUT)
        elapsed = time.time() - t0
        log(f"translated in {elapsed:.2f}s")

        nl_count = out.count("\n")
        para_count = out.count("\n\n") + 1 if out.strip() else 0

        # Each paragraph must be non-empty
        paragraphs = out.split("\n\n")
        empty = [i for i, p in enumerate(paragraphs) if not p.strip()]

        log(f"output ({len(out)} chars): {out[:150]}…")
        log(f"newlines: {nl_count}, paragraph-breaks(\\n\\n): {out.count(chr(10)+chr(10))}")
        log(f"paragraphs: {para_count}")

        if para_count != expected_paras:
            fail(f"paragraph break mismatch: input had {expected_paras}, "
                 f"output has {para_count} (translator stripped \\n\\n)")
        if empty:
            fail(f"paragraph(s) {empty} are empty after translation")
        if len(out) < len(MULTI_PARA_INPUT) // 3:
            fail(f"output suspiciously short ({len(out)} chars) — translation dropped content")

        log("✅ all paragraph breaks preserved")
        log("PASS")
        return 0
    finally:
        if proc and not args.reuse:
            log("shutting down NMT sidecar…")
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


if __name__ == "__main__":
    sys.exit(main())
