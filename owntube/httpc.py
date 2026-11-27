# -*- coding: utf-8 -*-
"""Небольшой HTTP(S)-клиент поверх маршрута own-tube (SOCKS5/SOCKS4, HTTP CONNECT или напрямую).

Нужен там, где yt-dlp не подходит: картинки, проверка связи, API моделей. Редиректы не выполняются.
Частные адреса (LAN, VPN, localhost) идут напрямую: прокси снаружи до них всё равно не дотянется.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

from . import validate
from .network import Route

LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home", ".intranet")


def is_private_host(host: str) -> bool:
    name = (host or "").lower().strip("[]").rstrip(".")
    if name == "localhost" or any(name.endswith(s) for s in LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def route_for(url: str, route: Route) -> Route:
    """Маршрут для конкретного адреса: частные адреса — напрямую."""
    host = urlsplit(url).hostname or ""
    return Route() if is_private_host(host) else route


def open_tunnel(host: str, port: int, route: Route, timeout: float) -> socket.socket:
    if route.direct:
        return socket.create_connection((host, port), timeout=timeout)
    from .bridge import http_connect, socks_connect

    if validate.is_socks_proxy(route.proxy):
        s = socks_connect(route.proxy, host, port, timeout=timeout, user=route.user, password=route.password)
    else:
        s = http_connect(route.proxy, host, port, timeout=timeout, user=route.user, password=route.password)
    s.settimeout(timeout)
    return s


def ssl_context() -> ssl.SSLContext:
    """Проверка сертификатов всегда включена (тесты подменяют эту функцию своим центром сертификации)."""
    return ssl.create_default_context()


class _HTTP(http.client.HTTPConnection):
    def __init__(self, host, port, route, timeout):
        super().__init__(host, port, timeout=timeout)
        self._route = route

    def connect(self):
        self.sock = open_tunnel(self.host, self.port, self._route, self.timeout)


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, route, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl_context())
        self._route = route

    def connect(self):
        raw = open_tunnel(self.host, self.port, self._route, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def open_url(method: str, url: str, route: Route, headers: dict | None = None, body: bytes | None = None,
             timeout: float = 20, direct_private: bool = True) -> http.client.HTTPResponse:
    """Открывает запрос и возвращает ответ (читать и закрывать — вызывающему)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Поддерживаются только адреса http и https")
    if parts.username or parts.password:
        raise ValueError("Логин в адресе не поддерживается")
    if direct_private:
        route = route_for(url, route)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    cls = _HTTPS if parts.scheme == "https" else _HTTP
    conn = cls(parts.hostname, port, route, timeout)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    hdrs = {"User-Agent": "own-tube", "Accept": "*/*"}
    hdrs.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    resp._owntube_conn = conn  # держим соединение живым, пока читают ответ
    return resp


def get(url: str, route: Route, headers: dict | None = None, limit: int = 4 * 1024 * 1024,
        timeout: float = 20) -> tuple[int, dict, bytes]:
    resp = open_url("GET", url, route, headers, timeout=timeout)
    try:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Слишком большой ответ")
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        resp.close()
