# -*- coding: utf-8 -*-
"""Скриншоты макета (owntube/ui/mockup.html) в screenshots/mockup_*.png."""
from __future__ import annotations

import functools
import http.server
import shutil
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "screenshots"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def main() -> None:
    OUT.mkdir(exist_ok=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/mockup.html"
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome)
        errs: list[str] = []

        def page_for(w, h):
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(base)
            pg.wait_for_timeout(2500)
            return pg

        pg = page_for(1440, 900)
        pg.screenshot(path=str(OUT / "mockup_home_chat_1440x900.png"))
        pg.click(".switcher button[data-id=kate]")      # другая помощница: меняется картинка и цвет
        pg.click(".acts button:nth-child(3)")          # «Радуется»
        pg.wait_for_timeout(450)
        pg.screenshot(path=str(OUT / "mockup_home_kate_joy_1440x900.png"))
        pg.click(".switcher button[data-id=rei]")
        pg.click(".account")                            # вошёл в Google
        pg.click("#side .nav >> text=Настройки")
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(OUT / "mockup_settings_ai_1440x900.png"))
        pg.evaluate("document.getElementById('models').scrollIntoView()")
        pg.click("#settingsBody .tabs button:nth-child(2)")
        pg.wait_for_timeout(300)
        pg.evaluate("document.getElementById('models').scrollIntoView()")
        pg.screenshot(path=str(OUT / "mockup_settings_paid_1440x900.png"))
        pg.evaluate("document.getElementById('net').scrollIntoView()")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(OUT / "mockup_settings_net_1440x900.png"))
        pg.close()

        ph = page_for(412, 860)
        ph.screenshot(path=str(OUT / "mockup_phone_412_home.png"))
        ph.click("#chatFab")
        ph.wait_for_timeout(500)
        ph.screenshot(path=str(OUT / "mockup_phone_412_chat.png"))
        print("console errors:", errs)
        b.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
