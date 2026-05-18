"""Orchestrator — runs every rig in tts_tools/, captures output, emits a
summary.md + report.html, exits non-zero if any rig failed.

Usage:
    python -m tts_tools.run_all --quick                    # default sweep
    python -m tts_tools.run_all --full                     # incl. heavy rigs
    python -m tts_tools.run_all --suites lifecycle,chaos   # cherry-pick
    python -m tts_tools.run_all --list                     # show all rigs
    python -m tts_tools.run_all --no-rebuild               # trust target/release
    python -m tts_tools.run_all --no-report                # skip HTML
    python -m tts_tools.run_all --quick --bail-on-fail     # halt at first fail

Outputs land under tts_samples/run_all/<timestamp>/.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Import order matters — _test_helpers.REPO_ROOT must be available before we
# resolve paths below.
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
SAMPLES_ROOT = REPO_ROOT / "tts_samples" / "run_all"


# Suite registry. `quick=True` rigs land in `--quick`; `quick=False` only in
# `--full` (or `--suites name`). Keep this in sync with TESTING-SOP.md.
@dataclass(frozen=True)
class Suite:
    name: str          # short name, used for --suites and result keys
    path: str          # filename inside tts_tools/, sans .py
    layer: str         # docs-anchor layer label (L3, L5, …)
    quick: bool        # included in `--quick`
    gpu: bool = False  # requires GPU; skipped when --no-gpu

SUITES: list[Suite] = [
    # L3 — wire contracts
    Suite("tts_e2e",          "test_server_e2e",            "L3",       quick=True),
    Suite("nmt_e2e",          "test_nmt_e2e",               "L3",       quick=True),
    # L4 — multi-service audio (heavy, GPU)
    Suite("audio_integrity",  "test_audio_integrity",       "L4",       quick=False, gpu=True),
    # L5 — lifecycle
    Suite("lifecycle",        "test_lifecycle",             "L5",       quick=True),
    # L6a/L6d/L6e — backend coverage
    Suite("backend_api",      "test_backend_api",           "L6a",      quick=True),
    Suite("backend_extended", "test_backend_api_extended",  "L6d",      quick=True),
    Suite("integration_full", "test_integration_full",      "L6e",      quick=True),
    # L6f / L6f-sec / L6f-prop — chaos + security + fuzz
    Suite("chaos",            "test_chaos",                 "L6f",      quick=True),
    Suite("security",         "test_security",              "L6f-sec",  quick=True),
    Suite("state_fuzz",       "test_state_transitions",     "L6f-prop", quick=False),
    # L6sse — SSE bridge
    Suite("sse_bridge",       "test_sse_bridge",            "L6sse",    quick=True),
    # L6b/g/flows — frontend coverage (requires playwright)
    Suite("frontend_e2e",     "test_frontend_e2e",          "L6b",      quick=True),
    Suite("frontend_extended","test_frontend_e2e_extended", "L6g",      quick=False),
    Suite("frontend_flows",   "test_frontend_flows",        "L6flows",  quick=False),
    # L6h / L6perf — benches
    Suite("bench_latency",    "bench_latency",              "L6h",      quick=True),
    Suite("perf_regression",  "test_perf_regression",       "L6perf",   quick=False),
    # New layers (P1-P4)
    Suite("ux_critic",        "test_ux_critic",             "P1",       quick=False),
    Suite("string_lints",     "test_user_facing_strings",   "P3",       quick=True),
    Suite("stress_walker",    "test_real_app_stress",       "P4",       quick=False),
    # TTS audio quality: 30 ASR-validated inputs, catches truncation /
    # bump / runaway / flat-emotion. GPU required. Prefers sidecar wire
    # if up, falls back to engine-direct.
    Suite("tts_audio_quality","test_tts_audio_quality",     "L_tts",    quick=False, gpu=True),
    # Long-input TTS stutter: a single 200+ char JA paragraph, ASR-verify
    # for consecutive 4-gram repetition (the failure shape SBV2 exhibits
    # at >~30s of synth when fed unsplit). Guards the engine-side
    # sentence-split defence in tts_lab/sbv2_lab/sbv2_sidecar.py.
    Suite("tts_long_input",   "test_tts_long_input",        "L_tts_long", quick=True, gpu=True),
    # NMT must preserve \n\n paragraph breaks. Without this, the
    # companion's paragraph-wise TTS streamer collapses to single-shot
    # synth and the user only hears one paragraph of a multi-paragraph
    # reply. Caught on 2026-05-18 — NLLB was stripping all whitespace.
    Suite("nmt_paragraphs",   "test_nmt_paragraphs",        "L3-para",  quick=True),
    # L7 — Tauri shell smoke: builds + launches the actual desktop app,
    # attaches via CDP, asserts the WebView renders the nav + #root
    # has children + backend reachable. Catches the blank-window class
    # that 130+ cargo tests and L1-L6 miss. ~90s on a warm cache.
    Suite("tauri_shell",      "test_tauri_shell",           "L7",       quick=True),
]


@dataclass
class Outcome:
    suite: Suite
    status: str             # "PASS" / "FAIL" / "SKIP" / "MISSING"
    duration_s: float
    log_path: Path
    tail: str = ""          # last ~40 lines if failed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the testing rigs.")
    p.add_argument("--quick", action="store_true", help="run the quick subset (default if no mode flag)")
    p.add_argument("--full", action="store_true", help="run every rig including heavy ones")
    p.add_argument("--suites", type=str, default="", help="comma-separated suite names; overrides --quick/--full")
    p.add_argument("--list", action="store_true", help="list available suites and exit")
    p.add_argument("--no-rebuild", action="store_true", help="skip `cargo build --release` step")
    p.add_argument("--no-gpu", action="store_true", help="skip GPU-required suites")
    p.add_argument("--no-report", action="store_true", help="skip HTML report rendering")
    p.add_argument("--bail-on-fail", action="store_true", help="halt at first failing suite")
    return p.parse_args()


def select_suites(args: argparse.Namespace) -> list[Suite]:
    if args.suites:
        names = {s.strip() for s in args.suites.split(",") if s.strip()}
        unknown = names - {s.name for s in SUITES}
        if unknown:
            raise SystemExit(f"unknown suite name(s): {sorted(unknown)}")
        return [s for s in SUITES if s.name in names]
    if args.full:
        return list(SUITES)
    return [s for s in SUITES if s.quick]


def ensure_built(quiet: bool = False) -> None:
    """Run `cargo build --release -p companion-server` so L5 has a fresh
    binary. The SOP is explicit: stale binaries are how lifecycle bugs hide.
    """
    if not quiet:
        print("[run_all] building companion-server (release) …", flush=True)
    r = subprocess.run(
        ["cargo", "build", "--release", "-p", "companion-server"],
        cwd=str(REPO_ROOT),
    )
    if r.returncode != 0:
        raise SystemExit(f"FAIL: cargo build returned {r.returncode}")


def run_suite(suite: Suite, log_dir: Path, python: str) -> Outcome:
    """Run one rig as `python -m tts_tools.<path>`. Captures stdout+stderr to
    a per-suite log. Returns an Outcome with status + duration + tail.
    """
    log_path = log_dir / f"{suite.name}.log"
    script = THIS_DIR / f"{suite.path}.py"
    if not script.exists():
        return Outcome(suite, "MISSING", 0.0, log_path, tail=f"rig not built yet: {script}")
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"# suite={suite.name}  layer={suite.layer}  rig={script}\n\n")
        f.flush()
        proc = subprocess.Popen(
            [python, "-m", f"tts_tools.{suite.path}"],
            stdout=f, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT),
        )
        rc = proc.wait()
    duration = time.monotonic() - t0
    status = "PASS" if rc == 0 else "FAIL"
    tail = ""
    if status == "FAIL":
        tail = _tail_file(log_path, n=40)
    return Outcome(suite, status, duration, log_path, tail=tail)


def _tail_file(path: Path, n: int = 40) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def write_summary(outcomes: list[Outcome], summary_md: Path, total_s: float) -> None:
    lines = [
        "# run_all summary",
        "",
        f"- timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- total duration: {total_s:.1f}s",
        f"- suites run: {len(outcomes)}",
        f"- pass: {sum(1 for o in outcomes if o.status == 'PASS')}",
        f"- fail: {sum(1 for o in outcomes if o.status == 'FAIL')}",
        f"- missing: {sum(1 for o in outcomes if o.status == 'MISSING')}",
        "",
        "| suite | layer | status | duration | log |",
        "|---|---|---|---|---|",
    ]
    for o in outcomes:
        rel_log = o.log_path.relative_to(summary_md.parent)
        lines.append(
            f"| {o.suite.name} | {o.suite.layer} | {o.status} | {o.duration_s:.1f}s | "
            f"[{rel_log}]({rel_log}) |"
        )
    # Add failing tails inline.
    failed = [o for o in outcomes if o.status == "FAIL"]
    if failed:
        lines.append("")
        lines.append("## failure tails (last 40 lines each)")
        for o in failed:
            lines.append("")
            lines.append(f"### {o.suite.name} ({o.suite.layer})")
            lines.append("```")
            lines.append(o.tail.rstrip())
            lines.append("```")
    summary_md.write_text("\n".join(lines), encoding="utf-8")


def render_html_report(summary_md: Path) -> Path:
    """Minimal self-contained HTML next to summary.md. The richer render is
    in tts_tools/render_report.py — this is the fallback.
    """
    html_path = summary_md.parent / "report.html"
    md = summary_md.read_text(encoding="utf-8")
    # Trivial md→html (good enough for a CI artifact).
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>run_all report</title>"
        "<style>body{font:14px system-ui;max-width:960px;margin:2em auto;padding:0 1em}"
        "table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border:1px solid #ddd}"
        "code,pre{background:#f6f8fa;padding:2px 6px;border-radius:4px;font:12px monospace}"
        "pre{padding:1em;overflow-x:auto}</style></head><body>"
        f"<pre>{_md_escape(md)}</pre></body></html>"
    )
    html_path.write_text(html, encoding="utf-8")
    return html_path


def _md_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    args = parse_args()

    if args.list:
        print("Available suites:")
        for s in SUITES:
            tag = "[quick]" if s.quick else "[full]"
            gpu = " [gpu]" if s.gpu else ""
            print(f"  {s.name:<22s} {s.layer:<10s} {tag}{gpu}  → {s.path}.py")
        return 0

    # Mode: --suites > --full > --quick (default)
    if not (args.quick or args.full or args.suites):
        args.quick = True

    suites = select_suites(args)
    if args.no_gpu:
        suites = [s for s in suites if not s.gpu]
    if not suites:
        print("no suites selected", flush=True)
        return 0

    # Ensure we have a binary unless told to skip.
    if not args.no_rebuild:
        ensure_built()

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_dir = SAMPLES_ROOT / timestamp
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"[run_all] running {len(suites)} suite(s) → {log_dir}")
    print()

    # Resolve python here so every suite uses the same interpreter.
    python = os.environ.get("COMPANION_TTS_PYTHON") or sys.executable

    outcomes: list[Outcome] = []
    t0 = time.monotonic()
    for s in suites:
        print(f"  [run] {s.name:<22s} ({s.layer}) …", flush=True, end="")
        o = run_suite(s, log_dir, python)
        outcomes.append(o)
        marker = {"PASS": "PASS", "FAIL": "FAIL", "MISSING": "MISS", "SKIP": "SKIP"}[o.status]
        print(f" {marker}  {o.duration_s:.1f}s", flush=True)
        if args.bail_on_fail and o.status == "FAIL":
            print(f"  → bail-on-fail; halting", flush=True)
            break
    total = time.monotonic() - t0

    summary_md = log_dir / "summary.md"
    write_summary(outcomes, summary_md, total)
    print()
    print(f"[run_all] summary: {summary_md}")
    if not args.no_report:
        html = render_html_report(summary_md)
        print(f"[run_all] report:  {html}")

    n_fail = sum(1 for o in outcomes if o.status == "FAIL")
    n_miss = sum(1 for o in outcomes if o.status == "MISSING")
    print(f"[run_all] {len(outcomes)-n_fail-n_miss}/{len(outcomes)} green, "
          f"{n_fail} failed, {n_miss} missing  ({total:.1f}s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
