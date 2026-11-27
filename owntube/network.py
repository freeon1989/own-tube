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
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import urlsplit

from . import validate


class RouteError(Exception):
    """Ошибка маршрута с текстом для пользователя."""


_bridges: dict[tuple[str, str], object] = {}
_bridges_lock = threading.Lock()


def _bridge_for(route: "Route"):
    from .bridge import Bridge

    key = (route.proxy, route.user)
    with _bridges_lock:
        b = _bridges.get(key)
        if b is not None and getattr(b, "credentials", lambda u, p: False)(route.user, route.password):
            return b
        if b is not None:
            try:
                b.close()
            except OSError:
                pass
        b = _bridges[key] = Bridge(route.proxy, route.user, route.password)
        return b


@dataclass(frozen=True)
class Route:
    proxy: str = ""   # scheme://host:port без логина и пароля; пусто: напрямую
    label: str = ""   # что показать пользователю, например «Прокси» или имя VPN-профиля
    user: str = ""
    password: str = field(default="", repr=False, compare=False)

    @property
    def direct(self) -> bool:
        return not self.proxy

    def ytdlp_options(self) -> dict:
        if not self.proxy:
            return {}
        url = self.proxy_url_with_auth()
        # DNS тоже через прокси, иначе запросы к YouTube обойдут SOCKS
        if url.startswith("socks5://"):
            url = "socks5h://" + url[len("socks5://"):]
        return {"proxy": url}

    def proxy_url_with_auth(self) -> str:
        """URL с логином и паролем. Только в памяти, не логировать."""
        return validate.proxy_url_with_auth(self.proxy, self.user, self.password)

    def mpv_proxy(self) -> str:
        """HTTP-прокси для mpv. Для SOCKS или HTTP с логином поднимается локальный мост без секретов."""
        if not self.proxy:
            return ""
        if validate.is_http_proxy(self.proxy) and not self.user and not self.password:
            return self.proxy
        try:
            return _bridge_for(self).url
        except OSError as exc:
            raise RouteError(f"Не удалось запустить мост к прокси: {exc}") from exc

    def mpv_args(self) -> list[str]:
        url = self.mpv_proxy()
        return ["--http-proxy=" + url] if url else []


CHECK_HOST = "i.ytimg.com"
CHECK_PORT = 443


class RouteProvider(Protocol):
    """Источник маршрута. Будущий VPN-провайдер реализует те же два метода."""

    def current(self) -> Route: ...

    def check(self) -> dict: ...


class ManualProxy:
    """Прокси, который пользователь ввёл в настройках."""

    def __init__(self, get_proxy: Callable[[], str], get_user: Callable[[], str] | None = None,
                 get_password: Callable[[], str] | None = None):
        self._get = get_proxy
        self._get_user = get_user or (lambda: "")
        self._get_password = get_password or (lambda: "")

    def current(self) -> Route:
        proxy = validate.clean_proxy(self._get())
        user = validate.clean_proxy_user(self._get_user())
        password = self._get_password() or ""
        if proxy:
            kind = "SOCKS-прокси" if validate.is_socks_proxy(proxy) else "HTTP-прокси"
            label = kind + (f" ({user})" if user else "")
        else:
            label = "Напрямую"
        return Route(proxy=proxy, label=label, user=user, password=password)

    def check(self) -> dict:
        """Настоящая проверка связи: рукопожатие (с логином, если есть) и выход в сеть."""
        route = self.current()
        if route.direct:
            return {"ok": True, "detail": "Прокси не задан — соединение идёт напрямую"}
        from .bridge import SocksError, http_connect, socks_connect

        try:
            if validate.is_socks_proxy(route.proxy):
                sock = socks_connect(route.proxy, CHECK_HOST, CHECK_PORT, timeout=8,
                                     user=route.user, password=route.password)
            else:
                sock = http_connect(route.proxy, CHECK_HOST, CHECK_PORT, timeout=8,
                                    user=route.user, password=route.password)
            sock.close()
        except SocksError as exc:
            return {"ok": False, "detail": validate.redact(str(exc))}
        except OSError as exc:
            return {"ok": False, "detail": "Прокси не отвечает: " + (exc.strerror or str(exc))}
        extra = "логин принят, " if route.user or route.password else ""
        return {"ok": True, "detail": f"Связь есть: {extra}прокси пускает к YouTube"}
