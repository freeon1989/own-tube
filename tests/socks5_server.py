"""Маленький SOCKS5-сервер с логином (RFC 1929) для тестов. Только 127.0.0.1."""
from __future__ import annotations

import select
import socket
import struct
import threading


class Socks5Server:
    def __init__(self, user: str, secret: str, allow_remote: bool = True, hosts: dict | None = None):
        self.user, self.secret, self.allow_remote = user, secret, allow_remote
        self.hosts = hosts or {}  # имя -> (адрес, порт): подставные «внешние» серверы на 127.0.0.1
        self.targets: list[tuple[str, int]] = []
        self.auth_failures = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass

    def _loop(self):
        while True:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    @staticmethod
    def _exact(c, n):
        b = b""
        while len(b) < n:
            x = c.recv(n - len(b))
            if not x:
                raise OSError("closed")
            b += x
        return b

    def _handle(self, c):
        try:
            ver, n = self._exact(c, 2)
            methods = self._exact(c, n)
            if 2 not in methods:
                c.sendall(b"\x05\xff")
                return c.close()
            c.sendall(b"\x05\x02")
            self._exact(c, 1)
            u = self._exact(c, self._exact(c, 1)[0]).decode()
            p = self._exact(c, self._exact(c, 1)[0]).decode()
            if (u, p) != (self.user, self.secret):
                self.auth_failures += 1
                c.sendall(b"\x01\x01")
                return c.close()
            c.sendall(b"\x01\x00")
            _, cmd, _, atyp = self._exact(c, 4)
            if atyp == 3:
                host = self._exact(c, self._exact(c, 1)[0]).decode()
            elif atyp == 1:
                host = socket.inet_ntoa(self._exact(c, 4))
            else:
                host = socket.inet_ntop(socket.AF_INET6, self._exact(c, 16))
            port = struct.unpack(">H", self._exact(c, 2))[0]
            self.targets.append((host, port))
            if cmd != 1 or not self.allow_remote:
                c.sendall(b"\x05\x02\x00\x01" + b"\x00" * 6)
                return c.close()
            try:
                up = socket.create_connection(self.hosts.get(host, (host, port)), timeout=15)
            except OSError:
                c.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
                return c.close()
            c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            up.settimeout(None)
            c.settimeout(None)
            socks = [c, up]
            while True:
                r, _, _ = select.select(socks, [], [], 120)
                if not r:
                    break
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        raise OSError("eof")
                    (up if s is c else c).sendall(d)
        except OSError:
            pass
        finally:
            for s in (c,):
                try:
                    s.close()
                except OSError:
                    pass
