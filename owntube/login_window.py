# -*- coding: utf-8 -*-
"""Окно входа в Google или VK (запускается отдельным процессом: own-tube --login google|vk).

Страница входа — настоящая страница Google/VK во встроенном WebView2 со своим постоянным профилем
в папке программы. Пароль вводится только на их странице; программа его не видит и не хранит.
Когда появляются cookies входа, нужные cookies шифруются (DPAPI) и окно закрывается.
Весь трафик окна идёт через тот же прокси, что и YouTube (через локальный мост со списком хостов).
"""
from __future__ import annotations

import logging
import os
import time

from . import accounts
from .network import ManualProxy
from .settings import SettingsStore, data_dir

log = logging.getLogger("owntube.login")

TITLE = {"google": "Вход в Google — own-tube", "vk": "Вход в VK — own-tube"}
COLLECT_FROM = {
    "google": ("https://www.youtube.com/", "https://accounts.google.com/"),
    "vk": ("https://vkvideo.ru/", "https://vk.com/"),
}
TIMEOUT = 15 * 60


def _morsels(raw) -> list[dict]:
    """pywebview отдаёт список http.cookies.SimpleCookie — превращаем в словари."""
    out = []
    for jar in raw or []:
        items = jar.values() if hasattr(jar, "values") else []
        for m in items:
            try:
                out.append({"name": m.key, "value": m.value, "domain": m.get("domain") or "",
                            "path": m.get("path") or "/", "secure": bool(m.get("secure")),
                            "httponly": bool(m.get("httponly")), "expires": _expires(m.get("expires"))})
            except Exception:
                continue
    return out


def _expires(value) -> int:
    if not value:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(str(value)).timestamp())
    except Exception:
        return 0


def _ready(cookies: list[dict], kind: str) -> bool:
    """Google: нужны cookies входа именно на youtube.com (их берёт yt-dlp). VK: remixsid/remixnsid."""
    if kind == "google":
        yt = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith("youtube.com")]
        return accounts.signed_in(yt, "google")
    return accounts.signed_in(cookies, kind)


def run(kind: str) -> int:
    if kind not in accounts.KINDS:
        return 2
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True)
    store = SettingsStore(home)
    secrets = store.secrets
    acc = accounts.Accounts(secrets, home)
    proxy = ManualProxy(lambda: store.value.proxy, lambda: store.value.proxy_user,
                        store.proxy_password).current()
    bridge = None
    if proxy.proxy:
        from .bridge import Bridge
        bridge = Bridge(proxy.proxy, proxy.user, proxy.password, allowed=accounts.LOGIN_HOSTS[kind])
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            f"--proxy-server=http://127.0.0.1:{bridge.port} --proxy-bypass-list=<-loopback>")
    profile = acc.profile_dir(kind)
    profile.mkdir(parents=True, exist_ok=True)

    import webview

    window = webview.create_window(TITLE[kind], accounts.LOGIN_URL[kind], width=560, height=760,
                                   background_color="#ffffff")

    def watch():
        started = time.time()
        while time.time() - started < TIMEOUT:
            time.sleep(1.5)
            try:
                cookies = _morsels(window.get_cookies())
            except Exception:
                continue
            clean = [c for c in cookies if accounts._domain_ok(c.get("domain", ""), kind)]
            if not _ready(clean, kind):
                continue
            # вход выполнен — собираем cookies с нужных сайтов
            collected = {(c["domain"], c["name"]): c for c in clean}
            for url in COLLECT_FROM[kind]:
                try:
                    window.load_url(url)
                    time.sleep(4)
                    for c in _morsels(window.get_cookies()):
                        if accounts._domain_ok(c.get("domain", ""), kind):
                            collected[(c["domain"], c["name"])] = c
                except Exception:
                    continue
            try:
                acc.save_cookies(kind, list(collected.values()), origin="window")
                log.info("Вход выполнен: %s", kind)
            except ValueError as exc:
                log.warning("Вход не сохранён: %s", exc)
                continue
            break
        try:
            window.destroy()
        except Exception:
            pass

    webview.start(watch, private_mode=False, storage_path=str(profile))
    if bridge:
        bridge.close()
    return 0
