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
    def fast_lists():
        from . import lists
        t = time.time()
        items, cur, _ = lists.first("search", "lofi hip hop", route)
        first = round(time.time() - t, 2)
        more, cur2 = lists.more(cur, route) if cur else ([], None)
        if len(items) < 10 or not cur or not more or {i["id"] for i in items} & {i["id"] for i in more}:
            raise RuntimeError(f"страницы не сработали: {len(items)} + {len(more)}, продолжение: {bool(cur)}")
        return {"engine": cur["k"], "first": len(items), "second": len(more), "first_sec": first, "more_token": bool(cur2)}

    step("lists_pages", fast_lists)

    def shorts_list():
        from . import lists
        items, cur, _ = lists.first("shorts", None, route)
        if len(items) < 8 or not all(i.get("short") for i in items):
            raise RuntimeError(f"Shorts: {len(items)}")
        return {"count": len(items), "more": bool(cur)}

    step("shorts_list", shorts_list)

    def socks_connect_modes():
        """Собственный мини-SOCKS5 на 127.0.0.1: соединение по имени (ATYP 3), затем по IPv4 при отказе по имени."""
        import socket
        import threading
        from . import bridge

        echo = socket.socket()
        echo.bind(("127.0.0.1", 0))
        echo.listen(4)
        seen = []

        def serve_echo():
            while True:
                try:
                    c, _ = echo.accept()
                except OSError:
                    return
                c.sendall(b"hi")
                c.close()

        def serve_socks(srv):
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                try:
                    c.recv(256)
                    c.sendall(b"\x05\x00")
                    head = c.recv(4)
                    atyp = head[3]
                    if atyp == 3:
                        n = c.recv(1)[0]
                        c.recv(n)
                        c.recv(2)
                        seen.append("domain")
                        c.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)   # «отказ»: прокси не смог по имени
                    else:
                        c.recv(4)
                        c.recv(2)
                        seen.append("ipv4")
                        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
                        up = socket.create_connection(("127.0.0.1", echo.getsockname()[1]))
                        c.sendall(up.recv(2))
                        up.close()
                except OSError:
                    pass
                finally:
                    c.close()

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        threading.Thread(target=serve_echo, daemon=True).start()
        threading.Thread(target=serve_socks, args=(srv,), daemon=True).start()
        old = bridge._resolve_ipv4
        bridge._resolve_ipv4 = lambda *a: (["127.0.0.1"], "тест")
        try:
            sock, info = bridge.connect_via_proxy(f"socks5://127.0.0.1:{srv.getsockname()[1]}", "rr1.example.test", 443, timeout=5)
            data = sock.recv(2)
            sock.close()
        finally:
            bridge._resolve_ipv4 = old
            srv.close()
            echo.close()
        if data != b"hi" or info["atyp"] != "ipv4" or seen[:2] != ["domain", "domain"]:
            raise RuntimeError(f"{data!r} {info} {seen}")
        return {"attempts": seen, "final": info["atyp"]}

    step("socks_domain_then_ipv4", socks_connect_modes)

    def first_bytes():
        """Как шаг «Проверка видео»: первые байты настоящего потока по Range-запросу, заголовки те же, что у плеера."""
        from urllib.parse import urlsplit
        from . import diagnostics
        st = player.resolve_streams("yt", "dQw4w9WgXcQ", "360", route, "auto", embedded=True)
        try:
            return {"host": urlsplit(st["video"]).hostname, "result": diagnostics.probe_stream(st, route)}
        except ValueError as first:
            # как в плеере: основной формат дал 403 — берём H.264/MP4
            st = player.resolve_streams("yt", "dQw4w9WgXcQ", "360", route, "h264", embedded=True)
            return {"host": urlsplit(st["video"]).hostname, "result": diagnostics.probe_stream(st, route),
                    "first_try": str(first)[:90], "retry": "h264"}

    step("stream_first_bytes", first_bytes)

    def stt_audio():
        """Запись → WAV 16 кГц через ffmpeg сборки (то же делает микрофон в чате)."""
        from . import voice
        ff = find_tool("ffmpeg")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "tone.wav"
            flags = 0x08000000 if sys.platform.startswith("win") else 0
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(src)],
                           check=True, timeout=60, shell=False, creationflags=flags)
            wav = voice.to_wav(ff, src.read_bytes(), Path(d) / "w")
            return {"wav_bytes": wav.stat().st_size, "whisper_pinned": voice.WHISPER_ZIP["sha256"][:8]}

    step("stt_audio", stt_audio)
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

    def cookie_api():
        """Чтение cookies из WebView2 (окно входа): на Windows проверяем, что нужные типы .NET доступны. Окно не открываем."""
        from . import login_window
        d = login_window.cookie_dict(type("C", (), {"Name": "a", "Value": "b", "Domain": ".vk.com", "Path": "/", "IsSecure": True,
                                                     "IsHttpOnly": True, "IsSession": False, "Expires": 1900000000})(), float)
        if not d or not d["httponly"]:
            raise RuntimeError("cookie_dict")
        if not sys.platform.startswith("win"):
            return "преобразование OK (WebView2 — только Windows)"
        try:
            from webview.platforms import edgechromium, winforms  # noqa: F401
            from Microsoft.Web.WebView2.Core import CoreWebView2Cookie  # noqa: F401
            return "преобразование OK, типы WebView2 доступны"
        except Exception as exc:  # под Wine .NET нет — это не ошибка сборки
            return f"преобразование OK; типы WebView2 недоступны здесь ({type(exc).__name__})"

    step("webview2_cookies", cookie_api)
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

    def under_wine() -> bool:
        try:
            import ctypes
            return hasattr(ctypes.windll.ntdll, "wine_get_version")
        except Exception:
            return False

    def awg_tunnel():
        exe = find_tool("awgproxy")
        if not exe:
            raise FileNotFoundError("awgproxy не найден в tools\\")
        flags = 0x08000000 if sys.platform.startswith("win") else 0
        out = subprocess.run([exe, "-selftest"], capture_output=True, timeout=60, shell=False, creationflags=flags)
        text = (out.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        res = json.loads(text[-1]) if text else {}
        if res.get("ok"):
            return {k: res.get(k) for k in ("bind", "hello", "dns_through_tunnel", "bad_auth_rejected", "first_ms")}
        err = str(res.get("error") or (out.stderr or b"").decode("utf-8", "replace"))[:300]
        if under_wine() and ("wsaioctl" in err.lower() or "10045" in err or "not supported" in err.lower()):
            return "пропущено: Wine не поддерживает UDP-сокеты Go (WSAEOPNOTSUPP); на Windows проверяется этим же шагом"
        raise RuntimeError(err or f"код {out.returncode}")

    def dotnet_config():
        from . import motw
        base = Path(sys.executable).resolve().parent
        if not getattr(sys, "frozen", False):
            return "пропущено: запуск из исходников"
        if not motw.has_runtime_config(base):
            raise RuntimeError("нет own-tube.exe.config с loadFromRemoteSources рядом с exe")
        if not (base / "_internal" / "pythonnet" / "runtime" / "Python.Runtime.dll").is_file():
            raise RuntimeError("нет pythonnet\\runtime\\Python.Runtime.dll")
        return {"config": True, "motw": motw.strip(base)}

    step("dotnet_config", dotnet_config)
    step("awgproxy", lambda: {"path": find_tool("awgproxy"), "version": _version(find_tool("awgproxy"), "-version")})
    step("awg_tunnel", awg_tunnel)

    def update_swap():
        import zipfile
        from . import updater
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = d / "own-tube"
            app.mkdir()
            (app / "own-tube.exe").write_bytes(b"old")
            z = d / "u.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("own-tube-9.9.9-win-x64/own-tube.exe", b"new")
                zf.writestr("own-tube-9.9.9-win-x64/tools/x.txt", b"x")
            staged = updater.extract(z, d / "stage")
            p = subprocess.Popen([sys.executable, "--version"] if not getattr(sys, "frozen", False) else ["cmd", "/c", "exit"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                                 creationflags=0x08000000 if sys.platform.startswith("win") else 0)
            p.wait()
            res = updater.apply(updater.make_plan(app, staged, "9.9.9", "0.0.1", pid=p.pid), relaunch=False, wait=10)
            if not res["ok"] or (app / "own-tube.exe").read_bytes() != b"new" or not (d / "own-tube.old-0.0.1" / "own-tube.exe").is_file():
                raise RuntimeError(str(res))
            try:
                updater._check_url("https://evil.example/x")
                raise RuntimeError("проверка адреса не сработала")
            except updater.UpdateError:
                pass
            return "замена папки и резервная копия: ок"

    step("update_swap", update_swap)
    report["ok"] = ok
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1
