"""Shared helpers for the testing rigs.

Conventions every rig follows:
  * Print one PASS/FAIL line per check.
  * Exit 0 on full pass; non-zero on any failure.
  * Never run if a required port is already bound (would test stale state).
  * Tear down spawned subprocesses on exit, including on exception.

Ports (centralized so a single rig change here updates every rig):
  9880  TTS sidecar             — production wire (or mock with control)
  9881  NMT sidecar             — translation
  9883  Mock-stack control plane — `POST /_set` toggles failure modes
  9181  companion-server         — primary HTTP/WS
  19182 companion-server (alt)   — secondary for multi-process rigs
  42617 mock zeroclaw            — upstream agent stub
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

# Standard port assignments. Keep these in sync with TESTING-SOP.md.
PORT_TTS = 9880
PORT_NMT = 9881
PORT_CONTROL = 9883
PORT_COMPANION = 9181
PORT_COMPANION_ALT = 19182
PORT_MOCK_ZEROCLAW = 42617

# Repo root: this file is at <repo>/tts_tools/_test_helpers.py.
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------- #
# Port plumbing
# ---------------------------------------------------------------------------- #
def is_port_free(port: int) -> bool:
    """Return True if no socket is bound to 127.0.0.1:port."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def require_ports_free(*ports: int) -> None:
    """Raise SystemExit if any port is already bound — never run against a
    stale process. Lists the bound ports so the operator knows what to stop.
    """
    bound = [p for p in ports if not is_port_free(p)]
    if bound:
        raise SystemExit(
            f"FAIL: port(s) already bound — refusing to test against stale "
            f"processes: {bound}. Stop them and re-run."
        )


def wait_for_url(url: str, timeout_s: float = 30.0, interval_s: float = 0.3) -> bool:
    """Poll `url` until a 200 lands or deadline expires. Returns True/False."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, socket.error, ConnectionError):
            pass
        time.sleep(interval_s)
    return False


def wait_for_port(port: int, timeout_s: float = 30.0) -> bool:
    """Poll until a TCP connect to 127.0.0.1:port succeeds."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not is_port_free(port):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------- #
# HTTP convenience (uses urllib so the rigs don't need `requests`)
# ---------------------------------------------------------------------------- #
def http_get(url: str, timeout: float = 5.0) -> tuple[int, bytes, dict[str, str]]:
    """GET → (status, body, headers). Never raises; 5xx returns the body."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def http_post_json(url: str, payload: Any, timeout: float = 10.0) -> tuple[int, bytes, dict[str, str]]:
    """POST JSON → (status, body, headers). Never raises; 5xx returns the body."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def http_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    """GET → JSON dict, or None on any error."""
    status, body, _ = http_get(url, timeout=timeout)
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------- #
# Subprocess management
# ---------------------------------------------------------------------------- #
@dataclass
class ManagedProc:
    """A Popen with a label, for tracing in summary output."""
    label: str
    proc: subprocess.Popen
    port: Optional[int] = None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, grace_s: float = 5.0) -> None:
        if not self.alive():
            return
        # Try graceful shutdown over HTTP first (every sidecar honors /shutdown)
        if self.port:
            try:
                http_post_json(f"http://127.0.0.1:{self.port}/shutdown", {}, timeout=1.0)
            except Exception:
                pass
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


@contextlib.contextmanager
def managed_procs() -> Iterator[list[ManagedProc]]:
    """Tracks spawned processes; stops them all on context exit (incl. exception)."""
    procs: list[ManagedProc] = []
    try:
        yield procs
    finally:
        # Reverse order so dependents go down before their deps.
        for mp in reversed(procs):
            mp.stop()


def spawn(label: str, args: list[str], port: Optional[int] = None,
          env: Optional[dict] = None, cwd: Optional[Path] = None,
          log_path: Optional[Path] = None) -> ManagedProc:
    """Spawn a subprocess with stdout+stderr captured to `log_path` (or DEVNULL).
    Returns a ManagedProc — caller should add it to a managed_procs() list."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode — useful if a rig restarts the same proc several times.
        out = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            args, stdout=out, stderr=subprocess.STDOUT,
            env=full_env, cwd=str(cwd) if cwd else None,
        )
    else:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=full_env, cwd=str(cwd) if cwd else None,
        )
    return ManagedProc(label=label, proc=proc, port=port)


# ---------------------------------------------------------------------------- #
# Companion-server spawn
# ---------------------------------------------------------------------------- #
def companion_server_binary() -> Path:
    """Resolve target/release/companion-server.exe. Built via `cargo build
    --release -p companion-server`. Falls back to debug if release missing.
    """
    rel = REPO_ROOT / "target" / "release" / "companion-server.exe"
    if rel.exists():
        return rel
    dbg = REPO_ROOT / "target" / "debug" / "companion-server.exe"
    if dbg.exists():
        return dbg
    raise SystemExit(
        f"FAIL: companion-server.exe not found at {rel} or {dbg}. "
        f"Run: cargo build --release -p companion-server"
    )


def spawn_companion_server(config: dict, port: int = PORT_COMPANION,
                           log_dir: Optional[Path] = None,
                           scratch_dir: Optional[Path] = None) -> tuple[ManagedProc, Path]:
    """Spawn companion-server with a temp config. Returns (proc, config_path).
    config: dict written as TOML; caller's responsibility to include valid keys.
    scratch_dir: COMPANION_DATA_DIR for character/pulse persistence (temp by default).
    """
    binary = companion_server_binary()
    scratch = scratch_dir or Path(tempfile.mkdtemp(prefix="companion-test-"))
    log_path = log_dir / "companion-server.log" if log_dir else None
    cfg_path = scratch / "test_config.toml"
    cfg_path.write_text(_dict_to_toml(config), encoding="utf-8")

    env = {
        "COMPANION_CONFIG": str(cfg_path),
        "COMPANION_DATA_DIR": str(scratch),
        "RUST_LOG": os.environ.get("RUST_LOG", "info"),
        "COMPANION_PORT": str(port),
    }
    mp = spawn(
        "companion-server", [str(binary)],
        port=port, env=env, log_path=log_path,
    )
    return mp, cfg_path


def _dict_to_toml(d: dict) -> str:
    """Minimal dict→TOML for test configs. Handles strings, ints, floats,
    bools, and arbitrarily-deep sub-tables. Not a TOML library — just
    enough for tests. Walks the tree depth-first, emitting scalars before
    descending into sub-tables at each level (the standard TOML ordering).
    """
    lines: list[str] = []

    def emit(table: dict, prefix: str) -> None:
        # Scalars first (must precede any [sub-table] headers at this level).
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(f"{k} = {_toml_value(v)}")
        # Then descend into sub-tables.
        for k, v in table.items():
            if isinstance(v, dict):
                lines.append("")
                qualified = f"{prefix}.{k}" if prefix else k
                lines.append(f"[{qualified}]")
                emit(v, qualified)

    emit(d, "")
    return "\n".join(lines) + "\n"


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # TOML basic strings need escaped backslashes and quotes.
        s = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ValueError(f"unsupported toml value type: {type(v)}")


# ---------------------------------------------------------------------------- #
# Mock control plane convenience
# ---------------------------------------------------------------------------- #
def mock_set(**knobs: Any) -> bool:
    """Tell the mock-stack control plane (port 9883) to flip failure modes.
    Knobs: tts_dead, tts_status, nmt_dead, nmt_slow, zc_dead, zc_status,
    speech_dead, etc. See _mock_stack.py for the full list.
    """
    url = f"http://127.0.0.1:{PORT_CONTROL}/_set"
    status, _, _ = http_post_json(url, knobs, timeout=2.0)
    return status == 200


def mock_clear() -> bool:
    """Reset every failure-mode knob to default (everything healthy)."""
    url = f"http://127.0.0.1:{PORT_CONTROL}/_clear"
    status, _, _ = http_post_json(url, {}, timeout=2.0)
    return status == 200


# ---------------------------------------------------------------------------- #
# Result reporting
# ---------------------------------------------------------------------------- #
class CheckReporter:
    """Aggregates pass/fail counts and prints a final summary.

    Usage:
        r = CheckReporter("test_chaos")
        r.check("tts forced 500 → chat still 200", status == 200)
        r.check("nmt slow → chat absorbs delay", elapsed < 3.0)
        r.summary_or_exit()
    """
    def __init__(self, suite_name: str):
        self.suite = suite_name
        self.passes = 0
        self.fails = 0
        self.failed_names: list[str] = []
        self._stdout_utf8()

    def _stdout_utf8(self) -> None:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore
        except Exception:
            pass

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        marker = "PASS" if ok else "FAIL"
        line = f"  [{marker}] {name}"
        if detail:
            line += f"  ({detail})"
        print(line, flush=True)
        if ok:
            self.passes += 1
        else:
            self.fails += 1
            self.failed_names.append(name)
        return ok

    def info(self, msg: str) -> None:
        print(f"  [info] {msg}", flush=True)

    def summary_or_exit(self) -> None:
        total = self.passes + self.fails
        print()
        print(f"  --- {self.suite}: {self.passes}/{total} green ---")
        if self.fails:
            print(f"  failed: {self.failed_names}")
            raise SystemExit(1)
        print(f"  PASS — {self.suite}")
        raise SystemExit(0)


# ---------------------------------------------------------------------------- #
# Misc
# ---------------------------------------------------------------------------- #
def python_exe() -> str:
    """Return the Python interpreter to use for spawning sidecars. Mirrors the
    auto-resolve order in companion-avatar/src/tts_server.rs:
      1. $COMPANION_TTS_PYTHON
      2. E:/miniconda/envs/tts/python.exe (canonical Windows dev path)
      3. sys.executable (whatever's running this rig)
    """
    if (p := os.environ.get("COMPANION_TTS_PYTHON")):
        return p
    canonical = "E:/miniconda/envs/tts/python.exe"
    if Path(canonical).exists():
        return canonical
    return sys.executable
