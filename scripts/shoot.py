# -*- coding: utf-8 -*-
"""Скриншоты интерфейса без окна: Chromium + настоящий Api (реальный поиск yt-dlp).
Запуск: python scripts/shoot.py "запрос"   (нужны playwright и google-chrome/chromium)
Мост window.pywebview.api подменён вызовами в тот же Api, что использует окно."""
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
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from owntube.api import Api  # noqa: E402
from owntube.settings import SettingsStore  # noqa: E402

SHIM = """
(() => {
  const call = (name) => (...a) => window.__owntube_call(name, JSON.stringify(a)).then(JSON.parse);
  window.pywebview = { api: {
    get_settings: call('get_settings'), save_settings: call('save_settings'),
    search: call('search'), play: call('play') } };
  window.addEventListener('DOMContentLoaded', () => window.dispatchEvent(new Event('pywebviewready')));
})();
"""


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "lofi hip hop"
    out = ROOT / "screenshots"
    out.mkdir(exist_ok=True)
    api = Api(store=SettingsStore(Path(tempfile.mkdtemp())))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "owntube" / "ui"))
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k): pass
    handler = functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    chrome = shutil.which("google-chrome") or shutil.which("chromium")

    def bridge(name, payload):
        return json.dumps(getattr(api, name)(*json.loads(payload)), ensure_ascii=False)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome)
        for label, (w, h) in {"desktop_1440x900": (1440, 900), "phone_412": (412, 860)}.items():
            ctx = b.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            page.expose_function("__owntube_call", bridge)
            page.add_init_script(SHIM)
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(url)
            page.wait_for_timeout(500)
            if label.startswith("desktop"):
                page.screenshot(path=str(out / "empty_1440x900.png"))
            page.fill("#q", query)
            page.press("#q", "Enter")
            page.wait_for_selector(".card", timeout=60000)
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out / f"results_{label}.png"))
            if label.startswith("desktop"):
                page.click(".card")  # без mpv покажет понятную ошибку
                page.wait_for_timeout(8000)
                page.screenshot(path=str(out / "play_attempt_1440x900.png"))
                page.click("#openSettings")
                page.fill("#proxy", "socks5://127.0.0.1:1080")
                page.wait_for_timeout(300)
                page.screenshot(path=str(out / "settings_1440x900.png"))
            print(label, "console errors:", errors)
            ctx.close()
        b.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
