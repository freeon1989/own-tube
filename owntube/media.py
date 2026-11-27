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
import socket
import logging
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from . import httpc, streamsrc, validate
from .network import Route
from .player import PlayerError, ffmpeg_command

log = logging.getLogger("owntube.media")
IMG_CACHE = 400
IMG_LIMIT = 3 * 1024 * 1024
CHUNK = 64 * 1024
OUT_CHUNK = 256 * 1024
SRC_CHUNK = 256 * 1024
RETRIES_403 = 6
RETRIES_403_SRC = 2  # у локального источника повторы уже внутри (streamsrc), адрес скорее просрочен
MAX_SESSIONS = 4
NET_HINTS = ("502", "timed out", "refused", "Connection reset", "Network is unreachable", "Server returned 5", "I/O error", "End of file")
_URL = re.compile(r"https?://\S+")


class MediaServer:
    def __init__(self, route_fn: Callable[[], Route], ffmpeg_fn: Callable[[], str | None],
                 prefetch_fn: Callable[[], int] | None = None, use_sources: bool = True):
        self._route_fn = route_fn
        self._ffmpeg_fn = ffmpeg_fn
        self._prefetch_fn = prefetch_fn or (lambda: 32)  # МБ вперёд
        self.use_sources = use_sources  # False — прежний путь: ffmpeg сам ходит на адрес потока
        self._sources: dict[tuple[str, str], streamsrc.Source] = {}
        self._warm: collections.OrderedDict[str, streamsrc.Source] = collections.OrderedDict()  # подготовленные заранее (наведение на карточку)
        self.metrics: collections.deque[dict] = collections.deque(maxlen=30)
        self.token = secrets.token_urlsafe(18)
        self._img: collections.OrderedDict[str, tuple[str, bytes]] = collections.OrderedDict()
        self._img_lock = threading.Lock()
        self._sessions: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._refresh: dict[str, Callable[[], dict]] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self.last_error = ""  # последняя ошибка ffmpeg (без секретов) — для самопроверки и журнала
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive: ffmpeg читает файл многими запросами Range

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
        self.close_all_sources()
        self._httpd.shutdown()
        self._httpd.server_close()

    # --- видео ---
    def add_session(self, streams: dict, refresh: Callable[[], dict] | None = None, exclusive: bool = True) -> str:
        """Новый сеанс просмотра. exclusive=True — обычное видео: всё остальное останавливается.
        Для Shorts (exclusive=False) живут до MAX_SESSIONS сеансов: следующий ролик подготовлен заранее.
        refresh — как получить свежие потоки (другой формат), если прежние не открылись."""
        sid = secrets.token_urlsafe(9)
        with self._lock:
            if exclusive:
                self.stop_all_locked()
                for gone in list(self._sessions):
                    self._close_sources_locked(gone)
                self._sessions.clear()
                self._refresh.clear()
            self._sessions[sid] = dict(streams)
            if refresh:
                self._refresh[sid] = refresh
            while len(self._sessions) > MAX_SESSIONS:
                old, _ = self._sessions.popitem(last=False)
                self._refresh.pop(old, None)
                self._close_sources_locked(old)
                proc = self._procs.pop(old, None)
                if proc:
                    _kill(proc)
        if self.use_sources:
            self.warm(sid)
        return sid

    # --- источники потоков (streamsrc) ---
    def _window(self) -> int:
        try:
            return max(4, min(256, int(self._prefetch_fn()))) * streamsrc.MB
        except (TypeError, ValueError):
            return 32 * streamsrc.MB

    def _close_sources_locked(self, sid: str) -> None:
        for key in [k for k in self._sources if k[0] == sid]:
            self._sources.pop(key).close()

    def _source(self, sid: str, kind: str) -> streamsrc.Source | None:
        with self._lock:
            streams = self._sessions.get(sid)
            if not streams or kind not in ("video", "audio") or not streams.get(kind):
                return None
            src = self._sources.get((sid, kind))
            if src is None:
                src = self._warm.pop(streams[kind], None)
                if src is not None:
                    self._sources[(sid, kind)] = src
            if src is None:
                window = self._window
                if kind == "audio":
                    window = lambda: min(self._window(), 8 * streamsrc.MB)  # noqa: E731
                src = streamsrc.Source(streams[kind], self._route_fn, streamsrc.source_headers(streams), window,
                                       kind, audio=kind == "audio")
                self._sources[(sid, kind)] = src
            return src

    def prepare(self, streams: dict) -> None:
        """Качать начало видео и звука, пока ролик ещё не открыт (наведение на карточку, следующий Short)."""
        if not self.use_sources:
            return
        for kind in ("video", "audio"):
            url = streams.get(kind)
            if not url or not validate.is_stream_url(url, streams.get("source", "yt")):
                continue
            with self._lock:
                if url in self._warm or any(s.url == url for s in self._sources.values()):
                    continue
                src = streamsrc.Source(url, self._route_fn, streamsrc.source_headers(streams), self._window, kind,
                                       audio=kind == "audio")
                self._warm[url] = src
                while len(self._warm) > 6:
                    self._warm.popitem(last=False)[1].close()
            src.warm()

    def warm(self, sid: str) -> None:
        """Сразу начать качать начало видео и звука: к моменту, когда <video> придёт за данными, они уже здесь."""
        for kind in ("video", "audio"):
            src = self._source(sid, kind)
            if src:
                src.warm()

    def reset_sources(self, sid: str) -> None:
        with self._lock:
            self._close_sources_locked(sid)

    def source_stats(self, sid: str) -> dict:
        with self._lock:
            items = [(k[1], v) for k, v in self._sources.items() if k[0] == sid]
        return {kind: src.stats() for kind, src in items}

    def drop_session(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
            self._refresh.pop(sid, None)
            self._close_sources_locked(sid)
            proc = self._procs.pop(sid, None)
        if proc:
            _kill(proc)

    def stop_all(self) -> None:
        with self._lock:
            self.stop_all_locked()

    def stop_all_locked(self) -> None:
        for p in list(self._procs.values()):
            _kill(p)
        self._procs.clear()

    def close_all_sources(self) -> None:
        with self._lock:
            for url in list(self._warm):
                self._warm.pop(url).close()
            for key in list(self._sources):
                self._sources.pop(key).close()

    def video_url(self, sid: str, start: float = 0) -> str:
        return f"{self.base}/v/{sid}?t={max(0.0, float(start)):.2f}"

    def _local_urls(self, sid: str, streams: dict) -> dict | None:
        if not self.use_sources:
            return None
        out = {}
        for kind in ("video", "audio"):
            if streams.get(kind):
                if self._source(sid, kind) is None:
                    return None
                out[kind] = f"{self.base}/s/{sid}/{kind}"
        return out or None

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
            if rest.startswith("s/"):
                sid, _, kind = rest[2:].partition("/")
                return self._serve_source(req, sid, kind)
            if rest.startswith("v/"):
                t = (q.get("t") or ["0"])[0]
                return self._video(req, rest[2:], float(t) if t.replace(".", "", 1).isdigit() else 0.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log.warning("Медиасервер (%s): %s", "картинка" if rest == "img" else "видео", validate.redact(str(exc))[:200])
            try:
                _send(req, 502)
            except OSError:
                pass
            return
        _send(req, 404)

    def _serve_source(self, req, sid: str, kind: str) -> None:
        """Файл потока для ffmpeg: Range, Content-Length, данные из упреждающего кэша."""
        src = self._source(sid, kind)
        if src is None:
            return _send(req, 404)
        try:
            total = src.size()
            try:
                rng = streamsrc.parse_range(req.headers.get("Range"), total)
            except ValueError:
                req.send_response(416)
                req.send_header("Content-Range", f"bytes */{total}")
                req.send_header("Content-Length", "0")
                req.end_headers()
                return
            a, b = rng if rng else (0, total - 1)
            rid = id(req)
            first = src.read(a, min(b - a + 1, SRC_CHUNK), rid=rid)  # до заголовков: устаревший адрес станет 403, а не обрывом
            req.send_response(206 if rng else 200)
            req.send_header("Content-Type", "application/octet-stream")
            req.send_header("Accept-Ranges", "bytes")
            req.send_header("Content-Length", str(b - a + 1))
            if rng:
                req.send_header("Content-Range", f"bytes {a}-{b}/{total}")
            req.end_headers()
        except streamsrc.Expired:
            src.release(id(req))
            return _send(req, 403)
        except streamsrc.SourceError as exc:
            src.release(id(req))
            log.info("Источник %s: %s", kind, validate.redact(str(exc))[:120])
            return _send(req, 502)
        pos = a
        try:
            data = first
            while True:
                if not data:
                    break
                req.wfile.write(data)
                pos += len(data)
                if pos > b:
                    break
                data = src.read(pos, min(b - pos + 1, SRC_CHUNK), rid=rid)
        except (streamsrc.SourceError, OSError):
            req.close_connection = True  # заголовки уже ушли: рвём соединение, ffmpeg переспросит
        finally:
            src.release(rid)

    def _image(self, req, url: str) -> None:
        if not validate.is_thumb_url(url):
            return _send(req, 403)
        with self._img_lock:
            hit = self._img.get(url)
            if hit:
                self._img.move_to_end(url)
        if not hit:
            try:
                status, headers, data = httpc.get(url, self._route_fn(), {"Accept": "image/*"}, limit=IMG_LIMIT, timeout=15)
            except OSError as exc:
                host = urlsplit(url).hostname
                log.warning("Обложка (%s) не загрузилась: %s", host, validate.redact(str(exc))[:120])
                return _send(req, 502)
            ctype = headers.get("content-type", "")
            if status != 200 or not ctype.startswith("image/"):
                log.warning("Обложка (%s): HTTP %s", urlsplit(url).hostname, status)
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
        local = self._local_urls(sid, streams)
        cmd = ffmpeg_command(ffmpeg, streams, start, "" if local else proxy, local)
        flags = 0x08000000 if sys.platform.startswith("win") else 0
        host = urlsplit(streams.get("video") or "").hostname or "?"
        refreshed = False
        attempt = 0
        t0 = time.monotonic()
        while True:
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
            first = proc.stdout.read1(OUT_CHUNK)  # что есть: без ожидания полного куска
            if first:
                if attempt or refreshed:
                    log.info("Видео (%s) открылось с попытки %d%s за %.1f с", host, attempt + 1,
                             ", после обновления адреса" if refreshed else "", time.monotonic() - t0)
                break
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill(proc)
            drain.join(timeout=2)
            msg = " ".join(errors) or f"код {proc.returncode}"
            with self._lock:
                current = sid in self._sessions
                refresh = self._refresh.get(sid)
                if self._procs.get(sid) is proc:
                    self._procs.pop(sid, None)
            if not current:
                return _send(req, 410)
            v6_direct = (not proxy and refresh and not refreshed
                         and httpc.family_of(streams.get("video") or "") == socket.AF_INET6)
            if "403" in msg and attempt < (RETRIES_403_SRC if local else RETRIES_403) and not v6_direct:
                attempt += 1
                log.info("ffmpeg: 403 от сервера видео (%s), повтор %d", host, attempt)
                time.sleep(1.5)
                continue
            net = any(h in msg for h in NET_HINTS)
            if (net or "403" in msg or "404" in msg or "410" in msg) and refresh and not refreshed:
                # адрес мог устареть или прокси не дошёл до этого сервера: просим другой формат/адрес и пробуем ещё раз
                refreshed = True
                log.info("Видео (%s): %s — обновляю адрес потока", host, "сеть" if net else "HTTP 4xx")
                try:
                    streams = refresh() or streams
                    with self._lock:
                        if sid in self._sessions:
                            self._sessions[sid] = dict(streams)
                    self.reset_sources(sid)
                    local = self._local_urls(sid, streams)
                    cmd = ffmpeg_command(ffmpeg, streams, start, "" if local else proxy, local)
                    host = urlsplit(streams.get("video") or "").hostname or host
                    continue
                except Exception as exc:
                    log.warning("Не удалось обновить адрес потока: %s", validate.redact(str(exc))[:160])
            if net and attempt < 2:
                attempt += 1
                log.info("ffmpeg: сеть (%s), повтор %d", host, attempt)
                time.sleep(1.0 * attempt)
                continue
            msg = _URL.sub("<адрес потока>", validate.redact(msg))  # в адресе потока есть IP пользователя
            self.last_error = msg[-600:]
            hint = " (прокси не открыл соединение с сервером видео)" if net else ""
            log.warning("ffmpeg не отдал видео с %s%s: %s", host, hint, msg[-300:])
            return _send(req, 502)
        try:
            req.send_response(200)
            req.send_header("Content-Type", "video/webm" if streams.get("container") == "webm" else "video/mp4")
            req.send_header("Cache-Control", "no-store")
            req.send_header("Connection", "close")
            req.end_headers()
            req.wfile.write(first)
            sent = len(first)
            self.metrics.append({"first_ms": int((time.monotonic() - t0) * 1000), "start": start,
                                 "local": bool(local), "t": time.time()})
            while True:
                chunk = proc.stdout.read1(OUT_CHUNK)
                if not chunk:
                    break
                req.wfile.write(chunk)
                sent += len(chunk)
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
