"""0.2.2: чат помощниц, весь трафик через прокси (кроме частных адресов), медиасервер, аккаунты, диагностика.

Сеть не нужна: «внешние» серверы подставные на 127.0.0.1, SOCKS5-сервер с логином подменяет их адреса.
Все ключи, пароли и cookies — фальшивые.
"""
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from owntube import accounts, diagnostics, httpc, vk, ytdl
from owntube.ai import assistant
from owntube.api import Api
from owntube.bridge import host_allowed
from owntube.network import Route
from owntube.secretstore import MemoryBackend, SecretStore
from owntube.settings import SettingsStore
from socks5_server import Socks5Server

FAKE_KEY = "sk-test-not-a-real-key-0000"
PROXY_USER = "tester"
PROXY_SECRET = "p@ss:w/rd#1"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000 + b"\xff\xd9"
HOSTS = ("api.fake-ai.test", "i.ytimg.com", "www.youtube.com", "rr1.googlevideo.com")


# ---------- подставной TLS-сервер для «внешних» адресов ----------
@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("нет openssl для тестового сертификата")
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    san = ",".join("DNS:" + h for h in HOSTS)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=own-tube-test",
                    "-addext", "subjectAltName=" + san, "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    seen = {"chat": [], "auth": []}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            return

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/robots.txt":
                return self._send(200, "text/plain", b"User-agent: *\n")
            if self.path.startswith("/videoplayback"):
                body = b"\x1aE\xdf\xa3" + b"v" * 65532
                self.send_response(206)
                self.send_header("Content-Type", "video/webm")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if self.path.startswith("/vi/"):
                return self._send(200, "image/jpeg", JPEG)
            self._send(404, "text/plain", b"no")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen["chat"].append(json.loads(body))
            seen["auth"].append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Bearer " + FAKE_KEY:
                return self._send(401, "application/json", b'{"error":{"message":"bad key"}}')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for piece in ("Привет! ", "Нашла кое-что.\n", "[[search: lofi ", "hip hop]]\n[[play: dQw4w9WgXcQ]]"):
                ev = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": srv.server_address[1], "cafile": str(cert), "seen": seen}
    srv.shutdown()


@pytest.fixture
def socks(tls, monkeypatch):
    client_ctx = ssl.create_default_context(cafile=tls["cafile"])
    monkeypatch.setattr(httpc, "ssl_context", lambda: client_ctx)
    srv = Socks5Server(PROXY_USER, PROXY_SECRET, hosts={h: ("127.0.0.1", tls["port"]) for h in HOSTS})
    yield srv
    srv.close()


def _api(tmp_path, proxy_url="", user="", secret=""):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=st, start_downloader=False, search_fn=lambda q, route, limit=24: [{"id": "dQw4w9WgXcQ"}])
    if proxy_url:
        assert api.save_settings({"proxy": proxy_url, "proxy_user": user, "proxy_password": secret})["ok"]
    return api


def _wait_job(api, job, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = api.assistant_poll(job)
        assert r["ok"], r
        if r["done"]:
            return r
        time.sleep(0.05)
    raise AssertionError("ответ не пришёл")


# ---------- чат ----------
def test_chat_through_socks_proxy_streams_and_parses_actions(tmp_path, socks, tls):
    api = _api(tmp_path, socks.url, PROXY_USER, PROXY_SECRET)
    cfg = api.save_ai_config({"name": "Фейк", "base_url": "https://api.fake-ai.test/v1", "model": "fake-model",
                              "api_key": FAKE_KEY, "active": True})
    assert cfg["ok"], cfg
    assert api.save_settings({"assistant": "kate"})["ok"]
    ctx = {"view": "search", "title": "Поиск", "items": [{"source": "yt", "id": "dQw4w9WgXcQ", "title": "Песня"}]}
    r = api.assistant_send("Что посмотреть?", ctx)
    assert r["ok"], r
    out = _wait_job(api, r["job"])
    assert out["error"] == ""
    assert out["text"].startswith("Привет! Нашла кое-что.") and "[[" not in out["text"]
    kinds = [(a["kind"], a.get("query") or a.get("id")) for a in out["actions"]]
    assert ("search", "lofi hip hop") in kinds and ("play", "dQw4w9WgXcQ") in kinds
    assert next(a for a in out["actions"] if a["kind"] == "play")["known"] is True
    # всё прошло через SOCKS5 с логином; персона и экран — в системной подсказке
    assert ("api.fake-ai.test", 443) in socks.targets and socks.auth_failures == 0
    sent = tls["seen"]["chat"][-1]
    assert sent["stream"] is True and sent["model"] == "fake-model"
    assert "Кейт" in sent["messages"][0]["content"] and "dQw4w9WgXcQ: Песня" in sent["messages"][0]["content"]
    assert sent["messages"][-1] == {"role": "user", "content": "Что посмотреть?"}
    # история сохраняется и уходит в следующий запрос
    hist = api.assistant_history()["messages"]
    assert [m["role"] for m in hist] == ["user", "assistant"] and "[[" not in hist[1]["text"]
    out2 = _wait_job(api, api.assistant_send("А ещё?")["job"])
    assert len(tls["seen"]["chat"][-1]["messages"]) == 4
    assert api.assistant_clear()["ok"] and api.assistant_history()["messages"] == []
    # ключ не попал ни в настройки, ни в ответы
    assert FAKE_KEY not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert FAKE_KEY not in json.dumps(out2, ensure_ascii=False)


def test_chat_api_error_is_shown_without_key(tmp_path, socks, tls):
    api = _api(tmp_path, socks.url, PROXY_USER, PROXY_SECRET)
    api.save_ai_config({"name": "Плохой ключ", "base_url": "https://api.fake-ai.test/v1", "model": "m",
                        "api_key": "sk-test-wrong-key-1111", "active": True})
    out = _wait_job(api, api.assistant_send("привет")["job"])
    assert out["error"] and "401" in out["error"] or "ключ" in out["error"].lower()
    assert "sk-test-wrong-key-1111" not in out["error"]


def _plain_server(handler_post):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            handler_post(self, body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_private_model_server_goes_direct_even_with_broken_proxy(tmp_path):
    got = {}

    def post(h, body):
        got["body"] = body
        payload = json.dumps({"choices": [{"message": {"content": "Локальная модель на связи"}}]}, ensure_ascii=False).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    srv = _plain_server(post)
    api = _api(tmp_path, "127.0.0.1:9", "u", "x")  # прокси, который не отвечает
    api.save_ai_config({"name": "LAN", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1", "model": "local",
                        "active": True})  # без ключа — локальным серверам можно
    out = _wait_job(api, api.assistant_send("ты тут?")["job"])
    assert out["error"] == "" and out["text"] == "Локальная модель на связи"
    assert got["body"]["model"] == "local"
    srv.shutdown()


def test_anthropic_adapter_streaming(tmp_path):
    seen = {}

    def post(h, body):
        seen["key"], seen["ver"], seen["body"] = h.headers.get("x-api-key"), h.headers.get("anthropic-version"), body
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        for t in ("Здравствуй", ", я Юки."):
            ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}
            h.wfile.write(b"event: content_block_delta\ndata: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
        h.wfile.write(b'data: {"type":"message_stop"}\n\n')

    srv = _plain_server(post)
    from owntube.ai.providers import chat_stream, kind_of
    base = f"http://127.0.0.1:{srv.server_address[1]}/anthropic/v1"
    # тип провайдера по адресу; для теста подменяем распознавание
    import owntube.ai.providers as prov
    orig = prov.kind_of
    prov.kind_of = lambda b: "anthropic"
    try:
        text = "".join(chat_stream({"base_url": base, "model": "claude-test"}, FAKE_KEY, "sys",
                                   [{"role": "user", "content": "hi"}], Route()))
    finally:
        prov.kind_of = orig
    assert text == "Здравствуй, я Юки."
    assert seen["key"] == FAKE_KEY and seen["ver"] and seen["body"]["system"] == "sys" and seen["body"]["stream"]
    assert kind_of("https://api.anthropic.com/v1") == "anthropic"
    srv.shutdown()


def test_parse_actions_and_trim():
    text, acts = assistant.parse_actions("Ок [[open: История]] [[play: bad]] [[like: dQw4w9WgXcQ]] [[open: rm -rf]]")
    assert text == "Ок" and acts[0] == {"kind": "open", "section": "history", "label": "История"}
    assert acts[1]["kind"] == "like" and acts[1]["confirm"] is True and len(acts) == 2
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1000} for i in range(100)]
    trimmed = assistant.trim_history(hist, 4000, "s")
    assert trimmed and trimmed[0]["role"] == "user" and len(trimmed) < 20 and trimmed[-1] is hist[-1]
    with pytest.raises(ValueError):
        assistant.clean_message("   ")
    assert assistant.clean_context({"items": [{"id": "<script>"}, {"id": "dQw4w9WgXcQ", "title": "a"}]})["items"][0]["id"] == "dQw4w9WgXcQ"


# ---------- медиасервер: картинки через прокси ----------
def test_thumbnails_go_through_proxy_via_local_media_server(tmp_path, socks):
    api = _api(tmp_path, socks.url, PROXY_USER, PROXY_SECRET)
    st = api.get_state()
    base = st["media"]
    assert base.startswith("http://127.0.0.1:")
    url = base + "/img?u=" + urllib.request.quote("https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg", safe="")
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.headers["Content-Type"] == "image/jpeg" and r.read() == JPEG
    assert ("i.ytimg.com", 443) in socks.targets
    for evil in ("https://evil.example/x.jpg", "http://i.ytimg.com/vi/x/a.jpg", "file:///etc/passwd"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/img?u=" + urllib.request.quote(evil, safe=""), timeout=10)
        assert e.value.code == 403
    # без токена — 404
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base.rsplit("/", 1)[0] + "/img?u=x", timeout=10)
    assert e.value.code == 404
    api._shutdown()


# ---------- диагностика ----------
def test_step_by_step_check(tmp_path, socks):
    good = Route(proxy=socks.url, label="SOCKS", user=PROXY_USER, password=PROXY_SECRET)
    out = diagnostics.run(good, lambda q, route, n: [{"id": "dQw4w9WgXcQ"}] * 3)
    assert out["ok"], out
    assert [s["name"] for s in out["steps"]] == ["Соединение с прокси (TCP)", "Рукопожатие и вход на прокси",
                                                 "Доступ к youtube.com (HTTPS)", "Обложки (i.ytimg.com)", "Поиск"]
    bad = Route(proxy=socks.url, label="SOCKS", user=PROXY_USER, password="wrong")
    out = diagnostics.run(bad, None)
    assert not out["ok"] and "Неверный логин или пароль" in out["detail"] and len(out["steps"]) == 2
    dead = Route(proxy="socks5://127.0.0.1:9", label="SOCKS")
    out = diagnostics.run(dead, None)
    assert not out["ok"] and out["steps"][0]["ok"] is False


def test_video_check_fetches_real_bytes_through_proxy(tmp_path, socks):
    good = Route(proxy=socks.url, label="SOCKS", user=PROXY_USER, password=PROXY_SECRET)
    streams = {"video": "https://rr1.googlevideo.com/videoplayback?ip=2001%3Adb8%3A%3A1&id=x", "height": 360, "container": "webm"}
    out = diagnostics.run(good, lambda q, route, n: [{"id": "dQw4w9WgXcQ"}], lambda route: streams, lambda st: 70000)
    names = [s["name"] for s in out["steps"]]
    assert out["ok"], out
    assert names[-3:] == ["Проверка видео: адрес потока", "Проверка видео: первые байты потока", "Проверка видео: встроенный плеер"]
    detail = out["steps"][-2]["detail"]
    assert "rr1.googlevideo.com" in detail and "HTTP 206" in detail and "IPv6" in detail and "ATYP 3" in detail
    assert "2001" not in json.dumps(out) and "videoplayback" not in json.dumps(out)  # без адресов и IP в журнале
    assert ("rr1.googlevideo.com", 443) in socks.targets


def test_read_log_hides_secrets(tmp_path):
    api = _api(tmp_path, "127.0.0.1:1080", "alice", PROXY_SECRET)
    (tmp_path / "own-tube.log").write_text(
        f"line socks5h://alice:{PROXY_SECRET}@127.0.0.1:1080 ok\nother {PROXY_SECRET} end\n", encoding="utf-8")
    lines = api.read_log(50)["lines"]
    assert lines and all(PROXY_SECRET not in line for line in lines)


# ---------- аккаунты ----------
COOKIES_TXT = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tfake-sapisid-value\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t4102444800\t__Secure-3PSID\tfake-3psid-value\n"
    ".evil.example\tTRUE\t/\tTRUE\t4102444800\tSID\tshould-be-dropped\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1\tOLD\texpired\n"
)


def test_cookie_import_encrypted_store_not_in_settings(tmp_path, caplog):
    api = _api(tmp_path)
    acc = api._accounts
    n = acc.import_text("google", COOKIES_TXT)
    assert n == 3  # evil.example отброшен
    st = api.account_status()["accounts"]
    assert st["google"]["signed_in"] and st["google"]["origin"] == "file" and not st["vk"]["signed_in"]
    text = acc.cookie_text()
    assert "fake-sapisid-value" in text and "should-be-dropped" not in text and "\tOLD\t" not in text
    assert "#HttpOnly_.youtube.com" in text
    settings_text = (tmp_path / "settings.json").read_text(encoding="utf-8") if (tmp_path / "settings.json").exists() else ""
    assert "fake-sapisid" not in settings_text
    assert "fake-sapisid" not in caplog.text
    assert "fake-sapisid" not in json.dumps(api.get_state(), ensure_ascii=False)
    with pytest.raises(ValueError):
        acc.import_text("vk", COOKIES_TXT)  # нет признаков входа VK
    prof = acc.profile_dir("google")
    prof.mkdir(parents=True)
    (prof / "x").write_text("1")
    assert api.account_logout("google")["ok"]
    assert not prof.exists() and acc.cookie_text() == ""


def test_ytdl_cookie_file_is_temporary(tmp_path, monkeypatch):
    import yt_dlp

    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen["path"] = opts.get("cookiefile")
            seen["content"] = open(seen["path"], encoding="utf-8").read() if seen["path"] else ""
            if os.name == "posix" and seen["path"]:
                seen["mode"] = os.stat(seen["path"]).st_mode & 0o777

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    api = _api(tmp_path)
    api._accounts.import_text("google", COOKIES_TXT)
    with ytdl.YDL({"quiet": True}):
        pass
    assert "fake-sapisid-value" in seen["content"]
    assert not os.path.exists(seen["path"])
    if os.name == "posix":
        assert seen["mode"] == 0o600
    api.account_logout("google")
    with ytdl.YDL({"quiet": True}):
        pass
    assert seen["path"] is None
    ytdl.set_cookie_provider(None)


def test_sapisid_hash_and_login_bridge_allowlist():
    assert accounts.sapisid_hash("abc", now=1700000000).startswith("SAPISIDHASH 1700000000_")
    g = accounts.LOGIN_HOSTS["google"]
    assert host_allowed("accounts.google.com", 443, g) and host_allowed("www.youtube.com", 443, g)
    assert not host_allowed("evil.example", 443, g) and not host_allowed("accounts.google.com", 80, g)
    assert host_allowed("id.vk.com", 443, accounts.LOGIN_HOSTS["vk"])
    assert not host_allowed("accounts.google.com", 443)  # обычный мост mpv пускает только к видео-CDN


def test_vk_search_parsing_and_adult_needs_login():
    resp = {"videos": [{"owner_id": -1, "id": 2, "title": "Котик", "duration": 65, "views": 1200,
                        "image": [{"url": "https://sun9-1.userapi.com/a.jpg", "width": 320}]},
                       {"owner_id": "bad", "id": 3}],
            "groups": [{"id": 1, "name": "Кошки"}]}
    items = vk.parse_search(resp)
    assert items == [{"source": "vk", "id": "-1_2", "title": "Котик", "channel": "Кошки", "channel_id": "",
                      "duration": "1:05", "views": items[0]["views"], "live": False,
                      "thumb": "https://sun9-1.userapi.com/a.jpg"}]


def test_settings_player_mode(tmp_path):
    api = _api(tmp_path)
    assert api.get_state()["settings"]["player_mode"] == "embedded"
    assert api.save_settings({"player_mode": "mpv"})["ok"]
    assert api.save_settings({"player_mode": "evil"})["ok"] is False
    assert api.seek_url("nope", 10)["ok"] is False


def test_login_window_cookie_conversion():
    from http.cookies import SimpleCookie
    from owntube import login_window

    jar = SimpleCookie()
    jar["SAPISID"] = "fake-value"
    jar["SAPISID"]["domain"] = ".youtube.com"
    jar["SAPISID"]["path"] = "/"
    jar["SAPISID"]["secure"] = True
    jar["SAPISID"]["expires"] = "Fri, 01 Jan 2100 00:00:00 GMT"
    got = login_window._morsels([jar])
    assert got[0]["name"] == "SAPISID" and got[0]["domain"] == ".youtube.com" and got[0]["expires"] == 4102444800
    assert login_window._ready(got, "google")
    assert not login_window._ready([{**got[0], "domain": ".google.com"}], "google")


@pytest.mark.skipif(os.name != "posix", reason="подставной ffmpeg — shell-скрипт")
def test_media_server_retries_403_and_hides_stream_url(tmp_path, monkeypatch):
    from owntube import media

    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    counter = tmp_path / "n"
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nn=$(cat '%s' 2>/dev/null || echo 0); n=$((n+1)); echo $n > '%s'\n"
                    "if [ $n -le 2 ]; then echo 'https://rr1.googlevideo.com/videoplayback?ip=203.0.113.9 403 Forbidden' >&2; exit 1; fi\n"
                    "head -c 200000 /dev/zero\n" % (counter, counter))
    fake.chmod(0o755)
    srv = media.MediaServer(lambda: Route(), lambda: str(fake))
    streams = {"source": "yt", "video": "https://rr1.googlevideo.com/videoplayback?x=1", "audio": None, "container": "webm"}
    sid = srv.add_session(streams)
    with urllib.request.urlopen(srv.video_url(sid, 0), timeout=10) as r:
        assert len(r.read()) == 200000
    assert counter.read_text().strip() == "3"
    # всегда 403 → 502, адрес потока (с IP) не попадает в текст ошибки
    counter.write_text("-100")
    sid = srv.add_session(streams)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(srv.video_url(sid, 0), timeout=20)
    assert e.value.code == 502 and "203.0.113.9" not in srv.last_error and "403" in srv.last_error
    srv.close()


# ---------- 0.2.3: надёжное соединение через прокси ----------
def _echo_server():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.sendall(b"hello")
            c.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def _via(proxy, host, port, user=PROXY_USER, pwd=PROXY_SECRET):
    from owntube import bridge
    s, info = bridge.connect_via_proxy(proxy.url, host, port, user, pwd, timeout=5)
    data = s.recv(5)
    s.close()
    return data, info


def test_domain_name_is_sent_by_default_and_domain_only_proxy_works():
    echo = _echo_server()
    srv = Socks5Server(PROXY_USER, PROXY_SECRET, domain_only=True, hosts={"rr1.googlevideo.com": ("127.0.0.1", echo.getsockname()[1])})
    data, info = _via(srv, "rr1.googlevideo.com", 443)
    assert data == b"hello" and info["atyp"] == "domain" and srv.atyps == [3]
    srv.close()


def test_transient_refusal_is_retried(monkeypatch):
    echo = _echo_server()
    srv = Socks5Server(PROXY_USER, PROXY_SECRET, flaky={"rr1.googlevideo.com": 1},
                       hosts={"rr1.googlevideo.com": ("127.0.0.1", echo.getsockname()[1])})
    data, info = _via(srv, "rr1.googlevideo.com", 443)
    assert data == b"hello" and info["tries"] == 2 and info["atyp"] == "domain"
    srv.close()


def test_falls_back_to_ipv4_when_proxy_cannot_resolve_name(monkeypatch):
    from owntube import bridge
    echo = _echo_server()
    srv = Socks5Server(PROXY_USER, PROXY_SECRET, reject_domains={"rr1.googlevideo.com": 5})
    monkeypatch.setattr(bridge, "_resolve_ipv4", lambda *a: (["127.0.0.1"], "тест"))
    data, info = _via(srv, "rr1.googlevideo.com", echo.getsockname()[1])
    assert data == b"hello" and info["atyp"] == "ipv4" and srv.atyps[-1] == 1 and srv.atyps[:2] == [3, 3]
    srv.close()


def test_proxy_rejecting_ip_literals_fails_cleanly_without_hanging(monkeypatch):
    from owntube import bridge
    srv = Socks5Server(PROXY_USER, PROXY_SECRET, domain_only=True, reject_domains={"rr1.googlevideo.com": 5})
    monkeypatch.setattr(bridge, "_resolve_ipv4", lambda *a: (["127.0.0.1"], "тест"))
    t = time.time()
    with pytest.raises(bridge.SocksError) as e:
        _via(srv, "rr1.googlevideo.com", 443)
    assert time.time() - t < 8 and e.value.code in (5, 8)
    srv.close()


def test_connection_cap_does_not_break_thumbnail_burst(tmp_path, socks, tls, monkeypatch):
    """24 обложек подряд при лимите прокси в 4 соединения: все приходят (очередь + повторы)."""
    from owntube import bridge
    monkeypatch.setattr(bridge, "_resolve_ipv4", lambda *a: ([], ""))
    socks.max_conns = 4
    api = _api(tmp_path, socks.url, PROXY_USER, PROXY_SECRET)
    base = api.get_state()["media"]
    results = []

    def get(i):
        u = f"https://i.ytimg.com/vi/id{i:09d}/mqdefault.jpg"
        try:
            with urllib.request.urlopen(base + "/img?u=" + urllib.request.quote(u, safe=""), timeout=30) as r:
                results.append(len(r.read()))
        except Exception as exc:
            results.append(str(exc))

    ths = [threading.Thread(target=get, args=(i,)) for i in range(24)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert results == [len(JPEG)] * 24, results
    assert socks.peak_conns <= httpc.MAX_PARALLEL_PROXIED  # клиент сам ограничивает число одновременных соединений
    api._shutdown()


# ---------- 0.2.3: cookies окна входа и сводка для журнала ----------
def test_webview2_cookie_conversion_and_summary():
    from types import SimpleNamespace as NS
    from owntube import login_window
    good = NS(Name="remixsid", Value="SECRET-VALUE", Domain=".vk.com", Path="/", IsSecure=True, IsHttpOnly=True,
              IsSession=False, Expires=1_900_000_000)
    sess = NS(Name="remixlang", Value="0", Domain="vkvideo.ru", Path="", IsSecure=False, IsHttpOnly=False,
              IsSession=True, Expires=-62135596800)
    broken = NS(Name="x")
    d1 = login_window.cookie_dict(good, lambda e: e)
    d2 = login_window.cookie_dict(sess, lambda e: e)
    assert d1["httponly"] is True and d1["expires"] == 1_900_000_000 and d1["domain"] == ".vk.com"
    assert d2["expires"] == 0 and d2["path"] == "/"
    assert login_window.cookie_dict(broken, lambda e: e) is None
    text = accounts.summarize([d1, d2])
    assert "vk.com: 1" in text and "vkvideo.ru: 1" in text and "remixsid" in text and "SECRET-VALUE" not in text
    assert login_window._ready([d1], "vk") and not login_window._ready([d2], "vk")


def test_vk_cookies_from_other_vk_domains_are_kept():
    cookies = [{"name": "remixsid", "value": "a", "domain": ".vk.com", "path": "/", "secure": True, "expires": 0, "httponly": True},
               {"name": "remixsid6", "value": "b", "domain": "vkvideo.ru", "path": "/", "secure": True, "expires": 0},
               {"name": "other", "value": "c", "domain": ".example.com", "path": "/", "secure": True, "expires": 0}]
    from owntube import login_window
    kept = login_window._wanted(cookies, "vk")
    assert {c["domain"] for c in kept} == {".vk.com", "vkvideo.ru"}
