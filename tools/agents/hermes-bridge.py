#!/usr/bin/env python3
"""Tiny HTTP -> hermes -z bridge.
POST /webhook  {"message": "..."}  ->  {"model":"hermes","response":"..."}
GET  /health                       ->  {"status":"ok","agent":"hermes"}
Gives hermes-agent the same /webhook shape zeroclaw uses, so a single
client can drive both. Bind 0.0.0.0:18791 by default.
"""
import json, os, subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("HERMES_BRIDGE_PORT", "18791"))
HOST = os.environ.get("HERMES_BRIDGE_HOST", "0.0.0.0")
HERMES_BIN = os.environ.get("HERMES_BIN", os.path.expanduser("~/.local/bin/hermes"))
TIMEOUT = int(os.environ.get("HERMES_TIMEOUT", "180"))

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "agent": "hermes"})
        else:
            self._send(404, {"error": "not_found"})
    def do_POST(self):
        if self.path != "/webhook":
            return self._send(404, {"error": "not_found"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            msg = json.loads(raw).get("message", "")
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})
        if not msg:
            return self._send(400, {"error": "missing message"})
        try:
            p = subprocess.run([HERMES_BIN, "-z", msg], capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return self._send(504, {"error": "hermes timed out"})
        if p.returncode != 0:
            return self._send(500, {"error": "hermes failed", "stderr": p.stderr[-2000:]})
        return self._send(200, {"model": "hermes", "response": p.stdout.strip()})
    def log_message(self, fmt, *args):
        sys.stderr.write("[hermes-bridge] " + (fmt % args) + "\n")

if __name__ == "__main__":
    print(f"[hermes-bridge] listening http://{HOST}:{PORT} -> {HERMES_BIN} -z", file=sys.stderr)
    HTTPServer((HOST, PORT), H).serve_forever()
