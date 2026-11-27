# -*- coding: utf-8 -*-
"""Небольшой HTTP(S)-клиент поверх маршрута own-tube (SOCKS5/SOCKS4, HTTP CONNECT или напрямую).

Нужен там, где yt-dlp не подходит: картинки, проверка связи, API моделей. Редиректы не выполняются.
Частные адреса (LAN, VPN, localhost) идут напрямую: прокси снаружи до них всё равно не дотянется.
"""
from __future__ import annotations

import http.client
import threading
import time
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

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


def family_of(url: str) -> int:
    """Адрес потока googlevideo привязан к IP, с которого его выдали (параметр ip=). Прямое соединение должно идти с тем же семейством."""
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith(".googlevideo.com"):
        return 0
    from urllib.parse import parse_qs
    ipq = (parse_qs(parts.query).get("ip") or [""])[0]
    return socket.AF_INET6 if ":" in ipq else (socket.AF_INET if ipq else 0)


def _connect_family(host: str, port: int, timeout: float, family: int) -> socket.socket:
    last: OSError | None = None
    for fam, kind, proto, _, addr in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        sock = socket.socket(fam, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last or OSError("нет адреса нужного семейства")


def open_tunnel(host: str, port: int, route: Route, timeout: float, family: int = 0) -> socket.socket:
    if route.direct:
        if family:
            try:
                return _connect_family(host, port, timeout, family)
            except (OSError, socket.gaierror):
                pass  # нужного семейства нет — обычное соединение
        return socket.create_connection((host, port), timeout=timeout)
    from .bridge import connect_via_proxy

    s, _ = connect_via_proxy(route.proxy, host, port, route.user, route.password, timeout=max(timeout, 10))
    s.settimeout(timeout)
    return s


def ssl_context() -> ssl.SSLContext:
    """Проверка сертификатов всегда включена (тесты подменяют эту функцию своим центром сертификации)."""
    return ssl.create_default_context()


class _HTTP(http.client.HTTPConnection):
    def __init__(self, host, port, route, timeout, family=0):
        super().__init__(host, port, timeout=timeout)
        self._route = route
        self._family = family

    def connect(self):
        self.sock = open_tunnel(self.host, self.port, self._route, self.timeout, self._family)


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, route, timeout, family=0):
        super().__init__(host, port, timeout=timeout, context=ssl_context())
        self._route = route
        self._family = family

    def connect(self):
        raw = open_tunnel(self.host, self.port, self._route, self.timeout, self._family)
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
    conn = cls(parts.hostname, port, route, timeout, family_of(url))
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
    return fetch("GET", url, route, headers, None, limit, timeout)


# ---------- пул keep-alive соединений ----------
# Каждое новое соединение через прокси — это рукопожатие SOCKS + TLS (несколько круговых задержек).
# Пул переиспользует уже открытые соединения к youtube.com, i.ytimg.com и т. п. и ограничивает их число.
IDLE_TTL = 25.0
MAX_IDLE_PER_HOST = 6
MAX_PARALLEL_PROXIED = 6
_pool: dict[tuple, list] = {}
_pool_lock = threading.Lock()
_par = threading.BoundedSemaphore(MAX_PARALLEL_PROXIED)
_direct_par = threading.BoundedSemaphore(16)


def _key(parts, route: Route) -> tuple:
    return (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
            route.proxy, route.user, family_of(f"{parts.scheme}://{parts.netloc}{parts.path}?{parts.query}"))


def _take(key: tuple):
    now = time.monotonic()
    with _pool_lock:
        lst = _pool.get(key) or []
        while lst:
            conn, ts = lst.pop()
            if now - ts < IDLE_TTL:
                return conn
            try:
                conn.close()
            except Exception:
                pass
    return None


def _give(key: tuple, conn) -> None:
    with _pool_lock:
        lst = _pool.setdefault(key, [])
        if len(lst) >= MAX_IDLE_PER_HOST:
            old, _ = lst.pop(0)
            try:
                old.close()
            except Exception:
                pass
        lst.append((conn, time.monotonic()))


def close_pool() -> None:
    with _pool_lock:
        items = [c for lst in _pool.values() for c, _ in lst]
        _pool.clear()
    for c in items:
        try:
            c.close()
        except Exception:
            pass


def fetch(method: str, url: str, route: Route, headers: dict | None = None, body: bytes | None = None,
          limit: int = 4 * 1024 * 1024, timeout: float = 20, attempts: int = 3) -> tuple[int, dict, bytes]:
    """Запрос через пул соединений. Читает ответ целиком (до limit), редиректов нет.

    Отказ прокси (перегруз, временный сбой) повторяется с нарастающей паузой."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Поддерживаются только адреса http и https")
    if parts.username or parts.password:
        raise ValueError("Логин в адресе не поддерживается")
    route = route_for(url, route)
    key = _key(parts, route)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    hdrs = {"User-Agent": "own-tube", "Accept": "*/*", "Connection": "keep-alive"}
    hdrs.update(headers or {})
    sem = _direct_par if route.direct else _par
    last: Exception | None = None
    for attempt in range(attempts):
        with sem:
            conn = _take(key)
            reused = conn is not None
            try:
                if conn is None:
                    cls = _HTTPS if parts.scheme == "https" else _HTTP
                    conn = cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), route, timeout, family_of(url))
                conn.timeout = timeout
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read(limit + 1)
                if len(data) > limit:
                    conn.close()
                    raise ValueError("Слишком большой ответ")
                out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
                if resp.will_close:
                    conn.close()
                else:
                    _give(key, conn)
                return out
            except ValueError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                last = exc
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                if reused:
                    continue  # залежавшееся соединение закрыто сервером — пробуем сразу свежее
        time.sleep(0.5 * (attempt + 1))
    raise last if last else OSError("нет соединения")
