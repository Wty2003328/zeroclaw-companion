"""Reference TTS-port wrapper for GPT-SoVITS v4 zero-shot voice cloning.

Speaks the model-agnostic waifu-companion avatar TTS contract:

    POST /tts   {"text": "...", "language": "ja", "voice": "...", "speed": 1.0}
                -> WAV bytes (with X-Sample-Rate / X-Channels / X-Format headers)
    GET  /health
                -> 200 OK once the model is loaded

Any TTS engine can plug in by writing a wrapper that conforms to the
same contract. Configure the companion with:

    [avatar.tts]
    engine             = "gpt-sovits-v4"
    launch_command     = "python tools/avatar/gptsovits_tts_server.py"
    port               = 9880
    voice              = "<your-voice-id>"
    reference_audio    = "<GPT-SoVITS root>/logs/<voice>/0_sliced/<clip>.wav"
    reference_text     = "<transcript of the reference clip>"
    reference_language = "ja"
    language           = "ja"   # default speech language
    auto_start         = true
    gpu_device         = 0

Env vars (all optional — companion-server forwards these from
`[avatar.tts]`):

    TTS_PORT               server bind port (default 9880)
    TTS_VOICE              voice id; also the default fine-tune name prefix
    TTS_LANGUAGE           default speech language
    TTS_REFERENCE_AUDIO    path to the reference clip (3-10 s)
    TTS_REFERENCE_TEXT     transcript of the reference clip
    TTS_REFERENCE_LANG     language of the reference clip
    TTS_MODEL_PATH         GPT-SoVITS install root
    TTS_LORA_NAME          fine-tune file-name prefix to load from
                           SoVITS_weights_v4/ and GPT_weights_v3/.
                           Defaults to `$TTS_VOICE` (if set) or the
                           first checkpoint found in each directory.
    CUDA_VISIBLE_DEVICES   GPU index; -1 for CPU

Run standalone for testing:
    python tools/avatar/gptsovits_tts_server.py
"""

import atexit
import io
import os
import signal
import sys
import threading
import time
import wave
from pathlib import Path

# When companion-server pipes our stdout/stderr, those streams open in
# Windows's default cp1252 instead of utf-8. Any em-dash in a log line
# (the wrapper prints a few) then raises `UnicodeEncodeError` inside
# Python's logging handler and the access log goes silent. Reconfigure
# both streams to utf-8 with `replace` errors so the wrapper never
# crashes its own logger.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- startup-time knobs (set before torch / transformers import) ---
# CUDA 12+ defaults to LAZY module loading; making it explicit is harmless
# and shaves a bit off `import torch` + first CUDA op on older toolkits.
os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")
# All HF model weights live on disk already (GPT-SoVITS ships them) — never
# let transformers/huggingface_hub do a network round-trip to "check for
# updates" at `from_pretrained()` time. Saves a few seconds on every start.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# Whether to disable PyTorch's CUDA caching allocator. When enabled,
# every tensor `del` returns memory to the driver immediately instead
# of into PyTorch's per-process pool — preventing VRAM fragmentation
# leaking into the next graphics workload (we observed "games stutter
# for ~30-90s after closing the companion" with caching ON).
#
# DOWNSIDE: 2-3x slower per inference call. PyTorch's cache was
# designed exactly to skip the per-allocation driver round-trip, and
# disabling it bottlenecks GPU utilization in the loop.
#
# Off by default now; the explicit `/shutdown` cleanup we already do
# in companion-server (CUDA empty_cache + process exit) handles the
# bulk of fragmentation at the right time. Users who still observe
# game stutter can re-enable by setting the env var explicitly.
if os.environ.get("PYTORCH_NO_CUDA_MEMORY_CACHING") == "1":
    print("[gpt-sovits-tts] CUDA caching DISABLED (slower; explicit env var)")
else:
    print("[gpt-sovits-tts] CUDA caching ON (default — ~2x faster inference)")

# When companion-server spawns this script via Tauri's sidecar, the
# parent's PATH does NOT include the conda env's Scripts/ dir, so
# subprocesses (notably ffmpeg, called by GPT-SoVITS' load_audio for
# reference clip decoding) fail with "WinError 2: cannot find the file
# specified". Activating conda's hooks at runtime is fragile; we just
# prepend the env's Scripts + Library/bin (where conda installs
# Windows binaries) to PATH unconditionally — harmless if they're
# already there.
_env_root = os.path.dirname(sys.executable)
for _bindir in (
    os.path.join(_env_root, "Scripts"),
    os.path.join(_env_root, "Library", "bin"),
    os.path.join(_env_root, "Library", "mingw-w64", "bin"),
    os.path.join(_env_root, "Library", "usr", "bin"),
):
    if os.path.isdir(_bindir):
        os.environ["PATH"] = _bindir + os.pathsep + os.environ.get("PATH", "")

import numpy as np
import torch
from fastapi import FastAPI, HTTPException

# Performance knobs — applied immediately after torch import so all
# downstream model/tensor work picks them up.
#
# - cudnn.benchmark = FALSE. It autotunes conv algorithms per *input
#   shape* — a win only when the same shapes recur. TTS text length
#   (hence the BigVGAN vocoder's conv input lengths) is different on
#   every call, so the shapes never repeat and `benchmark=True` pays a
#   ~15-20s autotune sweep on *every* /tts request (observed: ~20s for
#   even an 8-char sentence). With it off, cuDNN picks a fast heuristic
#   algorithm with no autotune — slightly slower per op, but each call
#   drops from ~20s to ~2-5s. Output audio is identical either way.
#   Override with TTS_CUDNN_BENCHMARK=1 if your usage really is fixed-shape.
# - allow_tf32 lets the matmul + cuDNN pipelines use TF32 on Ampere+.
#   We're already in fp16 for the heavy paths, but a few helper ops
#   stay in fp32 and benefit.
# - inference_mode is the strict no-grad context (faster than
#   no_grad). We swap it in below for the synthesis hot path.
torch.backends.cudnn.benchmark = os.environ.get("TTS_CUDNN_BENCHMARK") == "1"
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn


# ---------------------------------------------------------------------------
# Locate the GPT-SoVITS install. TTS_MODEL_PATH points at the repo root.
# ---------------------------------------------------------------------------

_TTS_MODEL_PATH = os.environ.get("TTS_MODEL_PATH")
if not _TTS_MODEL_PATH:
    raise SystemExit(
        "TTS_MODEL_PATH env var not set. Point it at your GPT-SoVITS "
        "checkout root, e.g.\n"
        "    set TTS_MODEL_PATH=C:\\path\\to\\GPT-SoVITS   (Windows)\n"
        "    export TTS_MODEL_PATH=/path/to/GPT-SoVITS    (Linux/macOS)\n"
        "or set it via [avatar.tts] model_path in companion.toml."
    )
GPT_SOVITS_ROOT = Path(_TTS_MODEL_PATH).resolve()

if not GPT_SOVITS_ROOT.exists():
    raise SystemExit(f"GPT-SoVITS root not found: {GPT_SOVITS_ROOT}")

os.chdir(str(GPT_SOVITS_ROOT))
sys.path.insert(0, str(GPT_SOVITS_ROOT))
sys.path.insert(0, str(GPT_SOVITS_ROOT / "GPT_SoVITS"))

# Optional: prepend a Python env's Scripts/ dir to PATH so a bundled
# ffmpeg.exe (common in conda envs) is discoverable. Pass the path via
# TTS_FFMPEG_BIN; ignored if unset.
_ffmpeg_bin = os.environ.get("TTS_FFMPEG_BIN")
if _ffmpeg_bin:
    os.environ["PATH"] = _ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")
os.environ["version"] = "v4"


# ---------------------------------------------------------------------------
# Model loading — mirrors test_v4_inference.py.
# Done once at process start. The /tts handler reuses these tensors.
# ---------------------------------------------------------------------------

DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
CNHUBERT_PATH = str(GPT_SOVITS_ROOT / "GPT_SoVITS" / "pretrained_models" / "chinese-hubert-base")
BERT_PATH = str(GPT_SOVITS_ROOT / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large")
S2_CONFIG = str(GPT_SOVITS_ROOT / "GPT_SoVITS" / "configs" / "s2.json")
PRETRAINED_S2G_V4 = str(
    GPT_SOVITS_ROOT / "GPT_SoVITS" / "pretrained_models" / "gsv-v4-pretrained" / "s2Gv4.pth"
)
VOCODER_PATH = str(
    GPT_SOVITS_ROOT / "GPT_SoVITS" / "pretrained_models" / "gsv-v4-pretrained" / "vocoder.pth"
)

# nltk (English POS tagger + cmudict) is only used by `clean_text(text, "en")`.
# Importing it + checking/downloading the data costs ~1s at startup that a
# Japanese-only setup never needs — defer it to the first English request.
_nltk_ready = False


def _ensure_nltk() -> None:
    global _nltk_ready
    if _nltk_ready:
        return
    import nltk

    for pkg in ("averaged_perceptron_tagger_eng", "cmudict", "averaged_perceptron_tagger"):
        try:
            nltk.data.find(f"taggers/{pkg}" if "tagger" in pkg else f"corpora/{pkg}")
        except LookupError:
            nltk.download(pkg, quiet=True)
    _nltk_ready = True


print("[gpt-sovits-tts] Loading HuBERT...")
from GPT_SoVITS.feature_extractor import cnhubert  # noqa: E402

cnhubert.cnhubert_base_path = CNHUBERT_PATH
hubert = cnhubert.get_model().half().to(DEVICE).eval()

# The Chinese RoBERTa BERT (~650 MB) is only consulted by `_bert_for()` when
# the text language is "zh" — for ja/en it returns zeros. So skip loading it
# at startup (and skip `import transformers`, ~1-2 s) and pull it in lazily
# on the first zh request.
_bert_lock = threading.Lock()
_bert_pair = None  # (tokenizer, model) once loaded


def _get_bert():
    global _bert_pair
    if _bert_pair is None:
        with _bert_lock:
            if _bert_pair is None:
                print("[gpt-sovits-tts] Loading BERT (first zh request)...")
                from transformers import AutoModelForMaskedLM, AutoTokenizer

                tok = AutoTokenizer.from_pretrained(BERT_PATH, local_files_only=True)
                mdl = (
                    AutoModelForMaskedLM.from_pretrained(BERT_PATH, local_files_only=True)
                    .half()
                    .to(DEVICE)
                    .eval()
                )
                _bert_pair = (tok, mdl)
    return _bert_pair

print("[gpt-sovits-tts] Loading SoVITS v4 (DiT + LoRA-merged)...")
import GPT_SoVITS.utils as utils  # noqa: E402
from GPT_SoVITS.module.models import Generator, SynthesizerTrnV3  # noqa: E402
from GPT_SoVITS.module.mel_processing import (  # noqa: E402
    mel_spectrogram_torch,
    spectrogram_torch,
)
from peft import LoraConfig, get_peft_model  # noqa: E402

hps = utils.get_hparams_from_file(S2_CONFIG)
hps.model.version = "v4"

vits = SynthesizerTrnV3(
    hps.data.filter_length // 2 + 1,
    hps.train.segment_size // hps.data.hop_length,
    n_speakers=hps.data.n_speakers,
    **hps.model,
)
base_state = torch.load(PRETRAINED_S2G_V4, map_location="cpu", weights_only=False)["weight"]
vits.load_state_dict(base_state, strict=False)

lora_config = LoraConfig(
    target_modules=["to_k", "to_q", "to_v", "to_out.0"],
    r=32,
    lora_alpha=32,
    init_lora_weights=True,
)
vits.cfm = get_peft_model(vits.cfm, lora_config)

# Fine-tune name prefix for picking checkpoints from
# SoVITS_weights_v4/ and GPT_weights_v3/. Convention from GPT-SoVITS
# training: files named `<prefix>_e<epoch>_...pth` (SoVITS) and
# `<prefix>-e<epoch>.ckpt` (GPT). Defaults to $TTS_VOICE if set;
# otherwise picks whichever checkpoint the directory glob returns
# first so an unconfigured user with a single fine-tune still works.
_LORA_PREFIX = os.environ.get("TTS_LORA_NAME") or os.environ.get("TTS_VOICE") or ""

sovits_dir = GPT_SOVITS_ROOT / "SoVITS_weights_v4"
# Same forgiving pattern as the GPT side: prefer prefix-matched files,
# then fall through to "any .pth in the directory".
_sovits_patterns = ([f"{_LORA_PREFIX}*.pth"] if _LORA_PREFIX else []) + ["*.pth"]
sovits_files: list = []
sovits_pattern = "*.pth"
for _pat in _sovits_patterns:
    hits = list(sovits_dir.glob(_pat))
    if hits:
        sovits_pattern = _pat
        sovits_files = hits
        break
# Sort by epoch when the naming convention exposes one; otherwise lexical.
try:
    sovits_files = sorted(
        sovits_files,
        key=lambda p: int(p.stem.split("_e")[1].split("_")[0]),
    )
except (IndexError, ValueError):
    pass
if not sovits_files:
    raise SystemExit(
        f"No SoVITS LoRA checkpoints matching '{sovits_pattern}' found in "
        f"{sovits_dir}. Set TTS_LORA_NAME or place a fine-tune in "
        f"SoVITS_weights_v4/."
    )
SOVITS_PATH = str(sovits_files[-1])
print(f"[gpt-sovits-tts]   SoVITS ckpt: {Path(SOVITS_PATH).name}")
ft_state = torch.load(SOVITS_PATH, map_location="cpu", weights_only=False)["weight"]
vits.load_state_dict(ft_state, strict=False)
vits.cfm = vits.cfm.merge_and_unload()
vits = vits.half().to(DEVICE).eval()

print("[gpt-sovits-tts] Loading 48kHz vocoder...")
vocoder = Generator(
    initial_channel=100,
    resblock="1",
    resblock_kernel_sizes=[3, 7, 11],
    resblock_dilation_sizes=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
    upsample_rates=[10, 6, 2, 2, 2],
    upsample_initial_channel=512,
    upsample_kernel_sizes=[20, 12, 4, 4, 4],
    gin_channels=0,
    is_bias=True,
)
# remove_weight_norm must run BEFORE loading; ckpt has plain weights.
vocoder.remove_weight_norm()
vocoder.load_state_dict(torch.load(VOCODER_PATH, map_location="cpu", weights_only=False))
vocoder = vocoder.half().to(DEVICE).eval()

print("[gpt-sovits-tts] Loading GPT (v3-base, fine-tuned)...")
from GPT_SoVITS.AR.models.t2s_lightning_module import (  # noqa: E402
    Text2SemanticLightningModule,
)

gpt_dir = GPT_SOVITS_ROOT / "GPT_weights_v3"
# GPT-SoVITS' training script names the file with a free-form prefix
# followed by `-e<epoch>.ckpt`. The user-supplied `TTS_LORA_NAME` (or
# `TTS_VOICE`) may be the full prefix (`asuna_combined`) or a shorter
# nickname (`asuna`). Try the strict form first; if that finds nothing,
# fall back to `<prefix>*-e*.ckpt` which tolerates suffixed prefixes.
def _find_gpt_ckpts(prefix: str):
    patterns = []
    if prefix:
        patterns.append(f"{prefix}-e*.ckpt")
        patterns.append(f"{prefix}*-e*.ckpt")  # tolerate suffixed prefixes
    patterns.append("*-e*.ckpt")               # last-resort: any LoRA in dir
    for pat in patterns:
        hits = list(gpt_dir.glob(pat))
        if hits:
            return pat, hits
    return patterns[-1], []

gpt_pattern, gpt_files = _find_gpt_ckpts(_LORA_PREFIX)
try:
    gpt_files = sorted(gpt_files, key=lambda p: int(p.stem.split("-e")[1]))
except (IndexError, ValueError):
    gpt_files = sorted(gpt_files)
if not gpt_files:
    raise SystemExit(
        f"No GPT checkpoints matching '{gpt_pattern}' found in {gpt_dir}. "
        f"Set TTS_LORA_NAME or place a fine-tune in GPT_weights_v3/."
    )
GPT_PATH = str(gpt_files[-1])
print(f"[gpt-sovits-tts]   GPT ckpt: {Path(GPT_PATH).name}")
s1config = {
    "data": {"max_sec": 54, "pad_val": 1024},
    "model": {
        "vocab_size": 1025,
        "phoneme_vocab_size": 732,
        "embedding_dim": 512,
        "hidden_dim": 512,
        "head": 16,
        "linear_units": 2048,
        "n_layer": 24,
        "dropout": 0,
        "EOS": 1024,
        "random_bert": 0,
    },
}
gpt_model = Text2SemanticLightningModule(s1config, Path("."), is_train=False)
gpt_model.load_state_dict(
    torch.load(GPT_PATH, map_location="cpu", weights_only=False)["weight"], strict=False
)
gpt_model = gpt_model.half().to(DEVICE).eval()
gpt_model.model.infer_panel = gpt_model.model.infer_panel_naive

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import librosa  # noqa: E402

from GPT_SoVITS.text import cleaned_text_to_sequence  # noqa: E402
from GPT_SoVITS.text.cleaner import clean_text  # noqa: E402
from tools.my_utils import load_audio  # noqa: E402

SPEC_MIN, SPEC_MAX = -12, 2


def _norm_spec(x):
    return (x - SPEC_MIN) / (SPEC_MAX - SPEC_MIN) * 2 - 1


def _denorm_spec(x):
    return (x + 1) / 2 * (SPEC_MAX - SPEC_MIN) + SPEC_MIN


def _mel_v4(x):
    return mel_spectrogram_torch(
        x,
        n_fft=1280,
        win_size=1280,
        hop_size=320,
        num_mels=100,
        sampling_rate=32000,
        fmin=0,
        fmax=None,
        center=False,
    )


def _phoneme_ids(text, lang):
    if lang == "en":
        _ensure_nltk()  # GPT-SoVITS' English cleaner needs the nltk taggers
    phones, w2p, norm = clean_text(text, lang, "v2")
    return cleaned_text_to_sequence(phones, "v2"), w2p, norm


def _bert_for(phone_ids, w2p, norm, lang):
    if lang != "zh":
        return torch.zeros((1024, len(phone_ids)), dtype=torch.float32)
    tokenizer, bert = _get_bert()
    with torch.no_grad():
        inp = {k: v.to(DEVICE) for k, v in tokenizer(norm, return_tensors="pt").items()}
        out = bert(**inp, output_hidden_states=True)
        res = torch.cat(out["hidden_states"][-3:-2], -1)[0].cpu()[1:-1]
    feats = [res[i].repeat(w2p[i], 1) for i in range(len(w2p))]
    return torch.cat(feats, dim=0).T


def _ssl(wav_path):
    audio = load_audio(wav_path, 32000)
    audio16 = librosa.resample(audio, orig_sr=32000, target_sr=16000).astype(np.float32)
    t = torch.from_numpy(audio16).half().to(DEVICE)
    with torch.no_grad():
        return hubert.model(t.unsqueeze(0))["last_hidden_state"].transpose(1, 2)


def _ref_spec(wav_path):
    audio = load_audio(wav_path, hps.data.sampling_rate)
    return spectrogram_torch(
        torch.FloatTensor(audio).unsqueeze(0),
        hps.data.filter_length,
        hps.data.sampling_rate,
        hps.data.hop_length,
        hps.data.win_length,
        center=False,
    )


def _ref_mel(wav_path):
    audio = load_audio(wav_path, 32000)
    audio_t = torch.FloatTensor(audio).unsqueeze(0).to(DEVICE)
    return _norm_spec(_mel_v4(audio_t)).half()


# ---------------------------------------------------------------------------
# Reference cache.
# ---------------------------------------------------------------------------

REF_WAV = os.environ.get("TTS_REFERENCE_AUDIO")
REF_TEXT = os.environ.get("TTS_REFERENCE_TEXT")
REF_LANG = os.environ.get("TTS_REFERENCE_LANG", "ja")
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "default")
DEFAULT_LANGUAGE = os.environ.get("TTS_LANGUAGE", "ja")

if not REF_WAV or not REF_TEXT:
    raise SystemExit(
        "GPT-SoVITS zero-shot needs a reference clip. Set TTS_REFERENCE_AUDIO "
        "to a 3-10s WAV of the target voice and TTS_REFERENCE_TEXT to its "
        "transcript (or configure [avatar.tts] reference_audio/reference_text "
        "in companion.toml)."
    )

print(f"[gpt-sovits-tts] Caching reference: {Path(REF_WAV).name} ({REF_LANG})")
ref_ssl = _ssl(REF_WAV)
with torch.no_grad():
    ref_codes = vits.extract_latent(ref_ssl)
ref_semantic = ref_codes[0, 0, :]
ref_phone_ids, ref_w2p, ref_norm = _phoneme_ids(REF_TEXT, REF_LANG)
ref_spec = _ref_spec(REF_WAV).half().to(DEVICE)
ref_mel = _ref_mel(REF_WAV)


# ---------------------------------------------------------------------------
# Inference. Returns 48 kHz float32 mono waveform.
# ---------------------------------------------------------------------------


def synthesize(text: str, lang: str, top_k: int = 15, temperature: float = 1.0,
               sample_steps: int = 32) -> np.ndarray:
    phone_ids, w2p, norm = _phoneme_ids(text, lang)
    bert_feat = _bert_for(phone_ids, w2p, norm, lang)
    all_phone_ids = torch.LongTensor(phone_ids).unsqueeze(0).to(DEVICE)
    all_phone_lens = torch.LongTensor([len(phone_ids)]).to(DEVICE)
    all_bert = bert_feat.half().unsqueeze(0).to(DEVICE)
    prompt_sem = ref_semantic[: min(50, ref_semantic.shape[0])].unsqueeze(0).to(DEVICE)

    with torch.no_grad():
        gen = gpt_model.model.infer_panel(
            all_phone_ids, all_phone_lens, prompt_sem, all_bert,
            top_k=top_k, top_p=1, temperature=temperature,
            early_stop_num=hps.data.sampling_rate // hps.data.hop_length * 54,
        )
        y, idx = next(gen)
    pred_sem = y[0, -idx:].unsqueeze(0).unsqueeze(0).to(DEVICE)

    prompt_sem_full = ref_semantic.unsqueeze(0).unsqueeze(0).to(DEVICE)
    ref_phones_t = torch.LongTensor(ref_phone_ids).unsqueeze(0).to(DEVICE)
    with torch.no_grad():
        fea_ref, ge = vits.decode_encp(prompt_sem_full, ref_phones_t, ref_spec)
        fea_todo, ge = vits.decode_encp(pred_sem, all_phone_ids, ref_spec, ge, 1.0)

        T_min = min(ref_mel.shape[2], fea_ref.shape[2])
        mel2 = ref_mel[:, :, :T_min]
        fea_ref = fea_ref[:, :, :T_min]
        T_ref = 500    # vocoder_configs["T_ref"] for v4
        T_chunk = 1000  # vocoder_configs["T_chunk"] for v4
        if T_min > T_ref:
            mel2 = mel2[:, :, -T_ref:]
            fea_ref = fea_ref[:, :, -T_ref:]
            T_min = T_ref
        chunk_len = T_chunk - T_min

        cfm_results = []
        idx_pos = 0
        while True:
            chunk = fea_todo[:, :, idx_pos: idx_pos + chunk_len]
            if chunk.shape[-1] == 0:
                break
            idx_pos += chunk_len
            fea = torch.cat([fea_ref, chunk], 2).transpose(2, 1)
            cfm_res = vits.cfm.inference(
                fea, torch.LongTensor([fea.size(1)]).to(fea.device),
                mel2, sample_steps, inference_cfg_rate=0,
            )
            cfm_res = cfm_res[:, :, mel2.shape[2]:]
            mel2 = cfm_res[:, :, -T_min:]
            fea_ref = chunk[:, :, -T_min:]
            cfm_results.append(cfm_res)

        full_mel = torch.cat(cfm_results, 2)
        full_mel = _denorm_spec(full_mel)
        wav_gen = vocoder(full_mel)
        audio = wav_gen[0, 0].cpu().float().numpy()

    return audio  # 48 kHz mono float32


# ---------------------------------------------------------------------------
# HTTP server (waifu-companion avatar TTS port contract).
# ---------------------------------------------------------------------------


class TtsRequest(BaseModel):
    text: str
    language: str = DEFAULT_LANGUAGE
    voice: str | None = None
    speed: float = 1.0


SAMPLE_RATE = 48000

app = FastAPI(title="gpt-sovits-tts")


def _wav_bytes(audio_f32: np.ndarray, sr: int) -> bytes:
    audio_i16 = np.clip(audio_f32, -1.0, 1.0)
    audio_i16 = (audio_i16 * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio_i16.tobytes())
    return buf.getvalue()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "engine": "gpt-sovits-v4",
        "voices": [DEFAULT_VOICE],
        "languages": ["ja", "en", "zh"],
        "default_voice": DEFAULT_VOICE,
        "default_language": DEFAULT_LANGUAGE,
        "sample_rate": SAMPLE_RATE,
    }


@app.post("/tts")
async def tts(req: TtsRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text must not be empty")
    if req.language not in ("ja", "en", "zh"):
        raise HTTPException(400, f"unsupported language: {req.language}")

    t0 = time.time()
    try:
        audio = synthesize(req.text, req.language)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"synthesis failed: {e}") from e

    if abs(req.speed - 1.0) > 1e-3:
        audio = librosa.effects.time_stretch(audio, rate=req.speed)

    wav = _wav_bytes(audio, SAMPLE_RATE)
    duration = len(audio) / SAMPLE_RATE
    print(
        f"[gpt-sovits-tts] /tts lang={req.language} chars={len(req.text)} "
        f"audio={duration:.2f}s wall={time.time() - t0:.2f}s"
    )
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "X-Sample-Rate": str(SAMPLE_RATE),
            "X-Channels": "1",
            "X-Format": "wav",
        },
    )


# ---------------------------------------------------------------------------
# Graceful shutdown: companion-server hits POST /shutdown when the user
# closes the desktop app. Without this, killing this process via
# TerminateProcess (the previous behavior) skips PyTorch's atexit
# handlers and the CUDA driver keeps the per-process compute state
# warmed up for ~30–90s, manifesting as game stutter for the user.
#
# We do three things on shutdown:
#   1. Drop references to model tensors so PyTorch can release them.
#   2. Call torch.cuda.empty_cache() + synchronize() to flush DMA.
#   3. Exit. We use os._exit(0) because uvicorn catches SystemExit
#      and would otherwise hold the process alive for a graceful
#      TCP close — which delays the GPU release we just did.
# ---------------------------------------------------------------------------

_cleanup_done = False


def shutdown_cleanup() -> None:
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    try:
        # Drop model references so PyTorch can release VRAM.
        for var_name in (
            "t2s_model",
            "vits_model",
            "ssl_model",
            "bert_model",
            "tokenizer",
        ):
            try:
                globals().pop(var_name, None)
            except Exception:
                pass
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            # Best-effort: reset the device so the CUDA context is
            # fully torn down. Some PyTorch builds fail this — don't
            # propagate the error.
            try:
                torch.cuda.reset_peak_memory_stats()
            except Exception:
                pass
        print("[gpt-sovits-tts] CUDA cleanup done; exiting cleanly")
    except Exception as e:
        print(f"[gpt-sovits-tts] cleanup error (continuing): {e}")


atexit.register(shutdown_cleanup)


def _on_signal(sig, _frame):
    print(f"[gpt-sovits-tts] received signal {sig}; cleaning up")
    shutdown_cleanup()
    # _exit avoids the uvicorn-graceful-tcp-close stall.
    os._exit(0)


# Register signal handlers BEFORE uvicorn.run so they catch Ctrl+C
# (SIGINT) and Ctrl+Break (SIGBREAK on Windows). On Windows
# `python.exe foo.py` interprets Ctrl+C as SIGINT only when the
# console is the foreground; for headless launches via Tauri the
# /shutdown endpoint is the primary path.
signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _on_signal)


@app.post("/shutdown")
async def shutdown():
    """Graceful shutdown — used by companion-server's stop_server when
    the user closes the desktop app. Returns 202 immediately; the
    actual exit happens on a daemon thread after a short delay so
    this response can flush."""
    def _exit_soon():
        time.sleep(0.2)
        shutdown_cleanup()
        os._exit(0)

    threading.Thread(target=_exit_soon, daemon=True).start()
    return {"status": "shutting_down"}


if __name__ == "__main__":
    port = int(os.environ.get("TTS_PORT", "9880"))
    print(f"[gpt-sovits-tts] serving on http://127.0.0.1:{port}")
    print("[gpt-sovits-tts] PYTORCH_NO_CUDA_MEMORY_CACHING="
          + os.environ.get("PYTORCH_NO_CUDA_MEMORY_CACHING", ""))
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    finally:
        # Defense in depth: if uvicorn returns normally without going
        # through /shutdown or a signal, still run cleanup.
        shutdown_cleanup()
