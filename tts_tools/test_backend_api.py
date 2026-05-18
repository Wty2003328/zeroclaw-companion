"""L6a — Backend HTTP coverage.

Boots the mock stack + companion-server with avatar AND pulse enabled
(against a temp scratch dir), then exercises every documented HTTP
endpoint with happy + edge cases.

Specifically guards against the iter-2 / iter-10 schema-drift bug class
where the /api/config payload silently dropped the new TTS override
keys (tts_streaming, tts_streaming_target_chars, tts_quality) — those
land in the Settings UI as "<empty>" instead of the configured value.

Targets: 18-22 green.
"""
from __future__ import annotations

import json
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from tts_tools._rig_shim import (
    PORT_ZC_SHIM,
    require_ports_free_with_wait,
    robust_http_get as http_get,
    robust_http_json as http_json,
    robust_http_post_json as http_post_json,
    start_shims,
    wait_for_shims,
)
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
    spawn,
    spawn_companion_server,
    wait_for_port,
    wait_for_url,
)


def _build_config() -> dict:
    """Companion config wired against the mock stack + rig shim. Avatar
    + pulse on; speech off (test_backend_api_extended exercises speech)."""
    return {
        "zeroclaw": {
            "kind": "zeroclaw",
            # Rig shim adapts /webhook + /health → foundation mock's
            # /api/chat + /api/healthz.
            "url": f"http://127.0.0.1:{PORT_ZC_SHIM}",
            "timeout_secs": 10,
        },
        "server": {
            "host": "127.0.0.1",
            "port": PORT_COMPANION,
        },
        "avatar": {
            "enabled": True,
            "chat_language": "en",
            "tts": {
                "engine": "mock",
                "api_url": f"http://127.0.0.1:{PORT_TTS}",
                "port": PORT_TTS,
                "language": "en",
                "voice": "asuna",
                "speed": 1.0,
                "quality": "balanced",
                "auto_start": False,
                "close_with_companion": False,
                "streaming": True,
                "streaming_target_chars": 80,
            },
            "subagent": {
                "enabled": False,
                "only_when_translating": True,
            },
            "speech": {
                "enabled": False,
            },
        },
        "pulse": {
            "enabled": True,
        },
        "pulse.database": {
            "path": "./pulse_test.db",
            "retention_days": 30,
        },
        "pulse.collectors.rss": {
            "enabled": False,
            "interval": "30m",
        },
        "pulse.collectors.hackernews": {
            "enabled": False,
            "interval": "15m",
        },
    }


def _http_method(url: str, method: str, body: object | None = None,
                 timeout: float = 5.0, headers: dict | None = None) -> int:
    """Wrapper that returns the status code for arbitrary HTTP methods.
    Convenient for PUT / DELETE which urllib doesn't have direct
    convenience methods for."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> None:
    r = CheckReporter("test_backend_api")
    require_ports_free_with_wait(
        PORT_TTS, PORT_NMT, PORT_CONTROL, PORT_MOCK_ZEROCLAW, PORT_COMPANION,
        wait_s=12.0,
    )

    scratch = Path(tempfile.mkdtemp(prefix="companion-l6a-"))
    log_dir = scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    zc_shim, speech_shim = start_shims()
    try:
        with managed_procs() as procs:
            # 1. Mock stack.
            mock = spawn(
                "mock-stack",
                [python_exe(), "-m", "tts_tools._mock_stack"],
                port=PORT_CONTROL,
                cwd=REPO_ROOT,
                log_path=log_dir / "mock-stack.log",
            )
            procs.append(mock)
            if not wait_for_port(PORT_CONTROL, timeout_s=15.0):
                r.check("mock stack control plane bound", False, "timeout")
                r.summary_or_exit()
            r.check("mock stack control plane bound", True)
            for p in (PORT_TTS, PORT_NMT, PORT_MOCK_ZEROCLAW):
                wait_for_port(p, timeout_s=10.0)
            wait_for_shims(timeout_s=5.0)

            # 2. Companion server.
            comp, _cfg_path = spawn_companion_server(
                _build_config(), port=PORT_COMPANION, log_dir=log_dir,
                scratch_dir=scratch,
            )
            procs.append(comp)
            if not wait_for_url(f"http://127.0.0.1:{PORT_COMPANION}/health", timeout_s=30.0):
                r.check("companion /health came up", False, "30s timeout")
                r.summary_or_exit()
            r.check("companion /health came up", True)

            base = f"http://127.0.0.1:{PORT_COMPANION}"

            # ── /health ─────────────────────────────────────────────
            status, body, _ = http_get(f"{base}/health")
            r.check("GET /health 200", status == 200, f"status={status}")
            body_str = body.decode("utf-8", errors="replace").strip()
            ok_shape = body_str == "ok" or '"ok"' in body_str.lower()
            r.check("GET /health body is 'ok'", ok_shape, f"body={body_str!r}")

            # ── /api/status ────────────────────────────────────────
            st = http_json(f"{base}/api/status")
            r.check("GET /api/status returns JSON", st is not None)
            if st:
                r.check("/api/status has avatar_enabled key", "avatar_enabled" in st,
                        f"keys={list(st.keys())}")
                r.check("/api/status .ok=true", st.get("ok") is True, f"ok={st.get('ok')}")

            # ── /api/config — schema-drift guard (iter-2/10 fix) ────
            cfg = http_json(f"{base}/api/config")
            r.check("GET /api/config returns JSON", cfg is not None)
            if cfg:
                avatar = cfg.get("avatar") or {}
                tts = avatar.get("tts") or {}
                required_tts_keys = [
                    "engine", "voice", "speed", "quality",
                    "streaming", "streaming_target_chars",
                ]
                missing = [k for k in required_tts_keys if k not in tts]
                r.check(
                    "GET /api/config has all avatar.tts override keys (iter-2/10 schema fix)",
                    not missing,
                    f"missing={missing}; got keys={sorted(tts.keys())}",
                )
                r.check("config.avatar.tts.engine present", "engine" in tts)

            # ── /api/config/avatar — clamps and round-trips ────────
            status, _, _ = http_post_json(f"{base}/api/config/avatar", {"tts_speed": 1.5})
            r.check("POST /api/config/avatar speed=1.5 → 200", status == 200, f"status={status}")
            speed = (
                ((http_json(f"{base}/api/config") or {}).get("avatar") or {})
                .get("tts", {}).get("speed")
            )
            r.check("GET /api/config sees speed=1.5",
                    abs((speed or 0) - 1.5) < 1e-6, f"speed={speed}")

            http_post_json(f"{base}/api/config/avatar", {"tts_speed": 5.0})
            speed = (
                ((http_json(f"{base}/api/config") or {}).get("avatar") or {})
                .get("tts", {}).get("speed")
            )
            r.check("avatar speed=5.0 clamps to 3.0 (max)", speed == 3.0, f"speed={speed}")

            http_post_json(f"{base}/api/config/avatar", {"tts_speed": 0.1})
            speed = (
                ((http_json(f"{base}/api/config") or {}).get("avatar") or {})
                .get("tts", {}).get("speed")
            )
            r.check("avatar speed=0.1 clamps to 0.25 (min)", speed == 0.25, f"speed={speed}")

            # ── /api/config/subagent round-trip ────────────────────
            status, _, _ = http_post_json(
                f"{base}/api/config/subagent",
                {"translator_nmt_tgt_lang": "ja"},
            )
            r.check("POST /api/config/subagent → 200", status == 200, f"status={status}")
            tgt = (
                ((http_json(f"{base}/api/config") or {}).get("avatar") or {})
                .get("subagent", {}).get("translator", {}).get("nmt_tgt_lang")
            )
            r.check("GET /api/config sees subagent nmt_tgt_lang=ja",
                    tgt == "ja", f"nmt_tgt_lang={tgt!r}")

            # ── /api/config/zeroclaw round-trip ────────────────────
            new_url = f"http://127.0.0.1:{PORT_MOCK_ZEROCLAW}"
            status, _, _ = http_post_json(f"{base}/api/config/zeroclaw", {"url": new_url})
            r.check("POST /api/config/zeroclaw url → 200", status == 200, f"status={status}")
            zurl = ((http_json(f"{base}/api/config") or {}).get("zeroclaw") or {}).get("url")
            r.check("GET /api/config sees zeroclaw url change",
                    zurl == new_url, f"url={zurl!r}")

            # Restore url to the shim so subsequent chats route through.
            # The hot-swap is synchronous on the Rust side (ArcSwap)
            # so a small settle isn't strictly required, but give it a
            # beat to avoid races with concurrent watchdog probes.
            http_post_json(f"{base}/api/config/zeroclaw",
                           {"url": f"http://127.0.0.1:{PORT_ZC_SHIM}"})
            time.sleep(0.3)

            # ── /api/chat happy path ───────────────────────────────
            status, body, _ = http_post_json(f"{base}/api/chat", {"message": "hi"}, timeout=15.0)
            r.check("POST /api/chat returns 200", status == 200, f"status={status}")
            try:
                reply = json.loads(body.decode("utf-8")).get("reply", "")
            except Exception:
                reply = ""
            r.check("POST /api/chat reply contains 'mock-reply'",
                    "mock-reply" in reply, f"reply={reply!r}")

            # ── /api/characters CRUD ────────────────────────────────
            chars = http_json(f"{base}/api/characters")
            r.check("GET /api/characters returns object with .characters list",
                    chars is not None and isinstance(chars.get("characters"), list),
                    f"got={chars!r}")

            char_id = "rig-test-char"
            new_char = {
                "id": char_id, "name": "Rig Test", "model_id": "",
                "system_prompt": "be brief", "notes": "",
            }
            status, _, _ = http_post_json(f"{base}/api/characters", new_char)
            r.check("POST /api/characters create → 200", status == 200, f"status={status}")
            chars2 = http_json(f"{base}/api/characters") or {}
            present = any(c.get("id") == char_id for c in chars2.get("characters", []))
            r.check("GET /api/characters lists the new character", present)

            # Activate via PUT /api/characters/{id}/active per spec.
            # main.rs registers POST /api/characters/active with id in
            # body — accept either by trying both.
            act_status = _http_method(
                f"{base}/api/characters/{char_id}/active", "PUT", body={},
            )
            if act_status != 200:
                act_status, _, _ = http_post_json(
                    f"{base}/api/characters/active", {"id": char_id})
            r.check("activate character (PUT or POST) → 200",
                    act_status == 200, f"status={act_status}")

            # Empty id → 400.
            status, _, _ = http_post_json(
                f"{base}/api/characters",
                {"id": "", "name": "", "model_id": "", "system_prompt": "", "notes": ""},
            )
            r.check("POST /api/characters with empty id → 400",
                    status == 400, f"status={status}")

            # Nonexistent character DELETE → 404.
            del_404 = _http_method(f"{base}/api/characters/nonexistent", "DELETE")
            r.check("DELETE /api/characters/nonexistent → 404",
                    del_404 == 404, f"status={del_404}")

            # ── Attachments PUT / GET / LIST / DELETE ───────────────
            att_url = f"{base}/api/characters/{char_id}/attachments/note.md"
            put_status = _http_method(att_url, "PUT", body={"body": "# lore\nhello"})
            r.check("PUT .../attachments/note.md → 200",
                    put_status == 200, f"status={put_status}")

            atts = http_json(f"{base}/api/characters/{char_id}/attachments") or {}
            names = [a.get("name") for a in atts.get("attachments", [])]
            r.check("GET .../attachments lists note.md",
                    "note.md" in names, f"got={names}")

            got = http_json(att_url) or {}
            r.check("GET .../attachments/note.md returns body",
                    got.get("body", "").startswith("# lore"), f"got={got!r}")

            del_att = _http_method(att_url, "DELETE")
            r.check("DELETE .../attachments/note.md → 200",
                    del_att == 200, f"status={del_att}")

            del_c = _http_method(f"{base}/api/characters/{char_id}", "DELETE")
            r.check("DELETE /api/characters/{id} → 200",
                    del_c == 200, f"status={del_c}")

            # ── Pulse ────────────────────────────────────────────────
            pst = http_json(f"{base}/api/pulse/status")
            r.check("GET /api/pulse/status returns object with .collectors",
                    pst is not None and "collectors" in pst, f"got={pst!r}")

            unread = http_json(f"{base}/api/pulse/unread_count")
            r.check("GET /api/pulse/unread_count has int .unread",
                    unread is not None and isinstance(unread.get("unread"), int),
                    f"got={unread!r}")

            # ── SPA fallthrough for unknown /api path ───────────────
            status, body, headers = http_get(f"{base}/api/badpath")
            ct = headers.get("content-type", "").lower()
            # /api/badpath should NOT return JSON-shaped content (the
            # SPA fallback serves HTML, or it's a clean 404). Failing
            # this would mean a route accidentally matched & gave junk
            # JSON to the UI.
            looks_like_html = b"<" in body[:50] or "html" in ct
            looks_like_json_object = (
                status == 200
                and body[:1] in (b"{", b"[")
            )
            r.check("GET /api/badpath returns SPA HTML (not bogus JSON)",
                    looks_like_html and not looks_like_json_object,
                    f"status={status} ct={ct!r} body[:60]={body[:60]!r}")

        r.summary_or_exit()
    finally:
        try:
            zc_shim.shutdown()
        except Exception:
            pass
        try:
            speech_shim.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
