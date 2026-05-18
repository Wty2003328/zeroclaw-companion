"""L6report — Richer HTML report renderer (not a test rig).

Turns a `tts_samples/run_all/<timestamp>/summary.md` (produced by
run_all.py) into a self-contained `report.html` next to it.

What the HTML includes:
  * Per-suite cards with PASS/FAIL badges + expandable <details> log
    panes containing the full per-suite log content.
  * Histogram of suite durations (inline SVG, horizontal bars).
  * Per-metric time-series sparklines (inline SVG) when
    tts_samples/bench/ has CSVs.
  * Visual baselines thumbnail grid (inline base64-PNGs) when
    tts_samples/visual_baselines/ exists.

No external CSS/JS — fully self-contained. System-ui font, sane spacing.

CLI:
    python -m tts_tools.render_report                       # latest run
    python -m tts_tools.render_report tts_samples/run_all/<timestamp>
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import re
import sys
from pathlib import Path
from typing import Optional

from tts_tools._test_helpers import REPO_ROOT


# ── Pure helpers ─────────────────────────────────────────────────────
def _parse_summary(md: str) -> list[dict]:
    """Best-effort parse of the markdown table run_all writes. Returns
    a list of {suite, layer, status, duration, log} dicts.

    The table shape (from run_all.write_summary):
        | suite | layer | status | duration | log |
        |---|---|---|---|---|
        | name | L3 | PASS | 1.2s | [foo.log](foo.log) |
    """
    rows: list[dict] = []
    in_table = False
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        # Skip header / separator rows.
        if cells[0] == "suite" and len(cells) >= 4:
            in_table = True
            continue
        if cells[0].startswith("-") or set("".join(cells)) <= set("- :"):
            continue
        if not in_table:
            continue
        if len(cells) < 5:
            continue
        # Extract log filename from markdown link `[name](name)`.
        log_cell = cells[4]
        m = re.search(r"\(([^)]+)\)", log_cell)
        log_name = m.group(1) if m else log_cell
        rows.append({
            "suite": cells[0],
            "layer": cells[1],
            "status": cells[2],
            "duration": cells[3],
            "log": log_name,
        })
    return rows


def _parse_duration(s: str) -> float:
    """'1.2s' → 1.2, '30s' → 30.0, garbage → 0.0."""
    m = re.match(r"^\s*([0-9.]+)", s)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def _resolve_run_dir(arg: Optional[str]) -> Path:
    """Pick the run directory. Arg may be absolute, relative, or None.
    None → latest under tts_samples/run_all/.
    """
    if arg:
        p = Path(arg)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        if not p.is_dir():
            raise SystemExit(f"FAIL: not a directory: {p}")
        if not (p / "summary.md").exists():
            raise SystemExit(f"FAIL: no summary.md in {p}")
        return p
    root = REPO_ROOT / "tts_samples" / "run_all"
    if not root.exists():
        raise SystemExit(f"FAIL: no run_all dir yet at {root}; run `python -m tts_tools.run_all` first")
    candidates = [c for c in root.iterdir() if c.is_dir() and (c / "summary.md").exists()]
    if not candidates:
        raise SystemExit(f"FAIL: no summary.md under {root}; run `python -m tts_tools.run_all` first")
    latest = max(candidates, key=lambda c: c.stat().st_mtime)
    return latest


# ── SVG primitives ───────────────────────────────────────────────────
def _svg_bar_chart(rows: list[dict], width: int = 720, row_h: int = 24) -> str:
    """Horizontal bar chart of suite durations."""
    if not rows:
        return ""
    durations = [(_parse_duration(r["duration"]), r) for r in rows]
    max_d = max((d for d, _ in durations), default=1.0) or 1.0
    label_w = 220
    bar_max = width - label_w - 80
    height = len(durations) * row_h + 20
    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Suite durations">',
        '<style>.lbl{font:12px system-ui;fill:#333} '
        '.val{font:12px system-ui;fill:#555} '
        '.bar{fill:#3b82f6} .bar-fail{fill:#dc2626} .bar-miss{fill:#94a3b8}</style>',
    ]
    for i, (d, row) in enumerate(durations):
        y = i * row_h + 14
        bar_w = max(2, int((d / max_d) * bar_max))
        status = row["status"].upper()
        cls = "bar"
        if status in ("FAIL",):
            cls = "bar-fail"
        elif status in ("MISSING", "SKIP"):
            cls = "bar-miss"
        label = html.escape(f'{row["suite"]} ({row["layer"]})')
        parts.append(
            f'<text class="lbl" x="6" y="{y + 4}">{label}</text>'
            f'<rect class="{cls}" x="{label_w}" y="{y - 8}" width="{bar_w}" height="16" rx="2"/>'
            f'<text class="val" x="{label_w + bar_w + 6}" y="{y + 4}">{d:.1f}s</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_sparkline(values: list[float], width: int = 220, height: int = 40,
                   color: str = "#3b82f6") -> str:
    """Single-line sparkline for a metric's history."""
    if not values:
        return ""
    if len(values) == 1:
        values = values + values  # duplicate so we get a flat line
    lo = min(values)
    hi = max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * (width - 4) + 2
        y = height - 2 - ((v - lo) / span) * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    pts_str = " ".join(pts)
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="sparkline">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'points="{pts_str}"/>'
        f'</svg>'
    )


# ── Data gatherers ───────────────────────────────────────────────────
def _gather_bench_history() -> dict[str, list[float]]:
    """Walk tts_samples/bench/<ts>/results.csv; build per-metric series
    ordered by directory name (== timestamp, lexicographic ≈ chronological).
    """
    bench_dir = REPO_ROOT / "tts_samples" / "bench"
    history: dict[str, list[float]] = {}
    if not bench_dir.exists():
        return history
    for sub in sorted(bench_dir.iterdir(), key=lambda p: p.name):
        csv_path = sub / "results.csv"
        if not csv_path.exists():
            continue
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    metric = row.get("metric")
                    val_s = row.get("value_ms")
                    if not metric or not val_s:
                        continue
                    try:
                        history.setdefault(metric, []).append(float(val_s))
                    except ValueError:
                        continue
        except Exception:
            continue
    return history


def _gather_visual_baselines() -> list[tuple[str, bytes]]:
    """Return [(name, png_bytes), ...] for every PNG under
    tts_samples/visual_baselines/."""
    vb = REPO_ROOT / "tts_samples" / "visual_baselines"
    out: list[tuple[str, bytes]] = []
    if not vb.exists():
        return out
    for png in sorted(vb.rglob("*.png")):
        try:
            out.append((str(png.relative_to(vb)), png.read_bytes()))
        except Exception:
            continue
    return out


def _read_log(run_dir: Path, log_name: str) -> str:
    """Read a per-suite log; cap at 200 KB to keep the HTML viewer-sized."""
    p = run_dir / log_name
    if not p.exists():
        return f"(log not found: {log_name})"
    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"(read failed: {e})"
    if len(body) > 200_000:
        body = body[-200_000:]
        body = f"… (truncated; showing last 200 KB)\n{body}"
    return body


# ── Renderer ─────────────────────────────────────────────────────────
def _status_badge(status: str) -> str:
    status_u = status.upper()
    color_map = {
        "PASS": ("#065f46", "#d1fae5"),
        "FAIL": ("#7f1d1d", "#fee2e2"),
        "SKIP": ("#374151", "#e5e7eb"),
        "MISSING": ("#374151", "#e5e7eb"),
    }
    fg, bg = color_map.get(status_u, ("#1e3a8a", "#dbeafe"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:10px;font:600 11px system-ui;letter-spacing:.04em;">'
        f'{html.escape(status_u)}</span>'
    )


def render(run_dir: Path) -> Path:
    summary_md_path = run_dir / "summary.md"
    summary_md = summary_md_path.read_text(encoding="utf-8")
    rows = _parse_summary(summary_md)

    # Suite cards.
    card_html_parts: list[str] = []
    for row in rows:
        log_body = _read_log(run_dir, row["log"])
        badge = _status_badge(row["status"])
        card_html_parts.append(
            '<section class="suite-card">'
            f'<header><span class="suite-name">{html.escape(row["suite"])}</span>'
            f'<span class="suite-layer">{html.escape(row["layer"])}</span>'
            f'{badge}'
            f'<span class="suite-dur">{html.escape(row["duration"])}</span>'
            '</header>'
            '<details>'
            f'<summary>Show log ({html.escape(row["log"])})</summary>'
            f'<pre class="log">{html.escape(log_body)}</pre>'
            '</details>'
            '</section>'
        )

    # Bench sparklines.
    bench = _gather_bench_history()
    bench_section = ""
    if bench:
        items: list[str] = []
        for metric, series in sorted(bench.items()):
            if len(series) < 1:
                continue
            spark = _svg_sparkline(series)
            last = series[-1] if series else 0.0
            items.append(
                '<div class="bench-item">'
                f'<div class="bench-metric">{html.escape(metric)}</div>'
                f'<div class="bench-spark">{spark}</div>'
                f'<div class="bench-last">latest: {last:.0f} ms '
                f'(n={len(series)})</div>'
                '</div>'
            )
        if items:
            bench_section = (
                '<section class="bench-section">'
                '<h2>Bench history</h2>'
                '<div class="bench-grid">' + "".join(items) + '</div>'
                '</section>'
            )

    # Visual baselines.
    visuals = _gather_visual_baselines()
    visuals_section = ""
    if visuals:
        thumbs: list[str] = []
        for name, png in visuals:
            b64 = base64.b64encode(png).decode("ascii")
            thumbs.append(
                '<figure class="thumb">'
                f'<img src="data:image/png;base64,{b64}" alt="{html.escape(name)}"/>'
                f'<figcaption>{html.escape(name)}</figcaption>'
                '</figure>'
            )
        visuals_section = (
            '<section class="visuals-section">'
            '<h2>Visual baselines</h2>'
            f'<div class="thumb-grid">{"".join(thumbs)}</div>'
            '</section>'
        )

    duration_chart = _svg_bar_chart(rows)

    # ── Compose HTML ─────────────────────────────────────────────
    css = """
    body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
        max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; }
    h1 { margin-top: 0; font-size: 1.5rem; }
    h2 { margin-top: 1.8em; font-size: 1.15rem; border-bottom: 1px solid #e5e7eb;
        padding-bottom: 0.3em; }
    .summary-md { background: #f9fafb; border-left: 4px solid #cbd5e1;
        padding: 0.8em 1.2em; border-radius: 6px;
        font: 12px/1.5 ui-monospace, "SF Mono", Menlo, monospace;
        white-space: pre-wrap; overflow-x: auto; }
    .suite-card { background: #fff; border: 1px solid #e5e7eb;
        border-radius: 8px; padding: 0.6em 0.9em; margin: 0.5em 0; }
    .suite-card header { display: flex; align-items: center; gap: 0.8em;
        flex-wrap: wrap; }
    .suite-name { font: 600 14px system-ui; }
    .suite-layer { font: 11px ui-monospace, monospace; color: #6b7280;
        background: #f3f4f6; padding: 1px 6px; border-radius: 4px; }
    .suite-dur { margin-left: auto; color: #6b7280; font-variant-numeric: tabular-nums; }
    .suite-card details { margin-top: 0.5em; }
    .suite-card summary { cursor: pointer; color: #2563eb; font-size: 12px; }
    .log { background: #0f172a; color: #e2e8f0; padding: 1em; border-radius: 6px;
        overflow-x: auto; font: 11px/1.4 ui-monospace, monospace; max-height: 480px;
        margin: 0.5em 0 0; }
    .bench-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 0.8em; }
    .bench-item { background: #fff; border: 1px solid #e5e7eb; border-radius: 6px;
        padding: 0.6em 0.8em; }
    .bench-metric { font: 600 12px ui-monospace, monospace; color: #1f2937; }
    .bench-last { font: 11px system-ui; color: #6b7280; }
    .thumb-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 0.8em; }
    .thumb { margin: 0; }
    .thumb img { width: 100%; border: 1px solid #e5e7eb; border-radius: 4px;
        display: block; }
    .thumb figcaption { font: 11px ui-monospace, monospace; color: #6b7280;
        margin-top: 0.3em; word-break: break-all; }
    """

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en"><head><meta charset="utf-8">')
    parts.append(f"<title>run_all — {html.escape(run_dir.name)}</title>")
    parts.append(f"<style>{css}</style>")
    parts.append("</head><body>")
    parts.append(f"<h1>run_all report — <code>{html.escape(run_dir.name)}</code></h1>")
    parts.append(f'<pre class="summary-md">{html.escape(summary_md)}</pre>')

    if duration_chart:
        parts.append('<section><h2>Suite durations</h2>')
        parts.append(duration_chart)
        parts.append('</section>')

    parts.append('<section><h2>Suite results</h2>')
    parts.extend(card_html_parts)
    parts.append('</section>')

    if bench_section:
        parts.append(bench_section)
    if visuals_section:
        parts.append(visuals_section)

    parts.append("</body></html>")

    html_path = run_dir / "report.html"
    html_path.write_text("".join(parts), encoding="utf-8")
    return html_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render an HTML report from a run_all output.")
    p.add_argument("run_dir", nargs="?", default=None, help="run_all/<timestamp> dir; latest if omitted.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    run_dir = _resolve_run_dir(args.run_dir)
    html_path = render(run_dir)
    print(f"  [ok] report → {html_path}")


if __name__ == "__main__":
    main()
