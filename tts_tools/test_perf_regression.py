"""L6perf — Performance regression guard.

Reads every CSV produced by `bench_latency` under tts_samples/bench/,
computes the per-metric median across that history, then runs a fresh
bench and fails if any metric exceeds `tolerance * historical_median`
(default 1.5×). Skips automatically when fewer than 2 historical runs
exist — there's nothing to regress against on first use.

Run directly:
    python -m tts_tools.test_perf_regression
    python -m tts_tools.test_perf_regression --tolerance 2.0
"""
from __future__ import annotations

import argparse
import csv
import statistics
import tempfile
import time
from pathlib import Path

from tts_tools._test_helpers import (
    CheckReporter,
    PORT_COMPANION,
    PORT_CONTROL,
    PORT_MOCK_ZEROCLAW,
    PORT_NMT,
    PORT_TTS,
    REPO_ROOT,
    managed_procs,
    python_exe,
    require_ports_free,
    spawn,
    spawn_companion_server,
    wait_for_port,
    wait_for_url,
)
from tts_tools._rig_shim import PORT_ZC_SHIM, start_shims, wait_for_shims
from tts_tools.bench_latency import _build_config, run_bench


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Perf-regression guard.")
    p.add_argument(
        "--tolerance",
        type=float,
        default=1.5,
        help="Fail when fresh > tolerance * historical_median (default 1.5).",
    )
    return p.parse_args()


def _load_history(bench_dir: Path) -> dict[str, list[float]]:
    """Walk every results.csv under bench_dir; collect per-metric samples.
    Each historical run contributes one sample per metric — we don't
    aggregate across rows within a CSV because each CSV is already one
    bench run's results.
    """
    history: dict[str, list[float]] = {}
    if not bench_dir.exists():
        return history
    for csv_path in sorted(bench_dir.glob("*/results.csv")):
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    metric = row.get("metric")
                    val_s = row.get("value_ms")
                    if not metric or not val_s:
                        continue
                    try:
                        v = float(val_s)
                    except ValueError:
                        continue
                    history.setdefault(metric, []).append(v)
        except Exception:
            # Skip malformed CSVs rather than fail the whole rig.
            continue
    return history


def main() -> None:
    args = _parse_args()
    r = CheckReporter("test_perf_regression")
    r.info(f"tolerance = {args.tolerance}x historical median")

    bench_dir = REPO_ROOT / "tts_samples" / "bench"
    history = _load_history(bench_dir)
    # Count distinct historical runs — len of any metric's series gives
    # us that (every run contributes one sample per metric it measured).
    run_count = max((len(v) for v in history.values()), default=0)
    r.info(f"historical runs found: {run_count}")

    if run_count < 2:
        r.info("fewer than 2 historical runs — skipping regression check")
        r.check("regression guard (skipped — not enough history)", True)
        r.summary_or_exit()

    require_ports_free(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        PORT_ZC_SHIM,
    )

    scratch = Path(tempfile.mkdtemp(prefix="companion-perfregress-"))
    log_dir = scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fresh: dict[str, float] = {}
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
        r.info("running fresh bench …")
        fresh = run_bench(r, base)

    # Compare per-metric.
    regressions: list[tuple[str, float, float, float]] = []  # metric, fresh, median, threshold
    compared = 0
    for metric, fresh_val in sorted(fresh.items()):
        samples = history.get(metric, [])
        if len(samples) < 2:
            r.info(f"{metric}: < 2 history points — not compared")
            continue
        med = statistics.median(samples)
        threshold = med * args.tolerance
        compared += 1
        ok = fresh_val <= threshold
        detail = f"fresh={fresh_val:.0f}ms median={med:.0f}ms threshold={threshold:.0f}ms"
        r.check(f"{metric} within {args.tolerance}x of historical median", ok, detail)
        if not ok:
            regressions.append((metric, fresh_val, med, threshold))

    if compared == 0:
        r.check(
            "regression guard found something to compare",
            False,
            "no metric had >= 2 historical samples",
        )

    if regressions:
        r.info("REGRESSIONS:")
        for m, fv, md, th in regressions:
            r.info(f"  - {m}: {fv:.0f}ms > {th:.0f}ms (median {md:.0f}ms)")

    r.summary_or_exit()


if __name__ == "__main__":
    main()
