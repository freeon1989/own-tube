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
from urllib.parse import urlsplit

from . import validate

log = logging.getLogger("owntube.bridge")

ALLOWED_SUFFIXES = tuple(s for hosts in validate.STREAM_HOSTS.values() for s in hosts)
ALLOWED_PORTS = (443,)
MAX_HEADER = 8192


class SocksError(OSError):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksError("Прокси закрыл соединение")
        buf += chunk
    return buf


def socks_connect(proxy: str, host: str, port: int, timeout: float = 15,
                  user: str = "", password: str = "") -> socket.socket:
    """TCP к host:port через SOCKS4a/5. Для SOCKS5 с логином — RFC 1929. Ошибки без секретов."""
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
            name = host.encode("idna")
            if len(name) > 255:
                raise SocksError("слишком длинное имя хоста")
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
            head = _recv_exact(s, 4)
            if head[1] != 0:
                raise SocksError(f"SOCKS5: отказ подключения (код {head[1]})")
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


def host_allowed(host: str, port: int) -> bool:
    if port not in ALLOWED_PORTS or not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # только имена хостов CDN, не голые адреса
    except ValueError:
        pass
    return validate.host_matches(host.lower().rstrip("."), ALLOWED_SUFFIXES)


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
    def __init__(self, upstream: str, user: str = "", password: str = ""):
        self.upstream = upstream
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
        scheme = (urlsplit(self.upstream).scheme or "").lower()
        if scheme in ("http", "https"):
            return http_connect(self.upstream, host, port, user=self._user, password=self._password)
        return socks_connect(self.upstream, host, port, user=self._user, password=self._password)

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
            if not host_allowed(host, port):
                log.warning("Мост: отказ для %s:%s", host, port)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                client.close()
                return
            upstream = self._upstream(host, port)
            client.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _pipe(client, upstream)
        except Exception as exc:  # одно соединение не должно ронять мост
            log.debug("Мост: %s", validate.redact(str(exc)))
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
