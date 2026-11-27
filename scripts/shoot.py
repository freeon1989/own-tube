# -*- coding: utf-8 -*-
"""Скриншоты рабочего интерфейса без окна: Chromium + настоящий Api (реальные запросы yt-dlp).

Запуск: python scripts/shoot.py [префикс]   → screenshots/<префикс>_*.png (по умолчанию stage1)
Мост window.pywebview.api подменён вызовами в тот же Api, что использует окно. Данные — во временной папке:
туда импортируется маленький тестовый Takeout (2 подписки, история, плейлист).
"""
from __future__ import annotations

import functools
import http.server
import io
import json
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from owntube import takeout  # noqa: E402
from owntube.api import Api  # noqa: E402
from owntube.db import Store  # noqa: E402
from owntube.settings import SettingsStore  # noqa: E402

SHIM = """
(() => {
  const names = %s;
  const api = {};
  for (const n of names) api[n] = (...a) => window.__owntube_call(n, JSON.stringify(a)).then(JSON.parse);
  window.pywebview = { api };
  window.addEventListener('DOMContentLoaded', () => window.dispatchEvent(new Event('pywebviewready')));
  window.confirm = () => true;
})();
"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def sample_takeout(path: Path) -> None:
    subs = "Channel Id,Channel Url,Channel Title\n" \
           "UCSJ4gkVC6NrvII8umztf0Ow,http://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow,Lofi Girl\n" \
           "UCsXVk37bltHxD1rDPwtNM8Q,http://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q,Kurzgesagt – In a Nutshell\n"
    hist = [{"header": "YouTube", "title": "Watched Never Gonna Give You Up", "titleUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
             "subtitles": [{"name": "Rick Astley", "url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"}],
             "time": "2026-11-20T19:00:00.000Z"}]
    pl = "Video ID,Playlist Video Creation Timestamp\ndQw4w9WgXcQ,2026-11-01T10:00:00+00:00\njfKfPfyJRdk,2026-11-02T10:00:00+00:00\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv", subs)
        z.writestr("Takeout/YouTube and YouTube Music/history/watch-history.json", json.dumps(hist))
        z.writestr("Takeout/YouTube and YouTube Music/playlists/Аниме на вечер-videos.csv", pl)


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "stage1"
    out = ROOT / "screenshots"
    out.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="owntube-shot-"))
    store = SettingsStore(home)
    store.update({"download_dir": str(home / "Загрузки")})
    db = Store(home / "own-tube.db")
    tk = home / "takeout.zip"
    sample_takeout(tk)
    print("takeout:", takeout.import_path(tk, db))
    api = Api(store=store, db=db)
    names = [n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    errors: list[str] = []

    def bridge(name, payload):
        return json.dumps(getattr(api, name)(*json.loads(payload)), ensure_ascii=False)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome)

        def page(w, h, view="home"):
            ctx = b.new_context(viewport={"width": w, "height": h})
            pg = ctx.new_page()
            pg.expose_function("__owntube_call", bridge)
            pg.add_init_script(SHIM % json.dumps(names))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "?view=" + view)
            return pg

        def shot(pg, name, wait_sel=None, delay=1800):
            if wait_sel:
                pg.wait_for_selector(wait_sel, timeout=90000)
            pg.wait_for_timeout(delay)
            pg.screenshot(path=str(out / f"{prefix}_{name}.png"))
            print("shot", name)

        pg = page(1440, 900)
        shot(pg, "home_1440x900", ".card")
        pg.click(".card .more", force=True)
        shot(pg, "card_menu_1440x900", ".menu:not([hidden])", 500)
        pg.keyboard.press("Escape")
        # загрузка короткого ролика, чтобы показать экран «Загрузки»
        api.download({"source": "yt", "id": "jNQXAC9IVRw", "title": "Me at the zoo"}, "720")
        for view, sel in (("subs", ".card"), ("cat:music", ".card"), ("shorts", ".card"), ("history", ".card"),
                          ("playlists", ".pl-card"), ("downloads", ".dl")):
            pg.click(f'#side .nav[data-view="{view}"]')
            shot(pg, view.replace(":", "_") + "_1440x900", sel, 2500 if view == "downloads" else 1500)
        pg.click('#side .nav[data-view="vk"]')
        pg.fill("#q", "https://vk.com/videos-77521?z=video-77521_162222515%2Fclub77521")
        pg.press("#q", "Enter")
        shot(pg, "vk_1440x900", ".card .badge.src")
        pg.click('#side .nav[data-view="settings"]')
        shot(pg, "settings_look_1440x900", "#set-look")
        pg.evaluate("document.getElementById('set-play').scrollIntoView()")
        shot(pg, "settings_play_net_1440x900", None, 400)
        pg.evaluate("document.getElementById('set-data').scrollIntoView()")
        shot(pg, "settings_data_1440x900", None, 400)
        for theme in ("neon", "sakura", "light"):
            pg.click(f'.theme-opt:nth-child({ {"neon": 3, "sakura": 4, "light": 6}[theme] })')
            pg.click('#side .nav[data-view="home"]')
            shot(pg, f"theme_{theme}_1440x900", ".card", 1200)
            pg.click('#side .nav[data-view="settings"]')
            pg.wait_for_selector("#set-look")
        pg.click('.theme-opt:nth-child(1)')
        pg.click('#switcher button[data-id="kate"]')
        pg.click('#side .nav[data-view="home"]')
        pg.wait_for_selector(".card", timeout=60000)
        pg.click("#hero", force=True)
        pg.fill("#chatIn", "Подбери что-нибудь на вечер")
        pg.press("#chatIn", "Enter")
        shot(pg, "assistant_kate_chat_1440x900", ".m.me", 1500)

        ph = page(412, 860)
        shot(ph, "phone_412_home", ".card")
        b.close()
    srv.shutdown()
    print("console errors:", errors)


if __name__ == "__main__":
    main()
