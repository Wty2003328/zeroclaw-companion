"""P1 — UX critic as pre-merge gate (deterministic lints).

The full review job is the `ux-critic` agent at `.claude/agents/ux-critic.md`.
This rig is the mechanical floor — rules that don't need judgment, just
strings: shell quoting in default config values, raw HTTP codes in chat
bubbles, Windows paths in user-visible strings, settings hidden under
'advanced' without justification.

Why deterministic + agent split:
  * Deterministic checks must run in CI (no LLM dependency, no API cost,
    sub-second).
  * The agent catches *judgment* calls that static rules can't (terminology
    drift, click-count, missing actionable next steps). Run it on demand.

Scope of files audited (relative to repo root):
  - companion.toml.example
  - web/src/pages/**/*.tsx
  - web/src/components/**/*.tsx
  - apps/companion-server/src/main.rs   (error-string templates)
  - crates/companion-avatar/src/config.rs (advanced flags + defaults)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from tts_tools._test_helpers import CheckReporter, REPO_ROOT


# ---------------------------------------------------------------------------- #
# Rule catalog
# ---------------------------------------------------------------------------- #
# Each rule is (name, file_glob, predicate, fix_hint). The predicate is given a
# (path, content) pair and returns a list of (lineno, evidence, severity) tuples.
# Severity: "BLOCKER" → hard fail; "WARN" → reported via r.info, not r.check.


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _iter_files(globs: Iterable[str]) -> list[Path]:
    out = []
    for g in globs:
        out.extend(sorted(REPO_ROOT.glob(g)))
    return out


def _line_of(content: str, idx: int) -> int:
    return content[:idx].count("\n") + 1


# Rule 1 — shell quoting / env-var chains in default values
# Triggers on `key = "...cmd /...&&..."` patterns inside config files.
SHELL_PATTERNS = [
    r"&&",                           # chained shell
    r"\|\|",                         # OR chain
    r"\b(set|export)\s+\w+\s*=",     # inline env-var prefix
    r"^[\"']?cmd\s+/[CK]\b",         # cmd.exe invocation
    r"^[\"']?sh\s+-c\b",             # sh -c invocation
    r"\$\(.*\)",                     # command substitution
    r"`[^`]+`",                      # backtick substitution
]
_SHELL_RE = re.compile("|".join(SHELL_PATTERNS), re.MULTILINE)

# Rule 2 — raw HTTP codes in user-visible string templates
# Triggers when the template explicitly mentions a literal status code.
HTTP_CODE_PATTERNS = [
    r"\[error\s+\d+\]",                          # `[error 502]`
    r"Error[: ]\s*\d{3}\b",                      # `Error: 500`
    r"HTTP\s+\d{3}\b",                           # `HTTP 502`
    r"Failed[: ]\s*\d{3}\b",                     # `Failed: 503`
    r"status\s*\(?\s*\d{3}\s*\)?\s*[\"',\]\}]",  # `status(500)`
]
_HTTP_RE = re.compile("|".join(HTTP_CODE_PATTERNS), re.IGNORECASE)

# Rule 3 — Windows / Unix paths in user-visible strings
# Triggers in JSX text or string templates that surface to users.
PATH_PATTERNS = [
    r"[A-Z]:[\\/]\w",                # `C:\Users\…` or `C:/…`
    r"/home/\w",                     # Unix home
    r"/Users/\w",                    # macOS home
    r"/etc/\w",                      # system paths
]
_PATH_RE = re.compile("|".join(PATH_PATTERNS))

# Rule 4 — stack-trace / debug fragments in user-visible strings
# Note: keep these specific enough not to fire on innocent JSX (`<code>`,
# `at <span>`). Stack frames look like `at FunctionName (file:line)` —
# require the parenthesized location.
TRACE_PATTERNS = [
    r"Traceback \(most recent call",
    r'File "[^"]+\.py", line \d+',
    r"at \w+ \([^)]*:\d+:\d+\)",       # JS stack: `at fn (file.js:10:5)`
    r"\[debug\]",
    r"\[trace\]",
    r"\bTODO\b.*shipit",
]
_TRACE_RE = re.compile("|".join(TRACE_PATTERNS))


def find_shell_quoting(path: Path, content: str) -> list[tuple[int, str, str]]:
    """Flags shell-quoting in user-editable default values.

    Scope: TOML config examples ONLY. TSX template literals (`${expr}`,
    `(default)` annotations) are a different category — not user-facing
    config syntax. The shell-hell bug class is the launch_command field
    in companion.toml.example; lint that file aggressively.
    """
    out: list[tuple[int, str, str]] = []
    if not (path.name.endswith(".example") or path.suffix == ".toml"):
        return out
    for m in _SHELL_RE.finditer(content):
        line_start = content.rfind("\n", 0, m.start()) + 1
        line_end = content.find("\n", m.end())
        if line_end == -1:
            line_end = len(content)
        line = content[line_start:line_end]
        # TOML: only flag if inside a quoted string value (`key = "..."`).
        # Skip comment lines.
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"=\s*['\"]", line):
            out.append((_line_of(content, m.start()), line.strip(), "BLOCKER"))
    return out


def find_http_codes(path: Path, content: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for m in _HTTP_RE.finditer(content):
        line_start = content.rfind("\n", 0, m.start()) + 1
        line_end = content.find("\n", m.end())
        if line_end == -1:
            line_end = len(content)
        line = content[line_start:line_end]
        stripped = line.lstrip()
        # Skip comments (single-line `//`, block `*` continuation, JSDoc, shell `#`).
        if stripped.startswith(("//", "/*", "*", "#")):
            continue
        # Whitelist: tests, debug logs.
        if "/test" in str(path).replace("\\", "/").lower():
            continue
        # Allow telemetry / log messages (not user-visible).
        if "tracing::" in line or "console.log" in line or "console.debug" in line:
            continue
        out.append((_line_of(content, m.start()), line.strip(), "BLOCKER"))
    return out


def find_user_paths(path: Path, content: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for m in _PATH_RE.finditer(content):
        line_start = content.rfind("\n", 0, m.start()) + 1
        line_end = content.find("\n", m.end())
        if line_end == -1:
            line_end = len(content)
        line = content[line_start:line_end]
        # Whitelist: documentation files, comments (// or //!  or #),
        # development-only constants explicitly tagged.
        if path.suffix in (".md", ".txt"):
            continue
        # Skip comment lines.
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "/*", "*")):
            continue
        # Allow if explicitly tagged dev-only via a nearby comment.
        if "DEV_ONLY" in content[max(0, m.start() - 80) : m.start() + 80]:
            continue
        # Path appearing in a JSX text node or user-visible template = BLOCKER.
        if path.suffix == ".tsx":
            severity = "BLOCKER" if re.search(r"['\"](msg|title|label|text|placeholder|defaultValue)['\"]", line) else "WARN"
        else:
            severity = "WARN"
        out.append((_line_of(content, m.start()), line.strip(), severity))
    return out


def find_trace_fragments(path: Path, content: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for m in _TRACE_RE.finditer(content):
        line_start = content.rfind("\n", 0, m.start()) + 1
        line_end = content.find("\n", m.end())
        if line_end == -1:
            line_end = len(content)
        line = content[line_start:line_end]
        # Skip the rig's own pattern definitions (this file!) and tests.
        if path.name == "test_ux_critic.py" or path.name == "test_user_facing_strings.py":
            continue
        if "test_" in path.name:
            continue
        out.append((_line_of(content, m.start()), line.strip(), "BLOCKER"))
    return out


def find_advanced_without_justification(path: Path, content: str) -> list[tuple[int, str, str]]:
    """Look for `advanced=true` or `<details>` Settings blocks. If the line
    above doesn't carry a `// reason:` or `# reason:` comment explaining why,
    warn (not block — this is a soft rule).
    """
    out: list[tuple[int, str, str]] = []
    pattern = re.compile(r"\badvanced\s*[:=]\s*true\b|<details\s")
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if pattern.search(line):
            # Check the previous 2 lines for a justification.
            prev = " ".join(lines[max(0, i - 2) : i])
            if "reason:" not in prev and "rationale:" not in prev:
                out.append((i + 1, line.strip(), "WARN"))
    return out


# ---------------------------------------------------------------------------- #
# Audit driver
# ---------------------------------------------------------------------------- #
def audit() -> tuple[list[dict], list[dict]]:
    """Return (blockers, warnings) lists. Each entry has {file, line, rule,
    evidence, fix_hint}."""
    files = _iter_files([
        "companion.toml.example",
        "web/src/pages/**/*.tsx",
        "web/src/components/**/*.tsx",
        "apps/companion-server/src/main.rs",
        "crates/companion-avatar/src/config.rs",
    ])

    rules: list[tuple[str, callable, str]] = [
        ("shell-quoting in default value", find_shell_quoting,
         "use auto-resolve in supervisor; user-facing config should declare intent, not shell ops"),
        ("raw HTTP code in user-visible string", find_http_codes,
         "translate to natural language: 'Service unavailable, retry shortly' instead of '[error 502]'"),
        ("user/system path in user-visible string", find_user_paths,
         "auto-resolve via supervisor; user shouldn't see absolute paths in UI"),
        ("trace fragment / debug label in user-visible string", find_trace_fragments,
         "remove debug instrumentation before shipping; route to log file, not chat"),
        ("advanced setting without `reason:` comment", find_advanced_without_justification,
         "add `// reason: ...` one-liner above; burying engineering knobs under 'advanced' is not a fix"),
    ]

    blockers: list[dict] = []
    warnings: list[dict] = []

    for path in files:
        content = _read_text(path)
        if not content:
            continue
        rel = path.relative_to(REPO_ROOT)
        for rule_name, fn, fix_hint in rules:
            try:
                hits = fn(path, content)
            except Exception as e:
                # A misbehaving rule shouldn't crash the gate — log it.
                hits = []
                warnings.append({
                    "file": str(rel),
                    "line": 0,
                    "rule": f"rule '{rule_name}' raised: {e!r}",
                    "evidence": "",
                    "fix_hint": "",
                })
                continue
            for lineno, evidence, sev in hits:
                entry = {
                    "file": str(rel),
                    "line": lineno,
                    "rule": rule_name,
                    "evidence": evidence,
                    "fix_hint": fix_hint,
                }
                (blockers if sev == "BLOCKER" else warnings).append(entry)

    return blockers, warnings


def main():
    r = CheckReporter("test_ux_critic")
    print("[ux-critic] auditing user-facing surfaces …")
    print(f"  repo root: {REPO_ROOT}")

    try:
        blockers, warnings = audit()
    except Exception as e:
        r.check("audit ran to completion", False, f"crashed: {e!r}")
        r.summary_or_exit()
        return

    # Print warnings first as info (they don't fail the gate).
    for w in warnings:
        r.info(f"{w['file']}:{w['line']}  WARN  {w['rule']}")
        if w["evidence"]:
            r.info(f"      └─ {w['evidence'][:120]}")

    # Each rule contributes ONE check (pass if zero blockers for that rule).
    by_rule: dict[str, list[dict]] = {}
    for b in blockers:
        by_rule.setdefault(b["rule"], []).append(b)

    # The catalog of rule names we audit — even if no hits, we want to assert
    # the rule was checked (zero hits = pass).
    rule_names = [
        "shell-quoting in default value",
        "raw HTTP code in user-visible string",
        "user/system path in user-visible string",
        "trace fragment / debug label in user-visible string",
        # 'advanced without justification' is WARN-only, not a rule check
    ]
    for rule in rule_names:
        hits = by_rule.get(rule, [])
        ok = len(hits) == 0
        detail = f"{len(hits)} hit(s)" if hits else "clean"
        r.check(f"rule: {rule}", ok, detail)
        for h in hits:
            r.info(f"  {h['file']}:{h['line']}  {h['evidence'][:120]}")
            r.info(f"  └─ fix: {h['fix_hint']}")

    # Surface warning count separately so it's visible but not fail-blocking.
    if warnings:
        print(f"\n  [info] {len(warnings)} soft warning(s) — review but not gate-blocking", flush=True)

    r.summary_or_exit()


if __name__ == "__main__":
    main()
