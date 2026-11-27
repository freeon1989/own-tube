# -*- coding: utf-8 -*-
"""0.3.0: встроенный VPN AmneziaWG — разбор ссылок и .conf, хранение, туннель через тестовый сервер."""
import base64
import json
import os
import shutil
import subprocess
import time
import zlib
from pathlib import Path

import pytest

from owntube import awg, httpc
from owntube.api import Api
from owntube.db import Store
from owntube.settings import SettingsStore
from owntube.secretstore import SecretStore, MemoryBackend

ROOT = Path(__file__).resolve().parents[1]
K1 = base64.b64encode(bytes(range(32))).decode()
K2 = base64.b64encode(bytes(range(1, 33))).decode()
K3 = base64.b64encode(bytes(range(2, 34))).decode()

CONF = f"""[Interface]
PrivateKey = {K1}
Address = 10.8.1.5/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1376
Jc = 4
Jmin = 40
Jmax = 70
S1 = 15
S2 = 18
S3 = 20
S4 = 8
H1 = 1000000-1000100
H2 = 2000000-2000100
H3 = 3000000-3000100
H4 = 4000000-4000100
I1 = <b 0xc70000000108><r 16><t>

[Peer]
PublicKey = {K2}
PresharedKey = {K3}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.test:51820
PersistentKeepalive = 25
"""


def q_compress(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + zlib.compress(data, 8)


def vpn_link(obj: dict, compress=True) -> str:
    raw = json.dumps(obj).encode()
    raw = q_compress(raw) if compress else raw
    return "vpn://" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def amnezia_export(conf_text: str) -> dict:
    tmpl = conf_text.replace("DNS = 1.1.1.1, 1.0.0.1", "DNS = $PRIMARY_DNS, $SECONDARY_DNS")
    last = {"config": tmpl, "hostName": "vpn.example.test", "port": 51820, "client_priv_key": K1, "server_pub_key": K2}
    return {"containers": [{"container": "amnezia-awg", "awg": {"last_config": json.dumps(last), "port": "51820",
                                                                  "transport_proto": "udp"}}],
            "defaultContainer": "amnezia-awg", "description": "Мой сервер", "dns1": "9.9.9.9", "dns2": "149.112.112.112"}


def test_parse_conf_all_awg_params():
    cfg = awg.parse_conf(CONF)
    assert cfg["obf"] == {"jc": "4", "jmin": "40", "jmax": "70", "s1": "15", "s2": "18", "s3": "20", "s4": "8",
                          "h1": "1000000-1000100", "h2": "2000000-2000100", "h3": "3000000-3000100",
                          "h4": "4000000-4000100", "i1": "<b 0xc70000000108><r 16><t>"}
    assert cfg["addresses"] == ["10.8.1.5/32"] and cfg["dns"] == ["1.1.1.1", "1.0.0.1"] and cfg["mtu"] == 1376
    p = cfg["peers"][0]
    assert p["endpoint"] == "vpn.example.test:51820" and p["allowed_ips"] == ["0.0.0.0/0", "::/0"] and p["keepalive"] == 25
    s = awg.summary(cfg)
    assert s["kind"] == "AmneziaWG 2.0" and s["server"] == "•••:51820"
    assert K1 not in json.dumps(s) and "example" not in json.dumps(s)


@pytest.mark.parametrize("compress", [True, False])
def test_parse_vpn_link_amnezia_export(compress):
    cfg = awg.parse_any(vpn_link(amnezia_export(CONF), compress))
    assert cfg["name"] == "Мой сервер"
    assert cfg["dns"] == ["9.9.9.9", "149.112.112.112"]  # $PRIMARY_DNS/$SECONDARY_DNS из ссылки
    assert cfg["obf"]["i1"].startswith("<b ") and cfg["private_key"] == K1


def test_parse_vpn_link_from_fields_awg1():
    last = {"client_priv_key": K1, "server_pub_key": K2, "psk_key": K3, "hostName": "203.0.113.9", "port": "443",
            "client_ip": "10.8.1.7", "Jc": "3", "Jmin": "10", "Jmax": "50", "S1": "1", "S2": "2",
            "H1": "11", "H2": "22", "H3": "33", "H4": "44"}
    link = vpn_link({"containers": [{"container": "amnezia-awg", "awg": {"last_config": json.dumps(last)}}],
                     "defaultContainer": "amnezia-awg", "dns1": "1.1.1.1"})
    cfg = awg.parse_any(link)
    assert cfg["peers"][0]["endpoint"] == "203.0.113.9:443" and cfg["addresses"] == ["10.8.1.7/32"]
    assert awg.summary(cfg)["kind"] == "AmneziaWG 1.0"


def test_premium_link_is_explained():
    with pytest.raises(awg.AwgConfigError, match="подписку"):
        awg.parse_any(vpn_link({"config_version": 1, "api_key": "x" * 20}))


@pytest.mark.parametrize("bad, msg", [
    (CONF.replace(K1, "AAAA"), "PrivateKey"),
    (CONF.replace("Jmin = 40", "Jmin = 90"), "Jmin больше Jmax"),
    (CONF.replace("H1 = 1000000-1000100", "H1 = 5-1"), "H1"),
    (CONF.replace("I1 = <b", "I1 = b"), "I1"),
    (CONF.replace("Endpoint = vpn.example.test:51820", "Endpoint = vpn example:1"), "Endpoint"),
    ("просто текст", "vpn://"),
])
def test_bad_configs_are_rejected_without_echoing_secrets(bad, msg):
    with pytest.raises(awg.AwgConfigError) as e:
        awg.parse_any(bad)
    assert msg in str(e.value) and K1 not in str(e.value)


def _api(tmp_path, backend=None):
    store = SettingsStore(tmp_path, SecretStore(tmp_path, backend or MemoryBackend()))
    return Api(store=store, db=Store(tmp_path / "db.sqlite"), start_downloader=False)


def test_import_keeps_config_out_of_settings_and_log(tmp_path, caplog):
    api = _api(tmp_path)
    caplog.set_level("DEBUG")
    res = api.vpn_import_text(vpn_link(amnezia_export(CONF)))
    assert res["ok"] and res["imported"] and res["config"]["kind"] == "AmneziaWG 2.0"
    api.save_settings({"theme": "neon"})
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert K1 not in text and K2 not in text and "example" not in text
    assert K1 not in caplog.text and "example.test" not in caplog.text
    assert K1 not in json.dumps(api.vpn_state())
    assert api.vpn_forget()["imported"] is False


def test_enable_without_config_is_refused(tmp_path):
    api = _api(tmp_path)
    r = api.vpn_enable(True)
    assert not r["ok"] and "импортируйте" in r["error"]


def test_hide_addresses():
    assert awg.hide_addresses("dial 203.0.113.9:51820 and 2001:db8::1 failed") == "dial <адрес>:51820 and <адрес> failed"


# ---------- настоящий туннель: помощник на Go + тестовый сервер в том же стеке ----------
@pytest.fixture(scope="session")
def helper_exe(tmp_path_factory):
    exe = os.environ.get("AWGPROXY")
    if exe:
        return exe
    if not shutil.which("go"):
        pytest.skip("нет Go для сборки awgproxy")
    out = tmp_path_factory.mktemp("awg") / "awgproxy"
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "CGO_ENABLED": "0"}
    subprocess.run(["go", "build", "-trimpath", "-o", str(out), "."], cwd=ROOT / "helper" / "awgproxy", env=env,
                   check=True, timeout=600)
    return str(out)


def conf_from(c: dict) -> str:
    lines = ["[Interface]", f"PrivateKey = {c['private_key']}", "Address = " + ", ".join(c["addresses"]),
             "DNS = " + ", ".join(c["dns"]), f"MTU = {c['mtu']}"]
    lines += [f"{k.upper()} = {v}" for k, v in sorted(c["obf"].items())]
    for p in c["peers"]:
        lines += ["[Peer]", f"PublicKey = {p['public_key']}", f"Endpoint = {p['endpoint']}",
                  "AllowedIPs = " + ", ".join(p["allowed_ips"]), f"PersistentKeepalive = {p['keepalive']}"]
    return "\n".join(lines) + "\n"


@pytest.fixture()
def test_server(helper_exe):
    proc = subprocess.Popen([helper_exe, "-test-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    info = json.loads(proc.stdout.readline())
    yield info
    proc.stdin.close()
    proc.wait(timeout=5)


def test_app_traffic_goes_through_embedded_tunnel(tmp_path, helper_exe, test_server, monkeypatch):
    api = _api(tmp_path)
    api._awg._find = lambda: helper_exe
    res = api.vpn_import_text(conf_from(test_server["client"]))
    assert res["ok"] and res["config"]["kind"] == "AmneziaWG 2.0"
    r = api.vpn_enable(True)
    assert r["ok"] and r["status"]["running"], r
    route = api._routes.current()
    assert route.proxy.startswith("socks5://127.0.0.1:") and route.password and route.label == "VPN AmneziaWG"
    # имя test.owntube знает только DNS-сервер на той стороне туннеля
    status, _, body = httpc.fetch("GET", "http://test.owntube/hello", route)
    assert status == 200 and body == b"hello through awg"
    # мост для mpv/ffmpeg тоже идёт через туннель (разрешаем тестовый хост на время проверки)
    import socket
    from owntube import bridge
    monkeypatch.setattr(bridge, "ALLOWED_SUFFIXES", bridge.ALLOWED_SUFFIXES + ("test.owntube",))
    monkeypatch.setattr(bridge, "ALLOWED_PORTS", (443, 80))
    bridge_url = route.mpv_proxy()
    port = int(bridge_url.rsplit(":", 1)[1])
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"CONNECT test.owntube:80 HTTP/1.1\r\nHost: test.owntube:80\r\n\r\n")
    head = s.recv(200)
    assert head.startswith(b"HTTP/1.1 200"), head
    s.sendall(b"GET /hello HTTP/1.1\r\nHost: test.owntube\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    assert data.endswith(b"hello through awg")
    st = api.vpn_state(True)
    assert st["status"]["running"] and st["status"]["handshake_age_sec"] >= 0 and st["status"]["rx"] > 0
    assert st["status"]["latency_ms"] is None or isinstance(st["status"]["latency_ms"], int)
    # неверный пароль SOCKS не проходит
    from owntube.bridge import SocksError, socks_connect
    with pytest.raises((SocksError, OSError)):
        socks_connect(route.proxy, "test.owntube", 80, timeout=5, user=route.user, password="wrong")
    # выключение останавливает помощника
    helper = api._awg._helper
    assert api.vpn_enable(False)["ok"]
    time.sleep(0.3)
    assert helper.proc is None and api._routes.current().proxy == ""


def test_helper_dies_with_stdin(helper_exe, test_server):
    cfg = awg.parse_conf(conf_from(test_server["client"]))
    h = awg.Helper(helper_exe)
    h.start(cfg)
    proc = h.proc
    proc.stdin.close()
    proc.wait(timeout=5)
    assert proc.returncode == 0


def test_bad_server_key_reports_without_secrets(helper_exe):
    cfg = awg.parse_conf(CONF.replace("vpn.example.test", "127.0.0.1"))
    p = awg.AwgProvider(lambda: cfg, lambda: helper_exe)
    r = p.check()  # туннель поднимется (UDP без ответа), но до YouTube не дойдёт
    assert r["ok"] is False and K1 not in r["detail"]
    p.stop()


def test_helper_selftest(helper_exe):
    out = subprocess.run([helper_exe, "-selftest"], capture_output=True, text=True, timeout=60)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["ok"] and data["dns_through_tunnel"] and data["bad_auth_rejected"]
