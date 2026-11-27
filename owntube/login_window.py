# -*- coding: utf-8 -*-
"""Окно входа в Google или VK (запускается отдельным процессом: own-tube --login google|vk).

Страница входа — настоящая страница Google/VK во встроенном WebView2 со своим постоянным профилем
в папке программы. Пароль вводится только на их странице; программа его не видит и не хранит.
Когда появляются cookies входа, нужные cookies шифруются (DPAPI) и окно закрывается.
Весь трафик окна идёт через тот же прокси, что и YouTube (через локальный мост со списком хостов).

Cookies читаются прямо из хранилища WebView2 (CookieManager) по всему профилю, включая HttpOnly,
а не только для текущей страницы, как делает window.get_cookies(): иначе cookies соседних доменов
(vk.com → vkvideo.ru, accounts.google.com → youtube.com) не попадали в программу.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import accounts
from .network import ManualProxy
from .settings import SettingsStore, data_dir

log = logging.getLogger("owntube.login")

TITLE = {"google": "Вход в Google — own-tube", "vk": "Вход в VK — own-tube"}
COLLECT_FROM = {
    "google": ("https://www.youtube.com/", "https://accounts.google.com/"),
    "vk": ("https://vkvideo.ru/", "https://vk.com/", "https://id.vk.com/"),
}
# Адреса, для которых дополнительно просим cookies у WebView2 (кроме «всех»)
PROBE_URLS = {
    "google": ("https://www.youtube.com/", "https://accounts.google.com/", "https://www.google.com/"),
    "vk": ("https://vk.com/", "https://vkvideo.ru/", "https://id.vk.com/", "https://login.vk.com/", "https://vk.ru/"),
}
TIMEOUT = 15 * 60
SETTLE_SECONDS = 10


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


def cookie_dict(c, to_epoch) -> dict | None:
    """Один CoreWebView2Cookie -> словарь. to_epoch(c.Expires) -> секунды Unix; для сеансовых cookies — 0."""
    try:
        session = bool(getattr(c, "IsSession", False))
        exp = 0 if session else int(to_epoch(c.Expires))
        if exp < 0:
            exp = 0
        return {"name": str(c.Name), "value": str(c.Value), "domain": str(c.Domain), "path": str(c.Path or "/"),
                "secure": bool(c.IsSecure), "httponly": bool(c.IsHttpOnly), "expires": exp}
    except Exception:
        return None


def webview2_cookies(window, urls) -> list[dict]:
    """Все cookies профиля WebView2 (и для перечисленных адресов) через CookieManager. Вызывать не из UI-потока."""
    from webview.platforms import winforms as wf  # только Windows
    from webview.platforms import edgechromium as _edge  # подгружает сборки WebView2 (Microsoft.Web.WebView2.Core)
    from System import Action, DateTime, DateTimeKind, Func, Type
    from System.Collections.Generic import List
    from System.Threading.Tasks import Task
    from Microsoft.Web.WebView2.Core import CoreWebView2Cookie

    assert _edge is not None
    inst = wf.BrowserView.instances.get(window.uid)
    if inst is None:
        raise RuntimeError("окно входа не найдено")
    browser = inst.browser
    epoch = DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)

    def to_epoch(dt):
        return dt.ToUniversalTime().Subtract(epoch).TotalSeconds

    targets = [None] + list(urls)
    got: list[dict] = []
    done = threading.Semaphore(0)
    errors: list[str] = []

    def start():
        manager = browser.webview.CoreWebView2.CookieManager

        def make_cb(label):
            def cb(task):
                try:
                    if task.IsFaulted:
                        errors.append(f"{label}: сбой чтения")
                    else:
                        for c in task.Result:
                            d = cookie_dict(c, to_epoch)
                            if d:
                                got.append(d)
                except Exception as exc:
                    errors.append(f"{label}: {type(exc).__name__}")
                finally:
                    done.release()
            return cb

        for u in targets:
            manager.GetCookiesAsync(u).ContinueWith(
                Action[Task[List[CoreWebView2Cookie]]](make_cb(u or "все")), browser.syncContextTaskScheduler)

    inst.Invoke(Func[Type](start))
    for _ in targets:
        if not done.acquire(timeout=10):
            errors.append("таймаут чтения cookies")
            break
    if errors:
        log.info("Чтение cookies WebView2: %s", "; ".join(errors)[:300])
    uniq = {(c["domain"], c["path"], c["name"]): c for c in got}
    return list(uniq.values())


def read_cookies(window, kind: str) -> list[dict]:
    """Cookies из WebView2 напрямую; если не получилось — как раньше, через pywebview (только текущая страница)."""
    try:
        return webview2_cookies(window, PROBE_URLS[kind])
    except Exception as exc:
        log.info("Прямое чтение cookies недоступно (%s: %s), использую запасной способ", type(exc).__name__, str(exc)[:80])
    try:
        return _morsels(window.get_cookies())
    except Exception as exc:
        log.info("Запасное чтение cookies не удалось: %s", type(exc).__name__)
        return []


def _ready(cookies: list[dict], kind: str) -> bool:
    """Google: нужны cookies входа именно на youtube.com (их берёт yt-dlp). VK: remixsid и подобные."""
    if kind == "google":
        yt = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith("youtube.com")]
        return accounts.signed_in(yt, "google")
    return accounts.signed_in(cookies, kind)


def _wanted(cookies: list[dict], kind: str) -> list[dict]:
    return [c for c in cookies if accounts._domain_ok(c.get("domain", ""), kind)]


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
    log.info("Окно входа (%s) открыто, прокси: %s", kind, "да" if proxy.proxy else "нет")

    import webview

    window = webview.create_window(TITLE[kind], accounts.LOGIN_URL[kind], width=560, height=760,
                                   background_color="#ffffff")

    def watch():
        started = time.time()
        last_report = 0.0
        while time.time() - started < TIMEOUT:
            time.sleep(1.5)
            clean = _wanted(read_cookies(window, kind), kind)
            if time.time() - last_report > 20:
                last_report = time.time()
                log.info("Ожидание входа: %s", accounts.summarize(clean))
            if not _ready(clean, kind):
                continue
            log.info("Признаки входа найдены, собираю cookies: %s", accounts.summarize(clean))
            collected = {(c["domain"], c["path"], c["name"]): c for c in clean}
            # сайты с общим входом получают свои cookies после захода на них — открываем каждый и ждём
            for url in COLLECT_FROM[kind]:
                host = url.split("/")[2]
                try:
                    window.load_url(url)
                except Exception:
                    continue
                end = time.time() + SETTLE_SECONDS
                while time.time() < end:
                    time.sleep(1.0)
                    now = _wanted(read_cookies(window, kind), kind)
                    for c in now:
                        collected[(c["domain"], c["path"], c["name"])] = c
                    base = ".".join(host.split(".")[-2:])
                    if any((c.get("domain") or "").lstrip(".").endswith(base) and c["name"] in
                           {n for v in accounts.SIGNED_IN.values() for n in v} | {"SID", "SAPISID"} for c in now):
                        break
            final = list(collected.values())
            log.info("Сохраняю cookies: %s", accounts.summarize(final))
            try:
                acc.save_cookies(kind, final, origin="window")
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
