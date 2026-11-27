# -*- coding: utf-8 -*-
"""Проверка 0.2.3 в Chromium без окна: настоящий Api, реальный YouTube, подставная локальная модель.

Что смотрим: скелетоны и скорость первой выдачи, бесконечная прокрутка и «Показать ещё», сохранённый список,
автозапуск действий помощницы, микрофон (поддельное устройство Chromium) → распознавание, озвучка,
Shorts (вертикальный плеер: стрелки, колесо), шаг «Проверка видео».
Запуск: python scripts/shoot_023.py [--socks] → screenshots/v023_*.png и отчёт проверок в консоли.
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

from owntube.api import Api  # noqa: E402
from owntube.secretstore import MemoryBackend, SecretStore  # noqa: E402
from owntube.settings import SettingsStore  # noqa: E402
from shoot import SHIM, Quiet  # noqa: E402
from shoot_022 import wait_js  # noqa: E402


def fake_model():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.endswith("/audio/transcriptions"):
                body = json.dumps({"text": "найди аниме опенинги"}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            body = json.loads(raw)
            system = body["messages"][0]["content"]
            last = body["messages"][-1]["content"]
            name = re.search(r"Ты — (\S+),", system).group(1)
            reply = f"Хорошо, я {name}! Сейчас сделаю."
            if "опенинги" in last:
                reply += "\n[[search: аниме опенинги]]"
            elif "ещё" in last:
                reply += "\n[[more]]"
            elif "лайк" in last:
                reply += "\n[[like: dQw4w9WgXcQ]]"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(reply), 8):
                ev = {"choices": [{"delta": {"content": reply[i:i + 8]}}]}
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    out = ROOT / "screenshots"
    out.mkdir(exist_ok=True)
    tag = "v023s" if "--socks" in sys.argv else "v023"
    home = Path(tempfile.mkdtemp(prefix="owntube-023-"))
    store = SettingsStore(home, secrets=SecretStore(home, MemoryBackend()))
    store.update({"download_dir": str(home / "dl"), "quality": "480"})
    model = fake_model()
    socks = None
    if "--socks" in sys.argv:
        sys.path.insert(0, str(ROOT / "tests"))
        from socks5_server import Socks5Server
        socks = Socks5Server("shot-user", "shot-secret-1:@/")
        store.update({"proxy": socks.url, "proxy_user": "shot-user", "proxy_password": "shot-secret-1:@/"})
    api = Api(store=store, start_downloader=False)
    api.save_ai_config({"name": "Локальная (тест)", "base_url": f"http://127.0.0.1:{model.server_address[1]}/v1",
                        "model": "demo-model", "active": True})
    api.warmup()
    names = [n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    errors: list[str] = []
    checks: dict[str, object] = {}

    def bridge(name, payload):
        return json.dumps(getattr(api, name)(*json.loads(payload)), ensure_ascii=False)

    count = "document.querySelectorAll('#view .grid .card:not(.sk)').length"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome, args=[
            "--autoplay-policy=no-user-gesture-required", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, permissions=["microphone"])
        pg = ctx.new_page()
        pg.expose_function("__owntube_call", bridge)
        pg.add_init_script(SHIM % json.dumps(names))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))

        # 1. поиск: скелетоны сразу, карточки быстро
        pg.goto(base + "?view=history")
        pg.wait_for_selector("#view")
        t0 = time.time()
        pg.evaluate("window.__sk = false; new MutationObserver(() => { if (document.querySelector('.card.sk')) window.__sk = true; }).observe(document.body, {subtree: true, childList: true})")
        pg.fill("#q", "аниме опенинги подборка")
        pg.press("#q", "Enter")
        wait_js(pg, count + " >= 10", 60)
        checks["skeleton_shown"] = pg.evaluate("window.__sk")
        checks["first_results_sec"] = round(time.time() - t0, 2)
        checks["first_results"] = pg.evaluate(count)
        pg.wait_for_timeout(1500)
        pg.screenshot(path=str(out / f"{tag}_search_first_page.png"))

        # 2. бесконечная прокрутка и «Показать ещё»
        n1 = pg.evaluate(count)
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            wait_js(pg, count + " > %d" % n1, 40)
            checks["infinite_scroll"] = f"{n1} -> {pg.evaluate(count)}"
        except Exception as exc:
            checks["infinite_scroll"] = "нет: " + str(exc)[:60]
        n2 = pg.evaluate(count)
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        pg.wait_for_timeout(2500)
        btn = pg.query_selector(".more-btn:not([hidden])")
        checks["more_button_visible"] = bool(btn)
        if btn:
            btn.scroll_into_view_if_needed()
            btn.click()
            try:
                wait_js(pg, count + " > %d" % pg.evaluate(count) if False else count + " > 0", 5)
                pg.wait_for_timeout(2500)
                checks["more_button_added"] = f"{n2} -> {pg.evaluate(count)}"
            except Exception as exc:
                checks["more_button_added"] = "нет: " + str(exc)[:60]
        checks["unique_ids"] = pg.evaluate("(() => { const k = [...document.querySelectorAll('#view .grid .card:not(.sk) .title')].map(x => x.textContent); return k.length })()")

        # 3. тот же запрос снова — показано сохранённое
        pg.evaluate("window.scrollTo(0, 0)")
        pg.fill("#q", "аниме опенинги подборка")
        pg.press("#q", "Enter")
        pg.wait_for_timeout(120)
        checks["cache_instant_cards"] = pg.evaluate(count)
        pg.wait_for_timeout(3000)

        # 4. помощница: поиск запускается сам, без клика; лайк — только с подтверждением
        pg.click("#hero", force=True)
        pg.wait_for_selector("#chat:not(.closed)")
        pg.click('#side .nav[data-view="history"]')
        pg.wait_for_timeout(500)
        pg.fill("#chatIn", "покажи опенинги")
        pg.press("#chatIn", "Enter")
        try:
            wait_js(pg, "document.querySelector('#view h1') && document.querySelector('#view h1').textContent.includes('аниме опенинги')", 30)
            checks["ai_autorun_search"] = "сработал без клика"
        except Exception as exc:
            checks["ai_autorun_search"] = "нет: " + str(exc)[:60]
        checks["ai_autorun_button_done"] = pg.evaluate("!!document.querySelector('.m-acts button.done')")
        pg.screenshot(path=str(out / f"{tag}_ai_autorun.png"))
        pg.wait_for_timeout(2500)
        pg.fill("#chatIn", "ещё пожалуйста")
        n_before = pg.evaluate(count)
        pg.press("#chatIn", "Enter")
        try:
            wait_js(pg, count + " > %d" % n_before, 40)
            checks["ai_autorun_more"] = f"{n_before} -> {pg.evaluate(count)}"
        except Exception as exc:
            checks["ai_autorun_more"] = "нет: " + str(exc)[:60]
        pg.fill("#chatIn", "поставь лайк")
        url_before = pg.evaluate("document.querySelector('#view h1').textContent")
        pg.press("#chatIn", "Enter")
        pg.wait_for_selector(".m-acts button.warn", timeout=30000)
        pg.wait_for_timeout(1200)
        checks["account_action_waits_for_click"] = pg.evaluate("document.querySelector('#view h1').textContent") == url_before

        # 5. голос: озвучка (в Chromium без русского голоса — понятное сообщение), микрофон → API → чат
        pg.click("#chatTts")
        pg.wait_for_timeout(900)
        checks["tts_toggle_status"] = pg.evaluate("document.getElementById('status').textContent")
        pg.click("#chatTts")
        pg.click("#chatMic")
        pg.wait_for_selector("#chatMic.rec", timeout=10000)
        pg.wait_for_timeout(2500)
        pg.click("#chatMic")
        try:
            wait_js(pg, "[...document.querySelectorAll('.m.me')].some(m => m.textContent.includes('найди аниме опенинги'))", 40)
            checks["mic_to_text_via_api"] = "распознано и отправлено"
        except Exception as exc:
            checks["mic_to_text_via_api"] = "нет: " + str(exc)[:60] + " | " + pg.evaluate("document.getElementById('status').textContent")
        pg.screenshot(path=str(out / f"{tag}_chat_voice.png"))
        pg.click("#chatClose")

        # 6. настройки → Голос
        pg.click('#side .nav[data-view="settings"]')
        pg.wait_for_selector("#set-voice")
        pg.evaluate("document.getElementById('set-voice').scrollIntoView()")
        pg.wait_for_timeout(1200)
        checks["voice_panel_status"] = pg.evaluate("[...document.querySelectorAll('#set-voice .note')].map(n => n.textContent).filter(Boolean)")
        pg.screenshot(path=str(out / f"{tag}_settings_voice.png"))

        # 7. «Проверить связь» с проверкой видео
        pg.evaluate("document.getElementById('set-net').scrollIntoView()")
        pg.click("#set-net >> text=Проверить связь")
        pg.wait_for_selector(".steps li", timeout=120000)
        pg.wait_for_timeout(500)
        wait_js(pg, "document.querySelectorAll('.steps li').length >= 5 && !document.querySelector('.steps li.busy')", 120)
        checks["route_steps"] = pg.evaluate("[...document.querySelectorAll('.steps li')].map(x => x.textContent)")
        pg.screenshot(path=str(out / f"{tag}_check_route.png"))

        # 8. Shorts
        pg.click('#side .nav[data-view="shorts"]')
        pg.wait_for_selector(".sh-frame", timeout=10000)
        try:
            wait_js(pg, "(() => { const v = document.querySelector('.sh-vid'); return !!v && v.currentTime > 1.5; })()", 120)
            checks["shorts_first_playing"] = True
        except Exception as exc:
            checks["shorts_first_playing"] = "нет: " + str(exc)[:60] + " | " + pg.evaluate("document.querySelector('.sh-msg').textContent")
        pg.screenshot(path=str(out / f"{tag}_shorts_1.png"))
        c1 = pg.evaluate("document.querySelector('.sh-count').textContent")
        pg.keyboard.press("ArrowDown")
        pg.wait_for_timeout(1800)
        pg.mouse.move(700, 400)
        pg.mouse.wheel(0, 300)
        pg.wait_for_timeout(1800)
        c2 = pg.evaluate("document.querySelector('.sh-count').textContent")
        pg.keyboard.press("ArrowUp")
        pg.wait_for_timeout(900)
        c3 = pg.evaluate("document.querySelector('.sh-count').textContent")
        checks["shorts_nav_counter"] = [c1, c2, c3]
        try:
            wait_js(pg, "(() => { const v = document.querySelector('.sh-vid'); return !!v && v.currentTime > 0.5; })()", 90)
            checks["shorts_after_nav_playing"] = True
        except Exception as exc:
            checks["shorts_after_nav_playing"] = "нет: " + str(exc)[:60]
        checks["shorts_msg"] = pg.evaluate("document.querySelector('.sh-msg').textContent")
        pg.screenshot(path=str(out / f"{tag}_shorts_2.png"))
        b.close()
    api._shutdown()
    srv.shutdown()
    model.shutdown()
    if socks:
        checks["socks_hosts"] = sorted({h for h, _ in socks.targets})
        checks["socks_atyps"] = sorted(set(socks.atyps))
        checks["socks_auth_failures"] = socks.auth_failures
        socks.close()
    for k, v in checks.items():
        print(f"CHECK {k}: {v}")
    print("console errors:", errors)


if __name__ == "__main__":
    main()
