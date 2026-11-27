# -*- coding: utf-8 -*-
"""Самопроверка сборки без окна: own-tube.exe --selftest report.json"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import __version__, player, search, sponsorblock, validate
from .db import Store
from .network import Route
from .tools import find_tool


def _version(exe: str | None, arg: str) -> str:
    if not exe:
        raise FileNotFoundError("не найден ни в tools\\, ни в PATH")
    flags = 0x08000000 if sys.platform.startswith("win") else 0
    out = subprocess.run([exe, arg], capture_output=True, timeout=60, shell=False, creationflags=flags)
    text = (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()
    return text[0][:120] if text else f"код {out.returncode}"


def run(path: str) -> int:
    report: dict = {"version": __version__, "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False))}
    ok = True

    def step(name, fn):
        nonlocal ok
        t = time.time()
        try:
            report[name] = {"ok": True, "result": fn(), "sec": round(time.time() - t, 1)}
        except Exception as exc:
            ok = False
            report[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400], "trace": traceback.format_exc()[-800:]}

    for tool, arg in (("mpv", "--version"), ("ffmpeg", "-version"), ("deno", "--version")):
        step(tool, lambda t=tool, a=arg: {"path": find_tool(t), "version": _version(find_tool(t), a)})
    route = Route()
    step("search", lambda: [i["title"][:40] for i in search.search("lofi hip hop", route, 3)])
    step("stream_720_h264", lambda: validate.is_stream_url(player.resolve_streams("yt", "dQw4w9WgXcQ", "720", route, "h264")["video"]))
    step("sponsorblock", lambda: len(sponsorblock.segments("dQw4w9WgXcQ", ["sponsor"], route)) >= 0)

    def embedded():
        """Встроенный плеер: yt-dlp → ffmpeg (-c copy) → локальный медиасервер; читаем первые 256 КБ."""
        import urllib.request
        from .media import MediaServer
        st = player.resolve_streams("yt", "dQw4w9WgXcQ", "360", route, "auto", embedded=True)
        srv = MediaServer(lambda: route, lambda: find_tool("ffmpeg"))
        try:
            sid = srv.add_session(st)
            try:
                with urllib.request.urlopen(srv.video_url(sid, 5), timeout=60) as r:
                    data = r.read(256 * 1024)
                    ctype = r.headers.get("Content-Type")
            except Exception as exc:
                raise RuntimeError(f"{exc}; ffmpeg: {srv.last_error}") from exc
            thumb = srv.base + "/img?u=" + urllib.request.quote(validate.thumb_url("dQw4w9WgXcQ"), safe="")
            with urllib.request.urlopen(thumb, timeout=30) as r:
                img = len(r.read())
        finally:
            srv.close()
        magic = "webm" if data[:4] == b"\x1a\x45\xdf\xa3" else ("mp4" if b"ftyp" in data[:16] else "?")
        if magic == "?" or len(data) < 64 * 1024:
            raise RuntimeError(f"поток не похож на видео: {magic}, {len(data)} байт")
        return {"container": st["container"], "magic": magic, "bytes": len(data), "type": ctype, "thumb_bytes": img}

    step("embedded_remux", embedded)

    def vk_search():
        from . import vk
        return len(vk.search("музыка", route)["items"])

    step("vk_search", vk_search)

    def ai_local():
        """Чат с подставной локальной моделью (127.0.0.1, SSE) — проверка разбора потока."""
        import http.server
        import threading
        from .ai.providers import chat_stream

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for t in ("При", "вет"):
                    self.wfile.write(('data: {"choices":[{"delta":{"content":"%s"}}]}\n\n' % t).encode())
                self.wfile.write(b"data: [DONE]\n\n")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            text = "".join(chat_stream({"base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1", "model": "m"}, "",
                                       "sys", [{"role": "user", "content": "hi"}], route))
        finally:
            srv.shutdown()
        if text != "Привет":
            raise RuntimeError("неожиданный ответ: " + text)
        return text

    step("ai_chat_local", ai_local)

    def secrets_roundtrip():
        from .secretstore import SecretStore
        with tempfile.TemporaryDirectory() as d:
            st = SecretStore(Path(d))
            persisted = st.set("selftest-value", name="selftest")
            raw = (Path(d) / "secrets.dpapi").read_bytes() if (Path(d) / "secrets.dpapi").exists() else b""
            again = SecretStore(Path(d)).get("selftest")
            return {"backend": type(st._backend).__name__, "persisted": persisted,
                    "plaintext_on_disk": b"selftest-value" in raw, "read_back": again == "selftest-value" or not persisted}

    step("secrets", secrets_roundtrip)

    def cookies_fmt():
        from . import accounts
        txt = ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tx\n.evil.example\tTRUE\t/\tTRUE\t0\tA\tb\n"
        c = accounts.parse_netscape(txt, "google")
        return len(c) == 1 and accounts.signed_in(c, "google")

    step("cookies_format", cookies_fmt)

    def webview_mod():
        import webview
        from . import login_window  # noqa: F401 — окно входа собрано в программу
        return getattr(webview, "__version__", "?")

    step("webview", webview_mod)
    step("ui_files", lambda: all((player.resource_dir() / "owntube" / "ui" / n).is_file() for n in ("index.html", "style.css", "app.js")))
    step("skip_script", lambda: player.SKIP_SCRIPT.is_file())

    def dbtest():
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            s.add_history({"source": "yt", "id": "jNQXAC9IVRw", "title": "t"})
            n = len(s.history())
            s.close()
            return n

    step("sqlite", dbtest)
    report["ok"] = ok
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1
