# -*- coding: utf-8 -*-
"""Источник потока с упреждающей загрузкой: кусочки большого файла качаются параллельно и заранее.

Зачем. ffmpeg, читающий адрес googlevideo напрямую через прокси, делает один длинный запрос на одном соединении.
Сервер видео душит такие запросы (скорость падает до «чуть выше битрейта» после короткого всплеска), а один
TCP-поток через далёкий прокси или туннель редко набирает больше нескольких Мбит/с. Здесь файл читается
кусками по 256 КБ…2 МБ: до 4 запросов одновременно, они идут по уже открытым keep-alive-соединениям пула
(httpc), и каждый короткий, как у yt-dlp с http_chunk_size, — душить нечего. Готовые куски лежат в памяти
в пределах «предзагрузки»; перемотка внутри окна не ходит в сеть.

ffmpeg получает от локального сервера обычный файл с Range и Content-Length и не знает ни про прокси,
ни про маршрут.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from . import httpc
from .network import Route

log = logging.getLogger("owntube.streamsrc")

# Границы кусков: первые маленькие (быстрый старт), дальше по 2 МБ. Сетка фиксирована, поэтому кэш переживает перемотки.
KB = 1024
MB = 1024 * 1024
HEAD = (128 * KB, 256 * KB, 512 * KB)
BODY = 512 * KB
AUDIO_HEAD = (64 * KB, 128 * KB, 256 * KB)
AUDIO_BODY = 256 * KB
PARALLEL = 6
RETRIES = 3
GOOGLE = ".googlevideo.com"
STALE = 15.0  # секунд: читатель без чтений не учитывается
PREWARM = 2 * MB  # сколько качать заранее, пока никто ещё не читает (подготовленные Shorts, наведение на карточку)


class SourceError(Exception):
    pass


class Expired(SourceError):
    """Адрес потока отклонён (403/404/410): нужен свежий."""


def _bounds(i: int, head: tuple = HEAD, body: int = BODY) -> tuple[int, int]:
    """Начало и конец (не включая) куска с номером i."""
    if i < len(head):
        a = sum(head[:i])
        return a, a + head[i]
    a = sum(head) + (i - len(head)) * body
    return a, a + body


def chunk_of(pos: int, head: tuple = HEAD, body: int = BODY) -> int:
    top = sum(head)
    if pos < top:
        a = 0
        for i, n in enumerate(head):
            if pos < a + n:
                return i
            a += n
    return len(head) + (pos - top) // body


def _with_range(url: str, a: int, b: int) -> str:
    """googlevideo принимает диапазон в адресе (&range=a-b): так делает yt-dlp (http_chunk_size)."""
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "range"]
    q.append(("range", f"{a}-{b - 1}"))
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


class Source:
    """Один файл (видео или звук) с упреждающей загрузкой. Потокобезопасен: читают несколько потоков ffmpeg."""

    def __init__(self, url: str, route_fn: Callable[[], Route], headers: dict | None = None,
                 window_fn: Callable[[], int] = lambda: 32 * MB, name: str = "stream", audio: bool = False,
                 parallel: int = PARALLEL):
        self.url = url
        self.parallel = max(1, parallel)
        # звук — 128 кбит/с: куски поменьше, иначе после перемотки ждали бы 2 МБ ≈ 2 минуты звука
        self.head, self.body = (AUDIO_HEAD, AUDIO_BODY) if audio else (HEAD, BODY)
        self._route_fn = route_fn
        self._headers = dict(headers or {})
        self._window_fn = window_fn
        self.name = name
        host = (urlsplit(url).hostname or "")
        self._google = host.endswith(GOOGLE)
        self.host = host
        self.total: int | None = None
        clen = (parse_qs(urlsplit(url).query).get("clen") or [""])[0]
        if clen.isdigit() and int(clen) > 0:
            self.total = int(clen)
        self._cv = threading.Condition()
        self._have: dict[int, bytes] = {}
        self._busy: set[int] = set()
        self._err: dict[int, Exception] = {}
        self._rd: dict[int, list] = {}  # читатели (соединения ffmpeg): номер -> [позиция, время последнего чтения]
        self._last_pos = 0
        self._started = False  # пока никто не читал — качаем только начало
        self._closed = False
        self._workers = 0
        self.bytes_fetched = 0
        self.requests = 0
        self.first_ms: float | None = None
        self._t0 = time.monotonic()

    def _b(self, i: int) -> tuple[int, int]:
        return _bounds(i, self.head, self.body)

    def _c(self, pos: int) -> int:
        return chunk_of(pos, self.head, self.body)

    # --- сеть ---
    def _fetch(self, i: int) -> bytes:
        a, b = self._b(i)
        if self.total is not None:
            b = min(b, self.total)
            if a >= b:
                return b""
        hdr = dict(self._headers)
        hdr["Accept"] = "*/*"
        url = self.url
        if self._google:
            url = _with_range(url, a, b)  # без Range-заголовка: так сервер отдаёт ровно этот кусок
        else:
            hdr["Range"] = f"bytes={a}-{b - 1}"
        status, resp, data = httpc.fetch("GET", url, self._route_fn(), hdr, limit=self.body + 64 * KB, timeout=30)
        self.requests += 1
        if status in (403, 404, 410):
            raise Expired(f"HTTP {status}")
        if status not in (200, 206):
            raise SourceError(f"HTTP {status}")
        if status == 200 and not self._google:
            raise SourceError("сервер не поддерживает Range")
        cr = resp.get("content-range", "")
        if "/" in cr and self.total is None:
            tail = cr.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                self.total = int(tail)
        if self.total is None and len(data) < b - a:
            self.total = a + len(data)  # короткий ответ: файл закончился
        if self.total is None:
            self.total = self._probe_total(hdr)
        if not data:
            raise SourceError("пустой ответ")
        if self.first_ms is None:
            self.first_ms = (time.monotonic() - self._t0) * 1000
        self.bytes_fetched += len(data)
        return data

    def _probe_total(self, hdr: dict) -> int | None:
        """Размер файла, если адрес не содержит clen и ответ его не назвал: Range 0-0 отдаёт Content-Range."""
        h = dict(hdr)
        h["Range"] = "bytes=0-0"
        status, resp, _ = httpc.fetch("GET", self.url, self._route_fn(), h, limit=4 * KB, timeout=20)
        cr = resp.get("content-range", "")
        tail = cr.rsplit("/", 1)[1].strip() if "/" in cr else ""
        return int(tail) if status == 206 and tail.isdigit() else None

    def _work(self, i: int) -> None:
        err: Exception | None = None
        data = b""
        for attempt in range(RETRIES):
            if self._closed:
                break
            try:
                data = self._fetch(i)
                err = None
                break
            except Expired as exc:
                err = exc
                break
            except (OSError, SourceError, ValueError) as exc:
                err = exc
                time.sleep(0.4 * (attempt + 1))
        with self._cv:
            self._busy.discard(i)
            self._workers -= 1
            if err is None:
                self._have[i] = data
                self._err.pop(i, None)
            else:
                self._err[i] = err
            self._cv.notify_all()
            self._schedule_locked()

    def _window(self) -> int:
        return max(self._window_fn(), 2 * MB) if self._started else PREWARM

    def _positions_locked(self) -> list[int]:
        """Места чтения, самое свежее первым. У ffmpeg бывает два соединения (заголовок и перемотка): окно держим у каждого,
        но недавнему отдаём приоритет; давно молчащие не учитываем."""
        now = time.monotonic()
        live = sorted((v for v in self._rd.values() if now - v[1] < STALE), key=lambda v: -v[1])
        return [v[0] for v in live] or [self._last_pos]

    def _wanted_locked(self) -> list[int]:
        """Куски окон, которых нет и которые не качаются, в порядке чтения."""
        out = []
        for pos in self._positions_locked():
            limit = pos + self._window()
            i = self._c(pos)
            while True:
                a, _ = self._b(i)
                if a >= limit or (self.total is not None and a >= self.total):
                    break
                if i not in self._have and i not in self._busy and i not in self._err and i not in out:
                    out.append(i)
                i += 1
        return out

    def _start_locked(self, i: int) -> None:
        self._busy.add(i)
        self._workers += 1
        threading.Thread(target=self._work, args=(i,), name="owntube-chunk", daemon=True).start()

    def _schedule_locked(self) -> None:
        if self._closed:
            return
        for i in self._wanted_locked():
            if self._workers >= self.parallel:
                break
            self._start_locked(i)
        self._evict_locked()

    def _evict_locked(self) -> None:
        """Память: держим окно вперёд, немного назад (перемотка на несколько секунд) и начало файла."""
        spans = [(self._c(max(0, p - 4 * MB)), self._c(p + self._window()) + 2) for p in self._positions_locked()]
        for i in list(self._have):
            if i >= len(self.head) and not any(a <= i <= b for a, b in spans):
                self._have.pop(i, None)
        for i in list(self._err):
            if not any(a <= i <= b for a, b in spans):
                self._err.pop(i, None)

    # --- чтение ---
    def warm(self) -> None:
        """Начать загрузку с начала файла, не дожидаясь читателя."""
        with self._cv:
            self._schedule_locked()

    def size(self, timeout: float = 30) -> int:
        """Размер файла (из clen адреса или из первого ответа)."""
        deadline = time.monotonic() + timeout
        with self._cv:
            self._schedule_locked()
            while self.total is None:
                e = self._err.get(self._c(self._last_pos))
                if e is not None:
                    self._err.pop(self._c(self._last_pos), None)
                    raise e
                if self._closed:
                    raise SourceError("закрыто")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SourceError("сервер не ответил")
                self._cv.wait(min(left, 1.0))
            return self.total

    def release(self, rid: int) -> None:
        with self._cv:
            self._rd.pop(rid, None)

    def read(self, pos: int, n: int, timeout: float = 45, rid: int = 0) -> bytes:
        """Не более n байт с позиции pos (ждёт нужный кусок). Пустой результат — конец файла."""
        deadline = time.monotonic() + timeout
        with self._cv:
            self._started = True
            self._rd[rid] = [pos, time.monotonic()]
            self._last_pos = pos
            if self.total is not None and pos >= self.total:
                return b""
            i = self._c(pos)
            self._schedule_locked()
            while i not in self._have:
                e = self._err.get(i)
                if e is not None:
                    self._err.pop(i, None)
                    raise e
                if self._closed:
                    raise SourceError("закрыто")
                if i not in self._busy:
                    self._start_locked(i)  # после перемотки нужного куска нет в окне: качаем вне очереди
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SourceError("сервер видео не ответил вовремя")
                self._cv.wait(min(left, 1.0))
            a, _ = self._b(i)
            data = self._have[i]
            off = pos - a
            out = data[off:off + n]
            self._rd[rid] = [pos + len(out), time.monotonic()]
            self._last_pos = pos + len(out)
            self._schedule_locked()
            return out

    def buffered_ahead(self) -> int:
        """Сколько байт подряд уже лежит впереди читателя."""
        with self._cv:
            pos = self._last_pos
            i = self._c(pos)
            n = 0
            while i in self._have:
                a, _ = self._b(i)
                n += len(self._have[i]) - max(0, pos - a) if n == 0 else len(self._have[i])
                i += 1
            return n

    def stats(self) -> dict:
        return {"requests": self.requests, "bytes": self.bytes_fetched, "first_ms": self.first_ms,
                "ahead": self.buffered_ahead(), "total": self.total}

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._have.clear()
            self._cv.notify_all()


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """«bytes=a-b» → (a, b включительно). None — заголовка нет; ValueError — диапазон неверен."""
    if not header:
        return None
    h = header.strip().lower()
    if not h.startswith("bytes=") or "," in h:
        raise ValueError("range")
    a, _, b = h[6:].partition("-")
    if a == "":
        n = int(b)  # последние n байт
        if n <= 0:
            raise ValueError("range")
        return max(0, total - n), total - 1
    start = int(a)
    end = int(b) if b else total - 1
    if start >= total or end < start:
        raise ValueError("range")
    return start, min(end, total - 1)


def source_headers(streams: dict) -> dict:
    h = {}
    if streams.get("user_agent"):
        h["User-Agent"] = streams["user_agent"]
    if streams.get("referer"):
        h["Referer"] = streams["referer"]
    return h


__all__ = ["Source", "SourceError", "Expired", "parse_range", "source_headers", "chunk_of"]
