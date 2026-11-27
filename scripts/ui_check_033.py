# -*- coding: utf-8 -*-
"""Проверка 0.3.3 в Chromium без окна: «Назад» (кнопка, Alt+←, Backspace, боковая кнопка мыши), возврат к тому же списку и месту
без запросов, подсказка «Войдите заново», вкладки VK. Данные — подставные; сеть не нужна.

Запуск: python scripts/ui_check_033.py [папка для скриншотов]
"""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from playwright.sync_api import sync_playwright  # noqa: E402

from owntube.api import Api  # noqa: E402
from owntube.secretstore import MemoryBackend, SecretStore  # noqa: E402
from owntube.settings import SettingsStore  # noqa: E402
from shoot import SHIM, Quiet  # noqa: E402
from shoot_022 import wait_js  # noqa: E402


def vid(n, pre="a"):
    return (pre + f"{n:010d}")[:11]


def items(pre, a, b, **kw):
    return [{"source": "yt", "id": vid(i, pre), "title": f"{pre.upper()}-видео {i}", "channel": "Канал", "channel_id": "", "duration": "1:00",
             "views": "", "live": False, "thumb": "", **kw} for i in range(a, b)]


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="owntube-033-"))
    store = SettingsStore(home, secrets=SecretStore(home, MemoryBackend()))
    store.update({"download_dir": str(home / "dl")})
    api = Api(store=store, start_downloader=False)
    calls: list[tuple] = []
    state = {"home_error": None}

    def feed(kind, arg=None, more=None, mode="fresh"):
        calls.append(("feed", kind, arg, more, mode))
        if mode == "cache":
            return {"ok": True, "items": [], "more": None}
        if kind == "home":
            r = {"ok": True, "from": "account" if not state["home_error"] else "popular", "more": {"n": 24},
                 "items": items("a", 0, 24) if not more else items("a", 24, 48)}
            if state["home_error"]:
                r["account_error"] = state["home_error"]; r["relogin"] = "google"
            if more:
                r["more"] = None
            return r
        if kind == "shorts":
            return {"ok": True, "items": items("s", 0, 6, short=True), "more": None}
        return {"ok": True, "items": items("z", 0, 5), "more": None}

    def search(q, source="yt", more=None, mode="fresh"):
        calls.append(("search", q, source, more, mode))
        if mode == "cache":
            return {"ok": True, "items": [], "more": None}
        return {"ok": True, "items": items("q", 0, 40) if not more else items("q", 40, 60), "more": None if more else {"p": 2}}

    def watch(item, quality=None, short=False, fast=False, *a):
        calls.append(("watch", item.get("id")))
        return {"ok": False, "error": "Тест: поток не нужен"}

    api.feed, api.search, api.watch = feed, search, watch
    api.prefetch = lambda *a, **k: {"ok": True, "started": False}
    names = [n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    errors: list[str] = []
    checks: dict = {}

    def bridge(name, payload):
        return json.dumps(getattr(api, name)(*json.loads(payload)), ensure_ascii=False)

    def fetches():
        return len([c for c in calls if c[0] in ("feed", "search") and c[4] == "fresh" and c[1] != "shorts"])

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=shutil.which("google-chrome") or shutil.which("chromium"))
        pg = b.new_context(viewport={"width": 1280, "height": 800}).new_page()
        pg.expose_function("__owntube_call", bridge)
        pg.add_init_script(SHIM % json.dumps(names))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base)
        wait_js(pg, "document.querySelectorAll('.card').length >= 24", 20)
        checks["home_back_disabled_at_root"] = pg.evaluate("document.getElementById('backBtn').disabled")
        # дозагрузим страницу, прокрутим вниз
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        wait_js(pg, "document.querySelectorAll('.card').length >= 48", 20)
        pg.evaluate("window.scrollTo(0, 700)")
        pg.wait_for_timeout(300)
        y0 = pg.evaluate("window.scrollY")
        n_before = fetches()
        # открыть видео и вернуться кнопкой
        pg.evaluate("document.querySelector('.card:nth-child(30)').click()")
        wait_js(pg, "!!document.querySelector('.watch-back')", 10)
        shot_watch = out / "back_watch.png"
        pg.screenshot(path=str(shot_watch))
        checks["watch_back_button_visible"] = pg.is_visible(".watch-back")

        def home_restored(label):
            wait_js(pg, "document.querySelectorAll('.card').length === 48", 10)
            pg.wait_for_timeout(900)
            checks[label] = {"cards": pg.evaluate("document.querySelectorAll('.card').length"),
                             "scroll_ok": abs(pg.evaluate("window.scrollY") - y0) <= 3, "y": [y0, pg.evaluate("window.scrollY"), pg.evaluate("document.body.scrollHeight")],
                             "no_new_requests": fetches() == n_before}

        pg.click(".watch-back")
        home_restored("back_button")
        for label, act in (("alt_left", lambda: pg.keyboard.press("Alt+ArrowLeft")), ("backspace", lambda: pg.keyboard.press("Backspace")),
                           ("mouse_back", lambda: pg.evaluate("window.dispatchEvent(new MouseEvent('mouseup', {button: 3, bubbles: true}))"))):
            pg.evaluate("document.querySelector('.card:nth-child(30)').click()")
            wait_js(pg, "!!document.querySelector('.watch-back')", 10)
            act()
            home_restored(label)
        # Backspace в поле поиска не уходит «назад»
        pg.evaluate("document.querySelector('.card:nth-child(30)').click()")
        wait_js(pg, "!!document.querySelector('.watch-back')", 10)
        pg.click("#q"); pg.keyboard.type("кот"); pg.keyboard.press("Backspace")
        checks["backspace_in_input_stays"] = pg.evaluate("!!document.querySelector('.watch-back')") and pg.input_value("#q") == "ко"
        pg.keyboard.press("Escape"); pg.click(".watch-back")
        wait_js(pg, "document.querySelectorAll('.card').length === 48", 10)
        pg.fill("#q", "котики")
        pg.press("#q", "Enter")
        wait_js(pg, "document.querySelectorAll('.card').length >= 40", 10)
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        wait_js(pg, "document.querySelectorAll('.card').length >= 60", 10)
        pg.evaluate("window.scrollTo(0, 500)")
        pg.wait_for_timeout(300)
        ys = pg.evaluate("window.scrollY")
        n_s = fetches()
        pg.evaluate("document.querySelector('.card:nth-child(50)').click()")
        wait_js(pg, "!!document.querySelector('.watch-back')", 10)
        pg.click(".watch-back")
        wait_js(pg, "document.querySelectorAll('.card').length === 60", 10)
        pg.wait_for_timeout(900)
        checks["search_restored"] = {"cards": pg.evaluate("document.querySelectorAll('.card').length"), "q": pg.input_value("#q"),
                                     "scroll_ok": abs(pg.evaluate("window.scrollY") - ys) <= 3, "no_new_requests": fetches() == n_s}
        # Назад из поиска → главная
        pg.click("#backBtn")
        wait_js(pg, "document.querySelectorAll('.card').length === 48", 10)
        checks["search_back_to_home"] = True
        # Shorts: кнопка «Назад» доступна, возвращает к списку
        pg.click('#side .nav[data-view="shorts"]')
        wait_js(pg, "!!document.querySelector('.sh-vid, .shorts, .sh')", 10)
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / "back_shorts.png"))
        checks["shorts_back_visible"] = pg.is_visible("#backBtn") and not pg.evaluate("document.getElementById('backBtn').disabled")
        pg.keyboard.press("Alt+ArrowLeft")
        wait_js(pg, "document.querySelectorAll('.card').length >= 24", 10)
        checks["shorts_back_works"] = True
        # аккаунт: просроченный вход → понятная подсказка
        state["home_error"] = "Вход в Google устарел. Войдите заново."
        pg.click('#side .nav[data-view="subs"]')
        pg.wait_for_timeout(500)
        pg.click('#side .nav[data-view="home"]')
        wait_js(pg, "!!document.querySelector('.acct-hint')", 10)
        checks["relogin_hint"] = pg.inner_text(".acct-hint")[:120]
        pg.screenshot(path=str(out / "relogin_hint.png"))
        b.close()
    srv.shutdown()
    api._shutdown()
    checks["js_errors"] = errors
    print(json.dumps(checks, ensure_ascii=False, indent=1))
    ok = (not errors and checks["home_back_disabled_at_root"] and all(v is True or (isinstance(v, dict) and all(v.values() if "cards" not in v else [v["scroll_ok"], v["no_new_requests"]]))
                                                                   for k, v in checks.items() if k not in ("js_errors", "relogin_hint", "home_back_disabled_at_root") and v is not None))
    print("UI OK" if ok else "UI FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
