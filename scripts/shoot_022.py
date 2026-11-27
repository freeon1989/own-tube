# -*- coding: utf-8 -*-
"""Проверка 0.2.2 в Chromium без окна: настоящий Api, подставная локальная модель (OpenAI SSE, без ключа).

Запуск: python scripts/shoot_022.py → screenshots/v022_*.png и отчёт проверок в консоли.
"""
from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

from owntube.api import Api  # noqa: E402
from owntube.secretstore import MemoryBackend, SecretStore  # noqa: E402
from owntube.settings import SettingsStore  # noqa: E402
from shoot import SHIM, Quiet  # noqa: E402


def fake_model():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            system = body["messages"][0]["content"]
            ids = re.findall(r"^- ([A-Za-z0-9_-]{11}): (.*)$", system, re.M)
            name = re.search(r"Ты — (\S+),", system).group(1)
            reply = f"Привет, я {name}! "
            if ids:
                reply += f"Советую «{ids[0][1][:60]}» — включить?\n[[play: {ids[0][0]}]]\n[[search: lofi для работы]]"
            else:
                reply += "Давай поищем что-нибудь.\n[[search: аниме опенинги]]"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(reply), 6):
                ev = {"choices": [{"delta": {"content": reply[i:i + 6]}}]}
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_js(pg, expr, timeout=60):
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pg.evaluate(expr):
                return True
        except Exception:
            pass
        pg.wait_for_timeout(500)
    raise TimeoutError("не дождались: " + expr[:60])


def main() -> None:
    out = ROOT / "screenshots"
    tag = "v022s" if "--socks" in sys.argv else "v022"
    out.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="owntube-022-"))
    store = SettingsStore(home, secrets=SecretStore(home, MemoryBackend()))
    store.update({"download_dir": str(home / "dl"), "quality": "480"})
    model = fake_model()
    socks = None
    if "--socks" in sys.argv:  # весь трафик — через локальный SOCKS5 с логином (тестовый, только 127.0.0.1)
        sys.path.insert(0, str(ROOT / "tests"))
        from socks5_server import Socks5Server
        socks = Socks5Server("shot-user", "shot-secret-1:@/")
        store.update({"proxy": socks.url, "proxy_user": "shot-user", "proxy_password": "shot-secret-1:@/"})
    api = Api(store=store, start_downloader=False)
    api.save_ai_config({"name": "Локальная (тест)", "base_url": f"http://127.0.0.1:{model.server_address[1]}/v1",
                        "model": "demo-model", "active": True})
    (home / "own-tube.log").write_text("2026-11-27 12:00:00 owntube.api: тестовая строка журнала\n", encoding="utf-8")
    names = [n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    errors: list[str] = []
    checks: dict[str, object] = {}

    def bridge(name, payload):
        return json.dumps(getattr(api, name)(*json.loads(payload)), ensure_ascii=False)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome, args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.expose_function("__owntube_call", bridge)
        pg.add_init_script(SHIM % json.dumps(names))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "?view=search")
        pg.fill("#q", "Rick Astley Never Gonna Give You Up")
        pg.press("#q", "Enter")
        pg.wait_for_selector(".card img", timeout=90000)
        pg.wait_for_timeout(3000)
        checks["thumbs_loaded"] = pg.evaluate("[...document.querySelectorAll('.card img')].filter(i => i.naturalWidth > 0).length")
        checks["thumbs_src_local"] = pg.evaluate("[...document.querySelectorAll('.card img')].every(i => i.src.startsWith('http://127.0.0.1:'))")
        pg.screenshot(path=str(out / f"{tag}_search_thumbs.png"))
        # чат
        pg.click("#hero", force=True)
        pg.wait_for_selector("#chat:not(.closed)")
        pg.fill("#chatIn", "Что посмотреть из этого?")
        pg.press("#chatIn", "Enter")
        pg.wait_for_selector(".m-acts button", timeout=30000)
        checks["chat_reply"] = pg.evaluate("[...document.querySelectorAll('.m.ai')].pop().textContent")
        checks["chat_actions"] = pg.evaluate("[...document.querySelectorAll('.m-acts button')].map(b => b.textContent)")
        pg.screenshot(path=str(out / f"{tag}_chat_reply.png"))
        # действие «Включить» из чата → встроенный плеер
        pg.click(".m-acts button")
        pg.wait_for_selector("video.vid", timeout=30000)
        try:
            wait_js(pg, "(() => { const v = document.querySelector('video.vid'); return !!v && v.currentTime > 3; })()", 120)
            checks["video_playing"] = True
        except Exception as exc:
            checks["video_playing"] = "нет: " + str(exc)[:100]
        checks["video_state"] = pg.evaluate("(() => { const v = document.querySelector('video.vid'); return v ? {t: v.currentTime, w: v.videoWidth, h: v.videoHeight, err: v.error && v.error.code, src: (v.currentSrc||'').slice(0,40)} : null })()")
        pg.click("#chatClose")
        pg.wait_for_timeout(2500)
        pg.screenshot(path=str(out / f"{tag}_watch.png"))
        # перемотка (перезапуск ffmpeg с -ss)
        pg.evaluate("document.querySelector('.p-bar').dispatchEvent(new MouseEvent('click', {clientX: document.querySelector('.p-bar').getBoundingClientRect().left + document.querySelector('.p-bar').getBoundingClientRect().width * 0.5, bubbles: true}))")
        try:
            wait_js(pg, "(() => { const t = document.querySelector('.p-time').textContent.split(' / '); const p = s => s.split(':').reduce((a, x) => a * 60 + Number(x), 0); return p(t[0]) > p(t[1]) * 0.45 && document.querySelector('video.vid').currentTime > 1; })()", 90)
            checks["seek_ok"] = pg.evaluate("document.querySelector('.p-time').textContent")
        except Exception as exc:
            checks["seek_ok"] = "нет: " + str(exc)[:80]
        pg.wait_for_selector(".w-related .card", timeout=90000)
        pg.screenshot(path=str(out / f"{tag}_watch_seek_related.png"))
        pg.click(".p-ctl .p-btn[aria-label='Во весь экран']")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(out / f"{tag}_watch_fullscreen.png"))
        pg.keyboard.press("Escape")
        # настройки: сеть (по шагам), журнал, аккаунты
        pg.click('#side .nav[data-view="settings"]')
        pg.wait_for_selector("#set-net")
        pg.evaluate("document.getElementById('set-net').scrollIntoView()")
        pg.click("#set-net >> text=Проверить связь")
        pg.wait_for_selector(".steps li", timeout=90000)
        checks["steps"] = pg.evaluate("[...document.querySelectorAll('.steps li')].map(x => x.textContent)")
        pg.screenshot(path=str(out / f"{tag}_settings_net_steps.png"))
        pg.click("#set-net >> text=Журнал")
        pg.wait_for_selector(".log-dlg pre", timeout=10000)
        pg.wait_for_timeout(600)
        pg.screenshot(path=str(out / f"{tag}_log.png"))
        pg.click(".log-dlg >> text=Закрыть")
        pg.evaluate("document.getElementById('set-accounts').scrollIntoView()")
        pg.wait_for_timeout(400)
        pg.screenshot(path=str(out / f"{tag}_settings_accounts.png"))
        pg.evaluate("document.getElementById('set-play').scrollIntoView()")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{tag}_settings_play.png"))
        # VK поиск
        pg.click("#tabVk")
        pg.fill("#q", "котики")
        pg.press("#q", "Enter")
        pg.wait_for_selector(".card .badge.src", timeout=60000)
        pg.wait_for_timeout(2500)
        checks["vk_results"] = pg.evaluate("document.querySelectorAll('.card').length")
        checks["vk_thumbs"] = pg.evaluate("[...document.querySelectorAll('.card img')].filter(i => i.naturalWidth > 0).length")
        pg.screenshot(path=str(out / f"{tag}_vk_search.png"))
        b.close()
    api._shutdown()
    srv.shutdown()
    model.shutdown()
    if socks:
        checks["socks_hosts"] = sorted({h for h, _ in socks.targets})
        checks["socks_auth_failures"] = socks.auth_failures
        socks.close()
    for k, v in checks.items():
        print(f"CHECK {k}: {v}")
    print("console errors:", errors)


if __name__ == "__main__":
    main()
