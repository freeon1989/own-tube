# -*- coding: utf-8 -*-
"""Мост HTTP→SOCKS (или HTTP-прокси с авторизацией) для mpv.

mpv (ffmpeg) умеет только HTTP-прокси без удобной передачи пароля в командной строке.
Если в настройках SOCKS или HTTP с логином, поднимаем на 127.0.0.1 маленький HTTP-прокси,
который понимает только CONNECT и только к хостам видео (YouTube/VK), а дальше идёт через
заданный прокси. Чужие хосты получают 403. Логин и пароль в командную строку mpv не попадают.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import select
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

from . import validate

log = logging.getLogger("owntube.bridge")

ALLOWED_SUFFIXES = tuple(s for hosts in validate.STREAM_HOSTS.values() for s in hosts)
ALLOWED_PORTS = (443,)
MAX_HEADER = 8192


class SocksError(OSError):
    """Ошибка прокси. code — ответ SOCKS5 (REP), если он был: 1 сбой, 2 запрещено, 3 сеть, 4 хост, 5 отказ, 8 тип адреса."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksError("Прокси закрыл соединение")
        buf += chunk
    return buf


def socks_connect(proxy: str, host: str, port: int, timeout: float = 15,
                  user: str = "", password: str = "", ip: str | None = None) -> socket.socket:
    """TCP к host:port через SOCKS4a/5. Для SOCKS5 с логином — RFC 1929. Ошибки без секретов.

    По умолчанию имя хоста уходит прокси как есть (ATYP=3, как socks5h). ip — запасной путь: прокси получает адрес
    (ATYP=1/4), если сам не смог соединиться по имени."""
    p = urlsplit(proxy)
    scheme = (p.scheme or "").lower()
    if not p.hostname or not p.port:
        raise SocksError("Некорректный адрес прокси")
    s = socket.create_connection((p.hostname, p.port), timeout=timeout)
    try:
        if scheme in ("socks5", "socks5h"):
            if user or password:
                s.sendall(b"\x05\x02\x00\x02")  # no-auth и username/password
            else:
                s.sendall(b"\x05\x01\x00")
            greet = _recv_exact(s, 2)
            if greet[0] != 5:
                raise SocksError("SOCKS5: неожиданный ответ")
            method = greet[1]
            if method == 0xFF:
                raise SocksError("Прокси требует логин и пароль" if not (user or password)
                                 else "Прокси не принял предложенный способ входа")
            if method == 0x02:
                ub = (user or "").encode("utf-8")
                pb = (password or "").encode("utf-8")
                if len(ub) > 255 or len(pb) > 255:
                    raise SocksError("Логин или пароль слишком длинные")
                s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
                auth = _recv_exact(s, 2)
                if auth[0] != 1 or auth[1] != 0:
                    raise SocksError("Неверный логин или пароль")
            elif method != 0x00:
                raise SocksError("SOCKS5: прокси требует логин или не поддерживает подключение")
            if ip:
                addr = ipaddress.ip_address(ip)
                atyp = b"\x01" if addr.version == 4 else b"\x04"
                s.sendall(b"\x05\x01\x00" + atyp + addr.packed + struct.pack(">H", port))
            else:
                name = host.encode("idna")
                if len(name) > 255:
                    raise SocksError("слишком длинное имя хоста")
                s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
            head = _recv_exact(s, 4)
            if head[1] != 0:
                raise SocksError(f"SOCKS5: отказ подключения (код {head[1]})", head[1])
            atyp = head[3]
            if atyp == 1:
                _recv_exact(s, 4 + 2)
            elif atyp == 4:
                _recv_exact(s, 16 + 2)
            elif atyp == 3:
                _recv_exact(s, _recv_exact(s, 1)[0] + 2)
            else:
                raise SocksError("SOCKS5: неизвестный ответ")
        elif scheme in ("socks4", "socks4a"):
            if ip and ipaddress.ip_address(ip).version == 4:
                s.sendall(b"\x04\x01" + struct.pack(">H", port) + ipaddress.ip_address(ip).packed + b"\x00")
            else:
                s.sendall(b"\x04\x01" + struct.pack(">H", port) + b"\x00\x00\x00\x01" + b"\x00"
                          + host.encode("idna") + b"\x00")
            resp = _recv_exact(s, 8)
            if resp[1] != 0x5A:
                raise SocksError("SOCKS4: отказ подключения")
        else:
            raise SocksError("не SOCKS-прокси")
        s.settimeout(None)
        return s
    except BaseException:
        s.close()
        raise


def http_connect(proxy: str, host: str, port: int, timeout: float = 15,
                 user: str = "", password: str = "") -> socket.socket:
    """CONNECT через HTTP-прокси. Заголовок авторизации не попадает в текст ошибок."""
    p = urlsplit(proxy)
    if not p.hostname or not p.port:
        raise SocksError("Некорректный адрес прокси")
    s = socket.create_connection((p.hostname, p.port), timeout=timeout)
    try:
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if user or password:
            token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            req += f"Proxy-Authorization: Basic {token}\r\n"
        req += "\r\n"
        s.sendall(req.encode("latin-1"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                raise SocksError("HTTP-прокси закрыл соединение")
            data += chunk
            if len(data) > MAX_HEADER:
                raise SocksError("HTTP-прокси вернул слишком длинный ответ")
        line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split(" ", 2)
        code = parts[1] if len(parts) >= 2 else ""
        if code != "200":
            if code in ("407", "401"):
                raise SocksError("Неверный логин или пароль")
            raise SocksError("HTTP-прокси отклонил подключение" + (f" (код {code})" if code else ""))
        s.settimeout(None)
        return s
    except BaseException:
        s.close()
        raise


def host_allowed(host: str, port: int, suffixes=None) -> bool:
    if port not in ALLOWED_PORTS or not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # только имена хостов CDN, не голые адреса
    except ValueError:
        pass
    return validate.host_matches(host.lower().rstrip("."), suffixes or ALLOWED_SUFFIXES)


def _pipe(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x or not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


class Bridge:
    def __init__(self, upstream: str, user: str = "", password: str = "", allowed=None):
        self.upstream = upstream
        self._allowed = tuple(allowed) if allowed else ALLOWED_SUFFIXES
        self._user = user
        self._password = password
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(32)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._loop, name="owntube-bridge", daemon=True).start()

    def credentials(self, user: str, password: str) -> bool:
        return self._user == user and self._password == password

    def __repr__(self) -> str:
        return f"Bridge({self.upstream!r}, user={self._user!r})"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _upstream(self, host: str, port: int):
        sock, _ = connect_via_proxy(self.upstream, host, port, self._user, self._password)
        return sock

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(15)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(1024)
                if not chunk:
                    client.close()
                    return
                data += chunk
                if len(data) > MAX_HEADER:
                    raise ValueError("слишком длинный запрос")
            line = data.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split(" ")
            if len(parts) != 3 or parts[0] != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                client.close()
                return
            host, _, port_s = parts[1].rpartition(":")
            host = host.strip("[]")
            port = int(port_s) if port_s.isdigit() else 0
            if not host_allowed(host, port, self._allowed):
                log.warning("Мост: отказ для %s:%s", host, port)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                client.close()
                return
            upstream = self._upstream(host, port)
            client.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _pipe(client, upstream)
        except Exception as exc:  # одно соединение не должно ронять мост
            log.warning("Мост: %s", validate.redact(str(exc))[:160])
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass


# ---------- надёжное соединение через прокси ----------
_RETRY_CODES = (1, 3, 4, 5, 6)         # «сбой», «сеть недоступна», «хост», «отказ», «TTL»
_NOT_IP_CODES = (7, 8)                  # команда / тип адреса не поддерживаются
_dns_cache: dict[str, tuple[float, list[str]]] = {}
_dns_lock = threading.Lock()
_fail_log: dict[str, float] = {}
DNS_TTL = 600
_slots = threading.BoundedSemaphore(10)  # одновременных рукопожатий с прокси (у 3proxy и подобных есть лимиты)


def _doh_a(proxy: str, user: str, password: str, host: str) -> list[str]:
    """A-записи, как их видит сам прокси: DNS-over-HTTPS по имени cloudflare-dns.com через тот же прокси."""
    import http.client
    import json
    import ssl

    raw = _connect_once(proxy, user, password, "cloudflare-dns.com", 443, 8, None)
    try:
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(raw, server_hostname="cloudflare-dns.com")
        conn = http.client.HTTPSConnection("cloudflare-dns.com", 443, timeout=8, context=ctx)
        conn.sock = tls
        conn.request("GET", f"/dns-query?name={host}&type=A", headers={"Accept": "application/dns-json"})
        data = json.loads(conn.getresponse().read(65536).decode("utf-8", "replace"))
        conn.close()
    finally:
        try:
            raw.close()
        except OSError:
            pass
    out = []
    for a in data.get("Answer") or []:
        if a.get("type") == 1:
            try:
                if ipaddress.ip_address(a.get("data")).version == 4:
                    out.append(a["data"])
            except ValueError:
                continue
    return out


def _local_a(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(i[4][0] for i in infos))


def _is_local_proxy(proxy: str) -> bool:
    return (urlsplit(proxy).hostname or "") in ("127.0.0.1", "localhost", "::1")


def _resolve_ipv4(proxy: str, user: str, password: str, host: str) -> tuple[list[str], str]:
    now = time.time()
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit and now - hit[0] < DNS_TTL:
            return hit[1], "кэш"
    ips: list[str] = []
    how = ""
    try:
        ips, how = _doh_a(proxy, user, password, host), "DoH через прокси"
    except Exception as exc:
        log.debug("DoH через прокси не сработал: %s", validate.redact(str(exc))[:100])
    if not ips and not _is_local_proxy(proxy):
        # прокси на 127.0.0.1 — это встроенный VPN или локальный клиент: DNS мимо туннеля не спрашиваем
        ips, how = _local_a(host), "локальный DNS"
    if ips:
        with _dns_lock:
            _dns_cache[host] = (now, ips)
    return ips, how


def _connect_once(proxy, user, password, host, port, timeout, ip):
    scheme = (urlsplit(proxy).scheme or "").lower()
    if scheme in ("http", "https"):
        return http_connect(proxy, host, port, timeout=timeout, user=user, password=password)
    return socks_connect(proxy, host, port, timeout=timeout, user=user, password=password, ip=ip)


def _note(host: str, text: str) -> None:
    """Одна строка в журнал на хост не чаще раза в 30 секунд: без адресов и секретов."""
    now = time.time()
    if now - _fail_log.get(host + text[:20], 0) > 30:
        _fail_log[host + text[:20]] = now
        log.warning("Прокси: %s", text)


LAST: dict[str, dict] = {}   # хост -> как удалось соединиться в последний раз (для журнала и «Проверить связь»)


def connect_via_proxy(proxy: str, host: str, port: int, user: str = "", password: str = "",
                      timeout: float = 20) -> tuple[socket.socket, dict]:
    sock, info = _connect_via_proxy(proxy, host, port, user, password, timeout)
    LAST[host] = dict(info)
    return sock, info


def _connect_via_proxy(proxy: str, host: str, port: int, user: str = "", password: str = "",
                       timeout: float = 20) -> tuple[socket.socket, dict]:
    """Соединение с host:port через прокси. Возвращает (сокет, сведения о способе).

    1) имя хоста целиком (ATYP=3); 2) при временном отказе — повтор; 3) если прокси всё равно не смог дойти
    (REP 4/5…), адрес берём сами (DoH через этот же прокси, иначе локальный DNS) и отдаём ему IPv4 (ATYP=1)."""
    info = {"host": host, "port": port, "atyp": "domain", "tries": 1}
    with _slots:
        try:
            return _connect_once(proxy, user, password, host, port, timeout, None), info
        except SocksError as exc:
            first = exc
        except socket.timeout as exc:
            first = SocksError("таймаут соединения с прокси")
            first.__cause__ = exc
        if getattr(first, "code", None) is None or first.code not in _RETRY_CODES:
            if not isinstance(first.__cause__, socket.timeout):
                raise first
        time.sleep(0.4)
        info["tries"] = 2
        try:
            return _connect_once(proxy, user, password, host, port, timeout, None), info
        except OSError as exc:
            second = exc
        scheme = (urlsplit(proxy).scheme or "").lower()
        if scheme.startswith("socks"):
            ips, how = _resolve_ipv4(proxy, user, password, host)
            for ip in ips[:3]:
                info.update(atyp="ipv4", tries=info["tries"] + 1, dns=how)
                try:
                    return _connect_once(proxy, user, password, host, port, timeout, ip), info
                except SocksError as exc:
                    if exc.code in _NOT_IP_CODES:
                        break
                    second = exc
                except OSError as exc:
                    second = exc
        code = getattr(second, "code", None)
        _note(host, f"не удалось открыть {host}:{port} (по имени: код {getattr(first, 'code', '?')}, "
                    f"повтор: код {code if code is not None else '?'}, попыток {info['tries']})")
        raise second
