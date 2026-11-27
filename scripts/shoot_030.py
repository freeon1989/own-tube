# -*- coding: utf-8 -*-
"""Проверка 0.3.0 в Chromium без окна: настоящий Api, встроенный VPN через тестовый сервер awgproxy -test-server,
окно обновления через поддельный сервер релизов. Ключи — тестовые, сгенерированные на месте.

Запуск: python scripts/shoot_030.py <путь к awgproxy для этой ОС> [папка] → vpn.png, update.png и отчёт в консоли.
"""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")]

from playwright.sync_api import sync_playwright  # noqa: E402

from owntube import updater  # noqa: E402
from owntube.api import Api  # noqa: E402
from owntube.secretstore import MemoryBackend, SecretStore  # noqa: E402
from owntube.settings import SettingsStore  # noqa: E402
from shoot import SHIM, Quiet  # noqa: E402
from shoot_022 import wait_js  # noqa: E402
from test_030 import conf_from  # noqa: E402
from test_update import FakeGitHub  # noqa: E402

NOTES = """## Что нового в 0.3.1
- Несколько профилей встроенного VPN
- Быстрее открываются каналы
- Исправлена подгрузка Shorts при медленной сети"""


def main() -> None:
    helper = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="owntube-030-"))
    store = SettingsStore(home, secrets=SecretStore(home, MemoryBackend()))
    store.update({"download_dir": str(home / "dl")})
    server = subprocess.Popen([helper, "-test-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    info = json.loads(server.stdout.readline())
    gh = FakeGitHub(version="0.3.1")
    gh_base = f"http://127.0.0.1:{gh.port}"
    updater.API_LATEST, updater.PAGE_LATEST = gh_base + "/api/latest", gh_base + "/page/latest"
    real = updater._check_url
    updater._check_url = lambda u: None if u.startswith(gh_base + "/") else real(u)
    orig = gh.srv.RequestHandlerClass.do_GET

    def do_get(self):  # описание релиза как у настоящего
        if self.path == "/api/latest":
            body = {"tag_name": "v0.3.1", "body": NOTES, "html_url": "https://github.com/freeon1989/own-tube/releases/tag/v0.3.1",
                    "published_at": "2026-12-05T12:00:00Z",
                    "assets": [{"name": n, "browser_download_url": f"{gh_base}/download/v0.3.1/{n}"} for n in (updater.ASSET, updater.ASSET + ".sha256")]}
            return self.send(200, json.dumps(body, ensure_ascii=False).encode())
        return orig(self)
    gh.srv.RequestHandlerClass.do_GET = do_get

    api = Api(store=store, start_downloader=False)
    api._awg._find = lambda: helper
    sys.frozen = True  # окно обновления как в собранной программе (кнопка «Скачать и обновить»)
    checks: dict = {}
    r = api.vpn_import_text(conf_from(info["client"]))
    checks["import"] = r["ok"] and r["config"]["kind"]
    names = [n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "owntube" / "ui")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    errors: list[str] = []

    def bridge(name, payload):
        return json.dumps(getattr(api, name)(*json.loads(payload)), ensure_ascii=False)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=shutil.which("google-chrome") or shutil.which("chromium"))
        pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        pg.expose_function("__owntube_call", bridge)
        pg.add_init_script(SHIM % json.dumps(names))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "?view=settings")
        pg.wait_for_selector("#set-vpn")
        pg.click('#set-vpn [data-vpn="true"]')
        checks["vpn_running"] = wait_js(pg, "document.querySelector('#vpnStat').textContent.startsWith('Работает')", 30)
        pg.wait_for_timeout(5500)  # статус обновляется раз в 5 с (без замера до YouTube: у тестового сервера нет интернета)
        pg.evaluate("document.getElementById('set-vpn').scrollIntoView({block: 'start'})")
        pg.wait_for_timeout(300)
        checks["vpn_status"] = pg.inner_text("#vpnStat")
        checks["route_chip"] = pg.inner_text("#routeChip")
        pg.screenshot(path=str(out / "vpn.png"))
        checks["banner"] = wait_js(pg, "!!document.querySelector('.upd-banner')", 15) and pg.inner_text(".upd-banner")
        pg.click(".upd-banner button:has-text('Подробнее')")
        pg.click("#set-about button:has-text('Проверить обновления')")
        checks["update_shown"] = wait_js(pg, "document.querySelector('#set-about').textContent.includes('Доступна own-tube 0.3.1')", 15)
        pg.evaluate("document.getElementById('set-about').scrollIntoView({block: 'start'})")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / "update.png"))
        b.close()
    api.vpn_enable(False)
    api._shutdown()
    server.stdin.close()
    server.wait(timeout=5)
    gh.close()
    checks["github_requests"] = gh.hits
    checks["js_errors"] = errors
    print(json.dumps(checks, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
