# -*- coding: utf-8 -*-
"""Общие настройки yt-dlp и безопасное чтение небольших ответов (RSS, SponsorBlock) через тот же маршрут."""
from __future__ import annotations

import json
import logging

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
    import yt_dlp
    from yt_dlp.networking import Request

    with yt_dlp.YoutubeDL(base_options(route)) as ydl:
        with ydl.urlopen(Request(url, headers={"Accept": accept})) as resp:
            body = resp.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("Слишком большой ответ")
    return body


def fetch_json(url: str, route: Route):
    return json.loads(fetch(url, route, "application/json").decode("utf-8"))
