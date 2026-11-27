# -*- coding: utf-8 -*-
"""Локальный медиасервер на 127.0.0.1: картинки и видео для окна программы.

* /<token>/img?u=<https-адрес картинки> — превью YouTube/VK через тот же маршрут (прокси), что и всё остальное.
  WebView2 сам в сеть за картинками не ходит: так они грузятся и там, где YouTube доступен только через прокси.
* /<token>/v/<sid>?t=<сек> — видео для <video>: ffmpeg перепаковывает потоки без перекодирования (WebM или
  фрагментированный MP4). Перемотка — новый запрос с другим t.

Слушает только 127.0.0.1, путь начинается со случайного токена; чужие хосты картинок не отдаются.
"""
from __future__ import annotations

import collections
import re
import logging
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from . import httpc, validate
from .network import Route
from .player import PlayerError, ffmpeg_command

log = logging.getLogger("owntube.media")
IMG_CACHE = 400
IMG_LIMIT = 3 * 1024 * 1024
CHUNK = 64 * 1024
RETRIES_403 = 6
_URL = re.compile(r"https?://\S+")


class MediaServer:
    def __init__(self, route_fn: Callable[[], Route], ffmpeg_fn: Callable[[], str | None]):
        self._route_fn = route_fn
        self._ffmpeg_fn = ffmpeg_fn
        self.token = secrets.token_urlsafe(18)
        self._img: collections.OrderedDict[str, tuple[str, bytes]] = collections.OrderedDict()
        self._img_lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self.last_error = ""  # последняя ошибка ffmpeg (без секретов) — для самопроверки и журнала
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # без адресов в журнале
                return

            def do_GET(self):
                server._dispatch(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, name="owntube-media", daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}"

    def close(self) -> None:
        self.stop_all()
        self._httpd.shutdown()
        self._httpd.server_close()

    # --- видео ---
    def add_session(self, streams: dict) -> str:
        sid = secrets.token_urlsafe(9)
        with self._lock:
            self.stop_all_locked()
            self._sessions = {sid: dict(streams)}
        return sid

    def stop_all(self) -> None:
        with self._lock:
            self.stop_all_locked()

    def stop_all_locked(self) -> None:
        for p in list(self._procs.values()):
            _kill(p)
        self._procs.clear()

    def video_url(self, sid: str, start: float = 0) -> str:
        return f"{self.base}/v/{sid}?t={max(0.0, float(start)):.2f}"

    # --- обработчик ---
    def _dispatch(self, req: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(req.path)
        prefix = f"/{self.token}/"
        if not parts.path.startswith(prefix):
            return _send(req, 404)
        rest = parts.path[len(prefix):]
        q = parse_qs(parts.query)
        try:
            if rest == "img":
                return self._image(req, (q.get("u") or [""])[0])
            if rest.startswith("v/"):
                t = (q.get("t") or ["0"])[0]
                return self._video(req, rest[2:], float(t) if t.replace(".", "", 1).isdigit() else 0.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log.warning("Медиасервер: %s", validate.redact(str(exc))[:200])
            try:
                _send(req, 502)
            except OSError:
                pass
            return
        _send(req, 404)

    def _image(self, req, url: str) -> None:
        if not validate.is_thumb_url(url):
            return _send(req, 403)
        with self._img_lock:
            hit = self._img.get(url)
            if hit:
                self._img.move_to_end(url)
        if not hit:
            status, headers, data = httpc.get(url, self._route_fn(), {"Accept": "image/*"}, limit=IMG_LIMIT, timeout=15)
            ctype = headers.get("content-type", "")
            if status != 200 or not ctype.startswith("image/"):
                return _send(req, 502)
            hit = (ctype.split(";")[0], data)
            with self._img_lock:
                self._img[url] = hit
                while len(self._img) > IMG_CACHE:
                    self._img.popitem(last=False)
        req.send_response(200)
        req.send_header("Content-Type", hit[0])
        req.send_header("Content-Length", str(len(hit[1])))
        req.send_header("Cache-Control", "private, max-age=86400")
        req.end_headers()
        req.wfile.write(hit[1])

    def _video(self, req, sid: str, start: float) -> None:
        with self._lock:
            streams = self._sessions.get(sid)
        if not streams:
            return _send(req, 404)
        ffmpeg = self._ffmpeg_fn()
        if not ffmpeg:
            return _send(req, 503)
        route = self._route_fn()
        proxy = route.mpv_proxy() if not route.direct else ""
        cmd = ffmpeg_command(ffmpeg, streams, start, proxy)
        flags = 0x08000000 if sys.platform.startswith("win") else 0
        # googlevideo иногда отвечает 403 в первые секунды после выдачи адреса — пробуем ещё раз
        for attempt in range(RETRIES_403 + 1):
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    shell=False, creationflags=flags, bufsize=CHUNK)  # ждём полный кусок или конец
            errors: collections.deque[str] = collections.deque(maxlen=20)
            drain = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
            drain.start()
            with self._lock:
                old = self._procs.pop(sid, None)
                if old:
                    _kill(old)
                self._procs[sid] = proc
            first = proc.stdout.read(CHUNK)
            if first:
                break
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill(proc)
            drain.join(timeout=2)
            msg = " ".join(errors) or f"код {proc.returncode}"
            with self._lock:
                current = sid in self._sessions
                if self._procs.get(sid) is proc:
                    self._procs.pop(sid, None)
            if "403" in msg and attempt < RETRIES_403 and current:
                log.info("ffmpeg: 403 от сервера видео, повтор %d", attempt + 1)
                time.sleep(1.5)
                continue
            msg = _URL.sub("<адрес потока>", validate.redact(msg))  # в адресе потока есть IP пользователя
            self.last_error = msg[-600:]
            log.warning("ffmpeg не отдал видео: %s", msg[-300:])
            return _send(req, 502)
        try:
            req.send_response(200)
            req.send_header("Content-Type", "video/webm" if streams.get("container") == "webm" else "video/mp4")
            req.send_header("Cache-Control", "no-store")
            req.send_header("Connection", "close")
            req.end_headers()
            req.wfile.write(first)
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                req.wfile.write(chunk)
        finally:
            _kill(proc)
            with self._lock:
                if self._procs.get(sid) is proc:
                    self._procs.pop(sid, None)


def _drain(stream, sink) -> None:
    try:
        for line in iter(stream.readline, b""):
            sink.append(line.decode("utf-8", "replace").strip())
    except (OSError, ValueError):
        pass


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass
    for f in (p.stdout, p.stderr):
        try:
            if f:
                f.close()
        except OSError:
            pass


def _send(req, code: int) -> None:
    req.send_response(code)
    req.send_header("Content-Length", "0")
    req.send_header("Connection", "close")
    req.end_headers()


__all__ = ["MediaServer", "PlayerError"]
