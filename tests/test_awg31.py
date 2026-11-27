# -*- coding: utf-8 -*-
"""0.3.2: AmneziaWG 3.1 — диапазоны и новые параметры одинаково из .conf и из vpn://, передача в amneziawg-go.

Все ключи здесь синтетические (последовательности байт), настоящих конфигураций в тестах нет."""
import base64
import json
import time

import pytest

from owntube import awg, httpc
from test_030 import (K1, K2, K3, _api, amnezia_export, conf_from, helper_exe, q_compress, test_server,  # noqa: F401
                      vpn_link)

HP = base64.b64encode(bytes(range(3, 35))).decode()

CONF31 = f"""[Interface]
Address = 10.8.1.13/32
DNS = 172.29.172.254, 1.0.0.1
PrivateKey = {K1}
Jc = 6
Jmin = 10
Jmax = 50
S1 = 12
S2 = 12
S3 = 12
S4 = 12
H1 = 1
H2 = 2
H3 = 3
H4 = 4
HeaderProtectionKey = {HP}
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on

[Peer]
PublicKey = {K2}
PresharedKey = {K3}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.test:48633
PersistentKeepalive = 25-35
"""

EXPECTED_OBF = {"jc": "6", "jmin": "10", "jmax": "50", "s1": "12", "s2": "12", "s3": "12", "s4": "12",
                "h1": "1", "h2": "2", "h3": "3", "h4": "4", "rekey_after_time": "100-120", "rekey_timeout": "3-7",
                "reject_after_time": "150-180", "keepalive_timeout": "5-15", "max_handshake_attempts": "15-20",
                "header_protection_key": HP, "random_trailers": "true", "disable_cookies": "true"}


def test_conf_31_accepts_ranges_and_keeps_every_key():
    cfg = awg.parse_conf(CONF31)
    assert cfg["obf"] == EXPECTED_OBF
    p = cfg["peers"][0]
    assert p["keepalive"] == "25-35" and p["allowed_ips"] == ["0.0.0.0/0", "::/0"] and p["preshared_key"] == K3
    assert cfg["dns"] == ["172.29.172.254", "1.0.0.1"] and cfg["addresses"] == ["10.8.1.13/32"]
    s = awg.summary(cfg)
    assert s["kind"] == "AmneziaWG 3.1" and "HEADER_PROTECTION_KEY" in s["obf"]
    assert HP not in json.dumps(s) and K1 not in json.dumps(s)


def test_single_keepalive_stays_a_number_and_names_are_flexible():
    cfg = awg.parse_conf(CONF31.replace("25-35", "30").replace("RekeyTimeout", "rekey_timeout").replace("Jc =", "JC="))
    assert cfg["peers"][0]["keepalive"] == 30 and cfg["obf"]["rekey_timeout"] == "3-7" and cfg["obf"]["jc"] == "6"
    cfg = awg.parse_conf(CONF31.replace("RandomTrailers = on", "RandomTrailers = false"))
    assert cfg["obf"]["random_trailers"] == "false"


def _link_text():
    return vpn_link(amnezia_export(CONF31))


def _fields_link():
    last = {"client_priv_key": K1, "server_pub_key": K2, "psk_key": K3, "hostName": "vpn.example.test", "port": "48633",
            "client_ip": "10.8.1.13", "Jc": "6", "Jmin": "10", "Jmax": "50", "S1": "12", "S2": "12", "S3": "12",
            "S4": "12", "H1": "1", "H2": "2", "H3": "3", "H4": "4", "HeaderProtectionKey": HP,
            "RekeyAfterTime": "100-120", "RekeyTimeout": "3-7", "RejectAfterTime": "150-180", "KeepaliveTimeout": "5-15",
            "MaxHandshakeAttempts": "15-20", "RandomTrailers": True, "DisableCookies": "on",
            "persistent_keep_alive": "25-35"}
    return vpn_link({"containers": [{"container": "amnezia-awg", "awg": {"last_config": json.dumps(last)}}],
                     "defaultContainer": "amnezia-awg", "dns1": "172.29.172.254", "dns2": "1.0.0.1"})


def test_vpn_link_with_config_text_equals_conf():
    a, b = awg.parse_conf(CONF31), awg.parse_any(_link_text())
    for k in ("private_key", "addresses", "mtu", "obf", "peers"):
        assert a[k] == b[k], k
    assert b["dns"] == a["dns"]  # DNS из текста конфигурации (литералы), а не $PRIMARY_DNS


def test_vpn_link_with_fields_only_equals_conf():
    a, b = awg.parse_conf(CONF31), awg.parse_any(_fields_link())
    assert a["obf"] == b["obf"] and a["peers"] == b["peers"] and b["dns"] == ["172.29.172.254", "1.0.0.1"]


def test_link_format_variants_and_whitespace():
    raw = json.dumps(amnezia_export(CONF31)).encode()
    for blob in (q_compress(raw), raw):
        link = "vpn://" + base64.urlsafe_b64encode(blob).decode().rstrip("=")
        assert awg.parse_any("  " + link + "\n")["obf"] == EXPECTED_OBF
    link = _link_text()
    assert awg.parse_any(link[:30] + "\n" + link[30:])["obf"] == EXPECTED_OBF  # перенос строки при копировании


@pytest.mark.parametrize("bad, msg", [
    (CONF31.replace("RekeyTimeout = 3-7", "RekeyTimeout = 7-3"), "RekeyTimeout"),
    (CONF31.replace("RandomTrailers = on", "RandomTrailers = maybe"), "RandomTrailers"),
    (CONF31.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = 25-99999"), "PersistentKeepalive"),
    (CONF31.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = abc"), "PersistentKeepalive"),
    (CONF31.replace(HP, "AAAA"), "HeaderProtectionKey"),
    (CONF31.replace("Jc = 6", "Jc = 6\nJ1 = secretvalue123"), "J1"),
    (CONF31.replace("PersistentKeepalive", "FooBar = secretvalue123\nPersistentKeepalive"), "FooBar"),
])
def test_bad_or_unknown_parameters_are_errors_not_silently_dropped(bad, msg):
    with pytest.raises(awg.AwgConfigError) as e:
        awg.parse_any(bad)
    text = str(e.value)
    assert msg in text and "secretvalue123" not in text and K1 not in text and HP not in text


def test_import_link_never_stored_or_logged(tmp_path, caplog):
    api = _api(tmp_path)
    caplog.set_level("DEBUG")
    link = _link_text()
    res = api.vpn_import_text(link)
    assert res["ok"] and res["config"]["kind"] == "AmneziaWG 3.1"
    stored = api._store.secrets.get(awg.SECRET_NAME)
    assert "vpn://" not in stored and link[6:60] not in stored
    for blob in (caplog.text, (tmp_path / "settings.json").read_text() if (tmp_path / "settings.json").exists() else "",
                 json.dumps(res)):
        assert link[6:60] not in blob and K1 not in blob and HP not in blob and K3 not in blob
    bad = api.vpn_import_text(link.replace("vpn://", "vpn://AAAA"))
    assert not bad["ok"] and link[6:60] not in json.dumps(bad)


def _conf31_for(c: dict) -> str:
    """Конфигурация клиента тестового сервера (его параметры уже в духе 3.1) — как .conf."""
    text = conf_from(c)
    assert "HeaderProtectionKey" in text and "RekeyAfterTime" in text and "PersistentKeepalive = 25-35" in text
    return text


def test_handshake_with_31_params_from_conf_and_from_link(helper_exe, test_server):  # noqa: F811
    conf = _conf31_for(test_server["client"])
    link = vpn_link(amnezia_export(conf))
    for source in (conf, link):
        cfg = awg.parse_any(source)
        assert cfg["peers"][0]["keepalive"] == "25-35" and "header_protection_key" in cfg["obf"]
        p = awg.AwgProvider(lambda cfg=cfg: cfg, lambda: helper_exe)
        try:
            route = p.current()
            status, _, body = httpc.fetch("GET", "http://test.owntube/hello", route)
            assert status == 200 and body == b"hello through awg"
            assert p.status()["handshake_age_sec"] >= 0
        finally:
            p.stop()


def test_wrong_header_protection_key_means_no_handshake(helper_exe, test_server):  # noqa: F811
    conf = _conf31_for(test_server["client"])
    other = base64.b64encode(bytes(range(9, 41))).decode()
    cfg = awg.parse_conf(conf)
    cfg["obf"]["header_protection_key"] = other  # ключ реально участвует в рукопожатии
    p = awg.AwgProvider(lambda: cfg, lambda: helper_exe)
    try:
        route = p.current()
        with pytest.raises(Exception):
            httpc.fetch("GET", "http://test.owntube/hello", route, timeout=4, attempts=1)
        time.sleep(0.5)
        assert p.status()["handshake_age_sec"] in (None, -1) or p.status()["handshake_age_sec"] < 0
    finally:
        p.stop()


def test_helper_rejects_unknown_parameter_instead_of_dropping_it(helper_exe, test_server):  # noqa: F811
    cfg = awg.parse_conf(_conf31_for(test_server["client"]))
    cfg["obf"]["j1"] = "x"
    h = awg.Helper(helper_exe)
    with pytest.raises(Exception, match="не поддерживается"):
        h.start(cfg)
    h.stop()


def test_old_stored_config_with_int_keepalive_still_starts(helper_exe, test_server):  # noqa: F811
    cfg = awg.parse_conf(_conf31_for(test_server["client"]))
    cfg["peers"][0]["keepalive"] = 25  # конфигурация, сохранённая версией 0.3.0
    h = awg.Helper(helper_exe)
    h.start(cfg)
    h.stop()
