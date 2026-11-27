# -*- coding: utf-8 -*-
"""Общие настройки yt-dlp и безопасное чтение небольших ответов (RSS, SponsorBlock) через тот же маршрут."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from typing import Callable

from .network import Route
from .tools import find_tool

MAX_BODY = 2 * 1024 * 1024


class _Log:
    """Сообщения yt-dlp — в журнал программы, а не в консоль."""
    _log = logging.getLogger("owntube.yt-dlp")

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self._log.debug(msg)

    info = debug

    def warning(self, msg):
        self._log.info(msg)

    def error(self, msg):
        self._log.warning(msg)


_cookie_provider: Callable[[], str] | None = None


def set_cookie_provider(fn: Callable[[], str] | None) -> None:
    """Источник cookies вошедших аккаунтов (текст Netscape). Задаёт Api."""
    global _cookie_provider
    _cookie_provider = fn


@contextlib.contextmanager
def YDL(opts: dict):
    """yt_dlp.YoutubeDL с cookies аккаунтов во временном файле (права 600), удаляется сразу после."""
    import yt_dlp

    text = ""
    if _cookie_provider is not None:
        try:
            text = _cookie_provider() or ""
        except Exception:
            text = ""
    path = None
    if text:
        fd, path = tempfile.mkstemp(prefix="owntube-c-", suffix=".txt")
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        opts = {**opts, "cookiefile": path}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def base_options(route: Route, **extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 2,
        "check_formats": False,
        "logger": _Log(),
    }
    opts.update(route.ytdlp_options())
    deno = find_tool("deno")
    if deno:
        # JS-движок для расшифровки подписей YouTube; без него часть форматов недоступна
        opts["js_runtimes"] = {"deno": {"path": deno}}
    opts.update(extra)
    return opts


def fetch(url: str, route: Route, accept: str = "*/*") -> bytes:
    """GET через сетевой стек yt-dlp (понимает и HTTP, и SOCKS-прокси). Ответ ограничен по размеру."""
    from yt_dlp.networking import Request

    with YDL(base_options(route)) as ydl:
        with ydl.urlopen(Request(url, headers={"Accept": accept})) as resp:
            body = resp.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("Слишком большой ответ")
    return body


def fetch_json(url: str, route: Route):
    return json.loads(fetch(url, route, "application/json").decode("utf-8"))
