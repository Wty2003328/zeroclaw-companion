"""HTTP server for the small-NMT translation engine.

Speaks a small contract for the companion's avatar subagent's
"translator" sidecar:

    POST /translate  {"text": "Hello world",
                       "src_lang": "en"?,    (optional, falls back to default)
                       "tgt_lang": "ja"?}
                  -> {"text": "<translated>", "src_lang": "en", "tgt_lang": "ja"}

    GET  /health    -> {"status": "ok", "backend": "opus-mt", "model_id": "...",
                        "src_lang": "en", "tgt_lang": "ja"}

    POST /shutdown  -> graceful exit

Env config — see `TranslatorConfig.from_env`. Companion forwards these
from [avatar.subagent.translator] when this backend is selected.

Run:
    python tools/avatar/nmt_translator_server.py
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# Importing the engine module first means its lightweight env-knob
# preamble fires before HTTP deps load. No model load yet — that
# happens in `NMTEngine(...)` below.
from nmt_engine import NMTEngine, TranslatorConfig

DEFAULT_PORT = 9881  # distinct from TTS server's 9880


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class TranslateRequest(BaseModel):
    text: str
    # Optional language overrides. Most callers will just send `text`
    # and rely on the engine's configured pair. For multilingual NLLB
    # callers can pass per-request langs once that backend ships.
    src_lang: Optional[str] = None
    tgt_lang: Optional[str] = None


class TranslateResponse(BaseModel):
    text: str
    src_lang: str
    tgt_lang: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(engine: NMTEngine) -> FastAPI:
    app = FastAPI(title="nmt-translator")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "backend": engine.backend_name,
            "model_id": engine.model_id,
            "quality_preset": engine.quality_preset,
            "num_beams": engine.config.resolve_num_beams(),
            "device": engine.config.device,
            "precision": engine.config.precision,
            "src_lang": engine.config.src_lang,
            "tgt_lang": engine.config.tgt_lang,
        }

    @app.post("/translate", response_model=TranslateResponse)
    async def translate(req: TranslateRequest):
        if not req.text or not req.text.strip():
            raise HTTPException(400, "text must not be empty")

        wanted_src = req.src_lang or engine.config.src_lang
        wanted_tgt = req.tgt_lang or engine.config.tgt_lang

        # Target-language check is hard — the model literally cannot
        # emit a language it wasn't trained for. NLLB's forced_bos
        # token is fixed at engine init (changing requires a model
        # reload), and Marian models are pair-pinned.
        if wanted_tgt != engine.config.tgt_lang:
            raise HTTPException(
                400,
                f"requested target {wanted_tgt!r} but engine is configured "
                f"for {engine.config.tgt_lang!r}. Change NMT_TGT_LANG and "
                "restart the sidecar, or switch the engine through the UI.",
            )

        # Source-language: NLLB tolerates per-request override (the
        # tokenizer accepts a different src_lang each call). Marian
        # doesn't — log a warning if the caller asked for a non-default
        # source on a backend that ignores it.
        src_override: Optional[str] = None
        if wanted_src != engine.config.src_lang:
            if engine.supports_src_lang_override:
                src_override = wanted_src
            else:
                print(
                    f"[nmt-translator] WARN: requested src={wanted_src!r} "
                    f"but engine ({engine.backend_name}) is pair-pinned to "
                    f"{engine.config.src_lang!r}→{engine.config.tgt_lang!r}. "
                    "Ignoring the override and translating as configured."
                )

        t0 = time.time()
        try:
            # Preserve paragraph breaks across the model. NLLB/Marian
            # collapse all whitespace when given a multi-paragraph
            # input, but the companion's paragraph-wise TTS streamer
            # NEEDS `\n\n` to fire — without it, the entire reply is
            # synthesized as one chunk and the user only hears one
            # paragraph's worth of audio. Translate each paragraph
            # separately, rejoin with the same `\n\n` delimiter.
            paragraphs = req.text.split("\n\n")
            translated_paras: list[str] = []
            for para in paragraphs:
                para_stripped = para.strip()
                if not para_stripped:
                    translated_paras.append("")
                    continue
                translated_paras.append(
                    engine.translate(para_stripped, src_lang=src_override)
                )
            out = "\n\n".join(translated_paras)
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise HTTPException(500, f"translation failed: {e}") from e

        effective_src = src_override or engine.config.src_lang
        print(
            f"[nmt-translator] /translate "
            f"{effective_src}->{engine.config.tgt_lang} "
            f"chars={len(req.text)} paragraphs={len(paragraphs)} "
            f"wall={time.time() - t0:.3f}s"
        )
        return TranslateResponse(
            text=out,
            src_lang=effective_src,
            tgt_lang=engine.config.tgt_lang,
        )

    @app.post("/shutdown")
    async def shutdown():
        """Graceful shutdown — companion-server hits this when closing.
        Returns immediately; the actual exit happens on a daemon thread
        so the response can flush before the process dies."""
        def _exit_soon():
            time.sleep(0.2)
            engine.shutdown()
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True).start()
        return {"status": "shutting_down"}

    return app


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------


def _install_signal_handlers(engine: NMTEngine) -> None:
    def _on_signal(sig, _frame):
        print(f"[nmt-translator] received signal {sig}; cleaning up")
        engine.shutdown()
        os._exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)
    atexit.register(engine.shutdown)


def main() -> None:
    port = int(os.environ.get("NMT_PORT", DEFAULT_PORT))
    skip_warmup = os.environ.get("NMT_NO_WARMUP") == "1"

    config = TranslatorConfig.from_env()
    print(
        f"[nmt-translator] config: preset={config.quality_preset} "
        f"model={config.resolve_model_id()} "
        f"{config.src_lang}->{config.tgt_lang} "
        f"device={config.device} precision={config.precision} "
        f"beams={config.resolve_num_beams()}"
    )
    engine = NMTEngine(config)
    _install_signal_handlers(engine)
    if skip_warmup:
        print("[nmt-translator] warmup skipped (NMT_NO_WARMUP=1)")
    else:
        engine.warmup()

    app = build_app(engine)
    print(f"[nmt-translator] serving on http://127.0.0.1:{port}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    finally:
        engine.shutdown()


if __name__ == "__main__":
    main()
