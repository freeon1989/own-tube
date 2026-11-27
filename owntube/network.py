# -*- coding: utf-8 -*-
"""Как трафик приложения выходит в сеть. Единственное место, которое знает про прокси.

Остальной код получает от провайдера маршрут (Route) и просит у него готовые параметры для
yt-dlp и mpv. Сейчас есть один провайдер, ManualProxy: адрес из настроек. Позже сюда же
подключается провайдер «VPN»: выбрать сервер AmneziaWG, поднять локальный прокси только для этого
приложения (остальной трафик компьютера идёт мимо) и проверить связь. Ни адресов серверов,
ни ключей в коде нет: профили будут лежать вне репозитория.
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import urlsplit

from . import validate


class RouteError(Exception):
    """Ошибка маршрута с текстом для пользователя."""


_bridges: dict[str, object] = {}
_bridges_lock = threading.Lock()


def _bridge_for(upstream: str):
    from .bridge import Bridge

    with _bridges_lock:
        b = _bridges.get(upstream)
        if b is None:
            b = _bridges[upstream] = Bridge(upstream)
        return b


@dataclass(frozen=True)
class Route:
    proxy: str = ""   # scheme://host:port без логина и пароля; пусто: напрямую
    label: str = ""   # что показать пользователю, например «Прокси» или имя VPN-профиля

    @property
    def direct(self) -> bool:
        return not self.proxy

    def ytdlp_options(self) -> dict:
        if not self.proxy:
            return {}
        return {"proxy": self.proxy}

    def mpv_proxy(self) -> str:
        """HTTP-прокси для mpv. Для SOCKS поднимается локальный мост HTTP→SOCKS."""
        if not self.proxy:
            return ""
        if validate.is_http_proxy(self.proxy):
            return self.proxy
        try:
            return _bridge_for(self.proxy).url
        except OSError as exc:
            raise RouteError(f"Не удалось запустить мост к SOCKS-прокси: {exc}") from exc

    def mpv_args(self) -> list[str]:
        url = self.mpv_proxy()
        return ["--http-proxy=" + url] if url else []


class RouteProvider(Protocol):
    """Источник маршрута. Будущий VPN-провайдер реализует те же два метода."""

    def current(self) -> Route: ...

    def check(self) -> dict: ...


class ManualProxy:
    """Прокси, который пользователь ввёл в настройках."""

    def __init__(self, get_proxy: Callable[[], str]):
        self._get = get_proxy

    def current(self) -> Route:
        proxy = validate.clean_proxy(self._get())
        return Route(proxy=proxy, label="Прокси" if proxy else "Напрямую")

    def check(self) -> dict:
        """Проверка без внешнего трафика: отвечает ли сам адрес прокси на подключение."""
        route = self.current()
        if route.direct:
            return {"ok": True, "detail": "Прокси не используется"}
        parts = urlsplit(route.proxy)
        try:
            with socket.create_connection((parts.hostname, parts.port), timeout=3):
                pass
        except OSError as exc:
            return {"ok": False, "detail": f"Прокси не отвечает: {exc.strerror or exc}"}
        return {"ok": True, "detail": "Прокси отвечает"}
