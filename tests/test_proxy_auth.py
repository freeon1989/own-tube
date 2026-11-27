import json
import logging
import socket
import threading
from urllib.parse import quote

import pytest

from owntube import bridge, network, validate
from owntube.api import Api
from owntube.secretstore import FileBackend, MemoryBackend, SecretStore
from owntube.settings import SettingsStore

GOOD = "right-pass"
BAD = "wrong-pass"


def _serve(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_host_port_saved_as_socks5(tmp_path):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=st, start_downloader=False)
    r = api.save_settings({"proxy": "127.0.0.1:1080"})
    assert r["ok"] and r["settings"]["proxy"] == "socks5://127.0.0.1:1080"
    assert r["settings"]["proxy_type"] == "socks5"
    r = api.save_settings({"proxy": "127.0.0.1:8080", "proxy_type": "http"})
    assert r["settings"]["proxy"] == "http://127.0.0.1:8080"


def test_password_not_in_settings_logs_or_repr(tmp_path, caplog):
    secret = "s3cret-PASS-value"
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    caplog.set_level(logging.DEBUG)
    api = Api(store=st, start_downloader=False)
    r = api.save_settings({"proxy": "127.0.0.1:1080", "proxy_user": "alice", "proxy_password": secret})
    assert r["ok"] and r["settings"]["proxy_password_set"] is True
    assert "proxy_password" not in r["settings"]
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert secret not in text and "alice" in text
    assert "proxy_password" not in text
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert secret.encode() not in f.read_bytes()
    route = network.ManualProxy(lambda: st.value.proxy, lambda: st.value.proxy_user, st.proxy_password).current()
    assert secret not in repr(route) and secret not in str(route)
    opts = route.ytdlp_options()
    assert quote(secret, safe="") in opts["proxy"]
    assert secret not in validate.redact(opts["proxy"])
    filt = validate.RedactFilter()
    caplog.handler.addFilter(filt)
    logging.getLogger("owntube.test").info("route=%s proxy=%s", route, opts["proxy"])
    assert secret not in caplog.text
    assert "***:***@" in caplog.text


def test_encrypted_file_backend_hides_plaintext(tmp_path):
    secret = "s3cret-PASS-value"

    def protect(data: bytes) -> bytes:
        return b"ENC" + bytes(b ^ 0xA5 for b in data)

    def unprotect(data: bytes) -> bytes:
        assert data.startswith(b"ENC")
        return bytes(b ^ 0xA5 for b in data[3:])

    note = "test"
    backend = FileBackend(tmp_path / "secrets.dpapi", protect, unprotect, note)
    store = SecretStore(tmp_path, backend)
    assert store.set(secret) is True
    raw = (tmp_path / "secrets.dpapi").read_bytes()
    assert secret.encode() not in raw
    assert store.get() == secret
    store.delete()
    assert not (tmp_path / "secrets.dpapi").exists()


def test_socks5_rfc1929_handshake_and_bad_password():
    echo, echo_port = _serve(lambda c: (c.sendall(b"pong:" + c.recv(100)), c.close()))
    seen = {}

    def socks5(c):
        greet = c.recv(16)
        assert greet[0] == 5
        methods = set(greet[2:2 + greet[1]])
        if 0x02 in methods:
            c.sendall(b"\x05\x02")
            auth = c.recv(512)
            assert auth[0] == 1
            ulen = auth[1]
            user = auth[2:2 + ulen].decode()
            plen = auth[2 + ulen]
            pwd = auth[3 + ulen:3 + ulen + plen].decode()
            seen["auth"] = (user, pwd)
            if user == "alice" and pwd == "right-pass":
                c.sendall(b"\x01\x00")
            else:
                c.sendall(b"\x01\x01")
                c.close()
                return
        else:
            c.sendall(b"\x05\xFF")
            c.close()
            return
        head = c.recv(5)
        name = c.recv(head[4])
        port = int.from_bytes(c.recv(2), "big")
        seen["target"] = (name.decode(), port)
        up = socket.create_connection(("127.0.0.1", echo_port))
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        bridge._pipe(c, up)

    srv, socks_port = _serve(socks5)
    proxy = f"socks5h://127.0.0.1:{socks_port}"
    try:
        s = bridge.socks_connect(proxy, "rr1---sn-x.googlevideo.com", 443, user="alice", password=GOOD)
        s.sendall(b"ping")
        assert s.recv(100) == b"pong:ping"
        s.close()
        assert seen["auth"] == ("alice", GOOD)
        with pytest.raises(bridge.SocksError, match="логин или пароль"):
            bridge.socks_connect(proxy, "rr1---sn-x.googlevideo.com", 443, user="alice", password=BAD)
        with pytest.raises(bridge.SocksError, match="логин и пароль"):
            bridge.socks_connect(proxy, "rr1---sn-x.googlevideo.com", 443)
    finally:
        srv.close()
        echo.close()


def test_bridge_with_auth_end_to_end(monkeypatch):
    echo, echo_port = _serve(lambda c: (c.sendall(b"pong:" + c.recv(100)), c.close()))

    def socks5(c):
        greet = c.recv(16)
        c.sendall(b"\x05\x02")
        auth = c.recv(512)
        ulen = auth[1]
        user = auth[2:2 + ulen].decode()
        plen = auth[2 + ulen]
        pwd = auth[3 + ulen:3 + ulen + plen].decode()
        if (user, pwd) != ("alice", GOOD):
            c.sendall(b"\x01\x01")
            c.close()
            return
        c.sendall(b"\x01\x00")
        head = c.recv(5)
        c.recv(head[4] + 2)
        up = socket.create_connection(("127.0.0.1", echo_port))
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        bridge._pipe(c, up)

    srv, socks_port = _serve(socks5)
    monkeypatch.setattr(bridge, "ALLOWED_PORTS", (443,))
    b = bridge.Bridge(f"socks5h://127.0.0.1:{socks_port}", "alice", "right-pass")
    try:
        s = socket.create_connection(("127.0.0.1", b.port))
        s.sendall(b"CONNECT rr1---sn-x.googlevideo.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert s.recv(100).startswith(b"HTTP/1.1 200")
        s.sendall(b"ping")
        assert s.recv(100) == b"pong:ping"
        s.close()
        assert "right-pass" not in repr(b)
    finally:
        b.close()
        srv.close()
        echo.close()


def test_check_route_reports_unused_only_when_empty(tmp_path):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=st, start_downloader=False)
    r = api.check_route()
    assert r["ok"] and "напрямую" in r["detail"].lower()
    api.save_settings({"proxy": "127.0.0.1:9"})
    r = api.check_route()
    assert r["ok"] is False
    assert "не используется" not in r["detail"].lower()
    assert "не отвечает" in r["detail"].lower() or "отказа" in r["detail"].lower() or "прокси" in r["detail"].lower()
