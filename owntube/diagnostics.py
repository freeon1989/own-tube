# -*- coding: utf-8 -*-
"""«Проверить связь» по шагам: TCP до прокси, рукопожатие и логин, HTTPS до YouTube, картинки, поиск yt-dlp."""
from __future__ import annotations

import socket
import time
from typing import Callable
from urllib.parse import urlsplit

from . import httpc, validate
from .network import Route

TEST_THUMB = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"


def _step(name: str, fn: Callable[[], str]) -> dict:
    t = time.monotonic()
    try:
        detail = fn() or "OK"
        ok = True
    except Exception as exc:
        ok = False
        detail = validate.redact(str(exc) or type(exc).__name__)[:300]
    return {"name": name, "ok": ok, "detail": detail, "ms": int((time.monotonic() - t) * 1000)}


def run(route: Route, search_fn=None) -> dict:
    steps: list[dict] = []
    if not route.direct:
        p = urlsplit(route.proxy)

        def tcp():
            s = socket.create_connection((p.hostname, p.port), timeout=8)
            s.close()
            return "Сервер прокси отвечает"

        steps.append(_step("Соединение с прокси (TCP)", tcp))
        if steps[-1]["ok"]:
            def handshake():
                s = httpc.open_tunnel("www.youtube.com", 443, route, 10)
                s.close()
                kind = "SOCKS5" if validate.is_socks_proxy(route.proxy) else "HTTP CONNECT"
                return f"{kind}: " + ("логин и пароль приняты, " if route.user or route.password else "") + \
                       "туннель до www.youtube.com открыт"
            steps.append(_step("Рукопожатие и вход на прокси", handshake))
    if all(s["ok"] for s in steps):
        def yt():
            status, _, _ = httpc.get("https://www.youtube.com/robots.txt", route, limit=64 * 1024, timeout=15)
            if status >= 400:
                raise ValueError(f"youtube.com ответил кодом {status}")
            return f"youtube.com отвечает (HTTP {status}) " + ("через прокси" if not route.direct else "напрямую")
        steps.append(_step("Доступ к youtube.com (HTTPS)", yt))

        def thumb():
            status, _, data = httpc.get(TEST_THUMB, route, limit=512 * 1024, timeout=15)
            if status != 200 or not data:
                raise ValueError(f"i.ytimg.com ответил кодом {status}")
            return f"Обложки грузятся ({len(data) // 1024} КБ)"
        steps.append(_step("Обложки (i.ytimg.com)", thumb))

        if search_fn is not None:
            def search():
                items = search_fn("lofi", route, 3)
                if not items:
                    raise ValueError("Поиск вернул пустой список")
                return f"Поиск через yt-dlp работает: {len(items)} результата"
            steps.append(_step("Поиск yt-dlp", search))
    ok = bool(steps) and all(s["ok"] for s in steps)
    first_bad = next((s for s in steps if not s["ok"]), None)
    detail = ("Всё в порядке: " + route.label) if ok else (f"Сбой на шаге «{first_bad['name']}»: {first_bad['detail']}"
                                                           if first_bad else "Нет шагов")
    return {"ok": ok, "detail": detail, "steps": steps}
