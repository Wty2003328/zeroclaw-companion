"""Permanent TTS audio-quality rig — comprehensive, ASR-validated, runs
under `python -m tts_tools.run_all --suites tts_audio_quality`.

Promoted from an earlier lab throwaway. This one lives
in `tts_tools/` because:
  - integrates with the existing test-app SOP (`run_all.py`)
  - has pass/fail semantics (exit 0/1)
  - covers ALL four user-reported failure modes systematically:
      1. truncation / not-all-text-read (ASR coverage + tail-survival)
      2. audio bump at start (first-50ms RMS spike)
      3. emotion flatness (RMS coefficient-of-variation)
      4. catastrophic runaway (audio_s > 5x expected from char count)

Coverage:
  - 30 inputs spanning short / medium / long / multi-paragraph /
    emotional / loanword-mixed / digit-heavy / date-heavy / edge cases
    (very-short, trailing punctuation, hesitant fillers)
  - engine-direct path (tier 1) — deterministic, no LLM, no sidecar
  - sidecar HTTP path (tier 2) — validates wire contract too

Exit: 0 if every input passes all four checks; 1 if any fail.
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import soundfile as sf

from tts_tools._test_helpers import CheckReporter, REPO_ROOT

for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

OUT_ROOT = REPO_ROOT.parent / "tts_lab" / "eval_out" / "_tts_audio_quality"
TIMESTAMP = time.strftime("%Y%m%d_%H%M%S")
OUT_DIR = OUT_ROOT / TIMESTAMP
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------- #
# 30 varied real-style chat inputs covering the failure surface
# ---------------------------------------------------------------------------- #
TEST_INPUTS = [
    # Tier 1: very short (the "うん" failure class)
    ("very_short_yes",    "うん"),
    ("very_short_no",     "いや"),
    ("very_short_oh",     "ええ？"),
    # Tier 2: short
    ("greet_short",       "こんにちは！"),
    ("greet_polite",      "こんにちは、今日もよろしくお願いします。"),
    ("affirm",            "うん、わかった。"),
    ("trailing_question", "ねえ、今何してるの？"),
    ("trailing_emphasis", "本当に大切なことなんです！"),
    # Tier 3: medium chat
    ("weather_chat",
     "今日はとてもいい天気ですね。一緒にお散歩しませんか？"),
    ("question_curious",
     "ねえ、聞いてもいい？最近何か面白いことあった？"),
    ("hesitant",
     "えっと…その…ちょっと言いにくいんだけど…"),
    # Tier 4: loanwords + mixed
    ("english_mix",
     "iPhoneの新しいアプリ、知ってる？YouTubeで紹介されていたよ。"),
    ("loanwords_heavy",
     "ノートパソコンとスマートフォンとタブレットをリュックに入れた。"),
    # Tier 5: DIGIT/DATE heavy (the runaway failure class)
    ("digits_dates",
     "2024年12月31日、夜の11時59分にカウントダウンが始まります。"),
    ("numbers_short",
     "30分くらいで終わる予定です。"),
    ("numbers_mid",
     "明日の会議は10時から始まります。資料は5ページあります。"),
    ("price_with_digits",
     "そのケーキは1500円で、お茶を付けると2000円になります。"),
    ("phone_number",
     "電話番号は090-1234-5678です。"),
    # Tier 6: long single paragraph
    ("story_short",
     "むかしむかし、ある小さな村に優しい女の子が住んでいました。"
     "彼女は毎日森の中を歩いて、動物たちにご飯をあげていました。"
     "ある日、村に大きな災害が起こり、たくさんの動物が困っていました。"),
    ("story_long",
     "むかしむかし、ある小さな村に、とても優しい女の子が住んでいました。"
     "彼女は毎日、森の中を歩いて、動物たちにご飯をあげていました。"
     "ある日、村に大きな災害が起こり、たくさんの動物が困っていました。"
     "女の子は勇気を出して、みんなを助けるために森の奥へと向かいました。"
     "そして、彼女の優しさによって、村と動物たちは救われたのです。"
     "それからずっと、彼女は村のヒーローとして語り継がれました。"),
    # Tier 7: emotional / expressive
    ("excited",
     "わぁ、すごい！本当にすごいよ！信じられない！"),
    ("sad",
     "そっか…ちょっと寂しいな。でも、また会えるよね？"),
    ("teasing",
     "もう、そんなにねほりはほり聞かないでよー。恥ずかしいんだから。"),
    ("gentle",
     "ゆっくりでいいよ。大丈夫、ちゃんと待ってるから。"),
    # Tier 8: multi-paragraph
    ("two_paragraphs",
     "今日のお話を聞いてくれてありがとう。\n\n"
     "また明日も色々お話しようね。おやすみ。"),
    ("three_paragraphs",
     "わかった、今夜は早めに寝るね。\n\n"
     "明日は朝から忙しいから、ちゃんと準備しないと。\n\n"
     "じゃあ、また明日ね。おやすみなさい。"),
    # Tier 9: edge cases that previously broke production
    ("punctuation_dense",
     "えっ？！本当に！？信じられない…"),
    ("repeated_word",
     "そうそう、その話、覚えてる？"),
    ("symbol_mix",
     "コーヒー★を飲みながら本（小説）を読むのが好き。"),
]


# ---------------------------------------------------------------------------- #
# Analyzers — pure functions, easy to unit-test
# ---------------------------------------------------------------------------- #
def normalize_text(s: str) -> str:
    """Strip whitespace + punctuation for char-by-char comparison."""
    return re.sub(r"[\s,\.\?\!、。？！「」『』ー〜・…★（）()]+", "", s)


def detect_audio_bump(audio: np.ndarray, sr: int) -> dict:
    """Detect click/transient in first 50ms vs sustained signal in next 100ms.
    Returns ratio + verdict. Bump = first 50ms RMS > 3× the next-100ms RMS,
    with absolute floor (avoid false positives on silent intro)."""
    if len(audio) < int(0.15 * sr):
        return {"bump": False, "reason": "too_short"}
    f50 = audio[:int(0.05 * sr)]
    next100 = audio[int(0.05 * sr):int(0.15 * sr)]
    f10_peak = float(np.max(np.abs(audio[:int(0.01 * sr)])))
    rms_f50 = float(np.sqrt(np.mean(f50 ** 2)))
    rms_next = float(np.sqrt(np.mean(next100 ** 2)) + 1e-9)
    ratio = rms_f50 / rms_next
    return {
        "first_50ms_rms": round(rms_f50, 5),
        "next_100ms_rms": round(rms_next, 5),
        "ratio": round(ratio, 2),
        "first_10ms_peak": round(f10_peak, 5),
        "bump": ratio > 3.0 and rms_f50 > 0.005,
    }


def measure_emotion(audio: np.ndarray, sr: int) -> dict:
    """Coefficient-of-variation of 20ms-bin RMS (within voiced regions).
    Higher CV = more dynamic = more expressive. Threshold flat<0.30."""
    bin_samples = int(0.02 * sr)
    nbins = max(1, len(audio) // bin_samples)
    bin_rms = np.array([
        np.sqrt(np.mean(audio[i*bin_samples:(i+1)*bin_samples] ** 2))
        for i in range(nbins)
    ])
    active = bin_rms[bin_rms > 0.003]  # drop silent bins
    if len(active) < 5:
        return {"rms_cv": 0.0, "verdict": "too_quiet"}
    cv = float(np.std(active) / np.mean(active))
    return {
        "rms_cv": round(cv, 3),
        "verdict": "flat" if cv < 0.30 else ("mild" if cv < 0.50 else "dynamic"),
    }


def check_completeness(asr_text: str, input_text: str,
                       audio_s: float, input_chars: int) -> dict:
    """4 checks compound into a 'completeness' verdict:
      - asr_coverage: SequenceMatcher ratio of input vs ASR (after normalize)
      - tail_survived: last 15 chars of input have a 5-char window in ASR
      - looped: 4-7 gram repeated 3+ times in ASR
      - runaway: audio_s > 5x what we'd expect from input_chars × 0.15s/char
    """
    asr_n = normalize_text(asr_text)
    in_n = normalize_text(input_text)
    sm = SequenceMatcher(None, in_n, asr_n)
    coverage = sm.ratio()
    in_tail = in_n[-15:] if len(in_n) >= 15 else in_n
    tail_survived = any(in_tail[i:i+5] in asr_n
                        for i in range(max(0, len(in_tail) - 5) + 1))
    looped = False; loop_ev = ""
    for n in (4, 5, 6, 7):
        for i in range(len(asr_n) - 3 * n):
            ng = asr_n[i:i+n]
            if asr_n[i+n:i+2*n] == ng and asr_n[i+2*n:i+3*n] == ng:
                looped = True; loop_ev = f"3x{ng!r}"; break
        if looped: break
    # Runaway: model generated WAY too much audio for the input
    expected_s = max(0.5, input_chars * 0.15)  # ~0.15s per JA char
    runaway = audio_s > 5 * expected_s
    truncated = (not tail_survived) and (coverage < 0.85)
    return {
        "asr_coverage": round(coverage, 3),
        "tail_survived": tail_survived,
        "looped": looped, "loop_evidence": loop_ev,
        "runaway": runaway, "expected_s": round(expected_s, 1),
        "truncated": truncated,
        "verdict": ("RUNAWAY" if runaway
                    else "LOOPED" if looped
                    else "TRUNCATED" if truncated
                    else "ok"),
    }


# ---------------------------------------------------------------------------- #
# Synthesis drivers — engine-direct (tier 1) and sidecar HTTP (tier 2)
# ---------------------------------------------------------------------------- #
def get_engine():
    """Lazy-load the production engine + register asuna voice. Cached."""
    if hasattr(get_engine, "_cached"):
        return get_engine._cached
    sys.path.insert(0, str(REPO_ROOT / "tools" / "avatar"))
    from qwen3_engine import Qwen3TTSEngine
    lab = REPO_ROOT.parent / "tts_lab"
    print("[engine] loading qwen3-tts ...", flush=True)
    t0 = time.time()
    eng = Qwen3TTSEngine(
        model_dir=str(lab / "models" / "qwen3-tts-1.7b-base"),
        apply_kernel_opt=True,
    )
    eng.register_voice(
        voice_id="asuna",
        reference_audio=str(lab / "reference_clips" / "asuna_concat_diverse5.wav"),
        reference_language="ja",
        reference_text=(lab / "reference_clips" / "asuna_concat_diverse5.txt")
            .read_text(encoding="utf-8").strip(),
    )
    print(f"[engine] ready in {time.time()-t0:.1f}s", flush=True)
    get_engine._cached = eng
    return eng


def synth_engine(text: str, quality: str = "high") -> tuple[int, np.ndarray, float]:
    eng = get_engine()
    t0 = time.time()
    sr, audio = eng.synthesize(text=text, voice_id="asuna",
                                language="ja", quality=quality)
    return int(sr), audio.astype(np.float32), time.time() - t0


def synth_sidecar(text: str, quality: str = "high") -> tuple[int, np.ndarray, float]:
    payload = {
        "input": text, "voice": "asuna",
        "response_format": "wav", "stream_format": "audio",
        "x_companion": {"language": "ja", "quality": quality},
    }
    t0 = time.time()
    req = urllib.request.Request(
        "http://127.0.0.1:9890/v1/audio/speech", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        wav_bytes = r.read()
        sr = int(r.headers.get("X-Sample-Rate", "24000"))
    audio, file_sr = sf.read(io.BytesIO(wav_bytes))
    if audio.ndim > 1: audio = audio[:, 0]
    return int(file_sr), audio.astype(np.float32), time.time() - t0


def is_sidecar_up() -> bool:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:9890/healthz", timeout=2)
        return '"status":"ok"' in r.read().decode()
    except Exception:
        return False


# ---------------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------------- #
def main():
    r = CheckReporter("tts_audio_quality")
    print(f"[tts-audit] output dir: {OUT_DIR}", flush=True)

    # Choose synthesis tier: prefer sidecar if up (tests production wire),
    # fall back to engine-direct.
    if is_sidecar_up():
        print("[tts-audit] tier=sidecar-http (port 9890 up)", flush=True)
        synth = synth_sidecar
    else:
        print("[tts-audit] tier=engine-direct (sidecar not running)", flush=True)
        synth = synth_engine

    # Lazy-load ASR
    print("[tts-audit] loading faster-whisper ...", flush=True)
    from faster_whisper import WhisperModel
    asr = WhisperModel("small", device="cuda", compute_type="float16")

    failures = []
    for in_name, text in TEST_INPUTS:
        try:
            sr, audio, wall = synth(text, quality="high")
        except Exception as e:
            r.check(f"{in_name}: synthesize", False, f"{type(e).__name__}: {e}")
            failures.append({"label": in_name, "error": str(e)})
            continue
        audio_s = len(audio) / sr
        wav_path = OUT_DIR / f"{in_name}.wav"
        sf.write(wav_path, audio, sr)

        # ASR
        segs, _ = asr.transcribe(str(wav_path), language="ja", beam_size=1)
        asr_text = "".join(s.text for s in segs).strip()

        # Analyze
        bump = detect_audio_bump(audio, sr)
        emo = measure_emotion(audio, sr)
        comp = check_completeness(asr_text, text, audio_s, len(text))

        # Per-input checks: 4 axes, each pass/fail
        complete_ok = comp["verdict"] == "ok"
        bump_ok = not bump["bump"]
        # Emotion: don't fail on "mild" since temp=0.3 high-preset can be mild
        emo_ok = emo["verdict"] != "flat"

        ok = complete_ok and bump_ok and emo_ok
        if not ok:
            failures.append({
                "label": in_name, "text": text,
                "audio_s": audio_s, "asr": asr_text,
                "completeness": comp, "bump": bump, "emotion": emo,
                "wav": str(wav_path),
            })

        r.check(
            f"{in_name}: complete + clean + dynamic",
            ok,
            f"audio={audio_s:.1f}s  cov={comp['asr_coverage']:.2f}  "
            f"bump={bump['ratio']:.1f}x  cv={emo['rms_cv']:.2f}  "
            f"verdict={comp['verdict']}"
        )

    print()
    if failures:
        print("=" * 90)
        print(f"FAILURES ({len(failures)}/{len(TEST_INPUTS)}):")
        print("=" * 90)
        for f in failures:
            if "error" in f:
                print(f"  {f['label']}: ERROR — {f['error']}"); continue
            c = f["completeness"]
            print(f"  {f['label']}  ({c['verdict']})")
            print(f"    audio_s={f['audio_s']:.1f}s (expected~{c['expected_s']})  "
                  f"cov={c['asr_coverage']:.2f}  tail={'Y' if c['tail_survived'] else 'N'}")
            print(f"    input: {f['text'][:80]}")
            print(f"    asr:   {f['asr'][:80]}")
            print(f"    wav:   {f['wav']}")
            print()
    summary = {"timestamp": TIMESTAMP, "n_total": len(TEST_INPUTS),
               "n_failed": len(failures), "failures": failures}
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[tts-audit] summary: {OUT_DIR / 'summary.json'}")

    r.summary_or_exit()


if __name__ == "__main__":
    main()
