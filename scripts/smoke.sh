#!/usr/bin/env bash
# Quick smoke check for a running companion stack.
# Doesn't start anything — just probes the public surfaces and reports
# which layer is live and which isn't.

set -u

ZEROCLAW_URL="${ZEROCLAW_URL:-http://127.0.0.1:8080}"
COMPANION_URL="${COMPANION_URL:-http://127.0.0.1:9181}"
TTS_URL="${TTS_URL:-http://127.0.0.1:9880}"

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
heading(){ printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }

check() {
  local label="$1" url="$2" expect="$3"
  local body
  if body=$(curl -s --max-time 5 "$url" 2>/dev/null); then
    if echo "$body" | grep -qF "$expect"; then
      green "  ✓ $label  ($url)"
    else
      yellow "  ! $label reachable but body unexpected: $(echo "$body" | head -c 80)…"
    fi
  else
    red "  ✗ $label unreachable  ($url)"
  fi
}

heading "zeroclaw upstream"
check "/health" "$ZEROCLAW_URL/health" "ok"

heading "companion-server"
check "/health"      "$COMPANION_URL/health"      "ok"
check "/api/status"  "$COMPANION_URL/api/status"  "\"ok\":true"

heading "TTS port"
if body=$(curl -s --max-time 5 "$TTS_URL/health" 2>/dev/null); then
  green "  ✓ /health reachable"
  echo "$body" | head -c 200; echo
else
  red "  ✗ /health unreachable  ($TTS_URL)"
fi

heading "synthesis round trip (TTS only)"
TMP=$(mktemp -t companion-smoke-XXXXX).wav
if curl -s --max-time 30 \
    -X POST "$TTS_URL/tts" \
    -H "Content-Type: application/json" \
    -d '{"text":"smoke test","language":"en"}' \
    --output "$TMP" 2>/dev/null \
  && [ -s "$TMP" ]; then
  size=$(wc -c < "$TMP")
  green "  ✓ /tts produced $size bytes  ($TMP)"
else
  red "  ✗ /tts did not produce audio (file empty or request failed)"
fi
rm -f "$TMP"

heading "Pulse"
if body=$(curl -s --max-time 5 "$COMPANION_URL/api/pulse/status" 2>/dev/null); then
  if echo "$body" | grep -q '"collectors"'; then
    green "  ✓ /api/pulse/status reachable (Pulse enabled)"
  else
    yellow "  ! Pulse appears disabled or returned an unexpected body"
  fi
else
  yellow "  - Pulse not enabled in companion.toml (or unreachable)"
fi

echo
green "smoke check complete"
