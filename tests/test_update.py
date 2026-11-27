# -*- coding: utf-8 -*-
"""0.3.0: обновление из релизов GitHub — проверка версии, скачивание с SHA-256, распаковка, замена файлов.

Вместо GitHub — локальный поддельный сервер релизов."""
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from owntube import updater
from owntube.api import Api
from owntube.db import Store
from owntube.network import Route
from owntube.secretstore import MemoryBackend, SecretStore
from owntube.settings import SettingsStore


def make_zip(version="9.9.9", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = f"own-tube-{version}-win-x64/"
        z.writestr(top + "own-tube.exe", b"MZ new " + version.encode())
        z.writestr(top + "tools/awgproxy.exe", b"MZ helper")
        z.writestr(top + "_internal/lib.dat", b"x" * 1000)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeGitHub:
    """api …/releases/latest, страница …/releases/latest (302) и файлы релиза с переадресацией, как у GitHub."""

    def __init__(self, version="9.9.9", zip_bytes=None, sha=None, api_status=200, redirect_to=None):
        self.version, self.api_status, self.redirect_to = version, api_status, redirect_to
        self.zip = zip_bytes if zip_bytes is not None else make_zip(version)
        self.sha = sha or hashlib.sha256(self.zip).hexdigest()
        self.hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.hits.append(self.path)
                base = f"http://127.0.0.1:{outer.port}"
                tag = "v" + outer.version
                if self.path == "/api/latest":
                    if outer.api_status != 200:
                        return self.send(outer.api_status, b'{"message":"API rate limit exceeded"}')
                    body = {"tag_name": tag, "body": "## Что нового\n- проверка", "html_url": base + "/tag/" + tag,
                            "published_at": "2026-11-27T12:00:00Z",
                            "assets": [{"name": n, "browser_download_url": f"{base}/download/{tag}/{n}"}
                                       for n in (updater.ASSET, updater.ASSET + ".sha256", "other.txt")]}
                    return self.send(200, json.dumps(body).encode())
                if self.path == "/page/latest":
                    return self.send(302, headers={"Location": f"https://github.com/freeon1989/own-tube/releases/tag/{tag}"})
                if self.path.startswith("/download/"):
                    to = outer.redirect_to or f"{base}/blob{self.path[len('/download'):]}"
                    return self.send(302, headers={"Location": to})
                if self.path.endswith(updater.ASSET):
                    return self.send(200, outer.zip, {"Content-Type": "application/zip"})
                if self.path.endswith(".sha256"):
                    return self.send(200, f"{outer.sha}  {updater.ASSET}\n".encode())
                self.send(404)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def gh(monkeypatch):
    servers = []

    def make(**kw):
        s = FakeGitHub(**kw)
        servers.append(s)
        base = f"http://127.0.0.1:{s.port}"
        monkeypatch.setattr(updater, "API_LATEST", base + "/api/latest")
        monkeypatch.setattr(updater, "PAGE_LATEST", base + "/page/latest")
        real = updater._check_url

        def check(url):  # локальный http вместо https GitHub; остальные правила те же
            if url.startswith(base + "/"):
                return
            real(url)
        monkeypatch.setattr(updater, "_check_url", check)
        return s
    yield make
    for s in servers:
        s.close()


def test_versions():
    assert updater.is_newer("0.3.1", "0.3.0") and updater.is_newer("v1.0.0", "0.9.9")
    assert not updater.is_newer("0.3.0", "0.3.0") and not updater.is_newer("0.2.9", "0.3.0")
    assert not updater.is_newer("0.4.0-beta", "0.3.0") and not updater.is_newer("garbage", "0.3.0")


def test_url_allowlist():
    ok = ["https://api.github.com/repos/freeon1989/own-tube/releases/latest",
          "https://github.com/freeon1989/own-tube/releases/download/v1.0.0/own-tube-win-x64.zip",
          "https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x"]
    for u in ok:
        updater._check_url(u)
    for u in ["http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x",
              "https://user@github.com/x", "https://github.com:8443/x", "file:///etc/passwd"]:
        with pytest.raises(updater.UpdateError):
            updater._check_url(u)


def test_check_and_download_verified(gh, tmp_path):
    s = gh()
    info = updater.check(Route(), current="0.3.0")
    assert info["available"] and info["version"] == "9.9.9" and info["has_assets"] and "Что нового" in info["notes"]
    assert updater.check(Route(), skip="9.9.9", current="0.3.0")["available"]  # доступна, но пропущена
    assert updater.check(Route(), skip="9.9.9", current="0.3.0")["skipped"]
    z = updater.download(info, Route(), tmp_path / "dl")
    assert z.read_bytes() == s.zip
    staged = updater.extract(z, tmp_path / "files")
    assert (staged / "own-tube.exe").read_bytes().startswith(b"MZ new")


def test_tampered_hash_rejected(gh, tmp_path):
    gh(sha="0" * 64)
    info = updater.check(Route(), current="0.3.0")
    with pytest.raises(updater.UpdateError, match="Контрольная сумма"):
        updater.download(info, Route(), tmp_path / "dl")
    assert not list((tmp_path / "dl").iterdir())  # ни архива, ни .part


def test_tampered_archive_rejected(gh, tmp_path):
    good = make_zip()
    gh(zip_bytes=good[:-10] + b"0123456789", sha=hashlib.sha256(good).hexdigest())
    with pytest.raises(updater.UpdateError):
        updater.download(updater.check(Route(), current="0.3.0"), Route(), tmp_path / "dl")


def test_redirect_off_github_rejected(gh, tmp_path):
    gh(redirect_to="https://evil.example/own-tube-win-x64.zip")
    info = updater.check(Route(), current="0.3.0")
    with pytest.raises(updater.UpdateError, match="вне GitHub"):
        updater.download(info, Route(), tmp_path / "dl")


def test_missing_assets(gh):
    gh()
    info = updater.check(Route(), current="0.3.0")
    info["assets"].pop(updater.ASSET + ".sha256")
    with pytest.raises(updater.UpdateError, match="sha256"):
        updater.download(info, Route(), Path("/nonexistent"))


def test_rate_limit_fallback(gh):
    s = gh(api_status=403)
    info = updater.check(Route(), current="0.3.0")
    assert info["version"] == "9.9.9" and info["available"]
    assert info["assets"][updater.ASSET].startswith("https://github.com/freeon1989/own-tube/releases/download/v9.9.9/")
    assert s.hits == ["/api/latest", "/page/latest"]


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "C:/win.txt", "own-tube/../../x"])
def test_zip_slip_rejected(tmp_path, bad):
    z = tmp_path / "a.zip"
    z.write_bytes(make_zip(extra={bad: b"x"}))
    with pytest.raises(updater.UpdateError):
        updater.extract(z, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _layout(tmp_path):
    app = tmp_path / "Programs" / "own-tube"
    (app / "tools").mkdir(parents=True)
    (app / "own-tube.exe").write_bytes(b"MZ old")
    (app / "tools" / "mpv.exe").write_bytes(b"old mpv")
    data = tmp_path / "AppData" / "own-tube"
    data.mkdir(parents=True)
    (data / "settings.json").write_text('{"theme": "neon"}')
    z = tmp_path / "u.zip"
    z.write_bytes(make_zip("0.3.1"))
    staged = updater.extract(z, tmp_path / "stage")
    return app, data, staged


def test_apply_swaps_and_keeps_backup(tmp_path):
    app, data, staged = _layout(tmp_path)
    plan = updater.make_plan(app, staged, "0.3.1", "0.3.0", pid=_dead_pid())
    res = updater.apply(plan, relaunch=False, wait=5)
    assert res["ok"], res
    assert (app / "own-tube.exe").read_bytes() == b"MZ new 0.3.1"
    assert not (app / "tools" / "mpv.exe").exists()  # папка заменена целиком
    backup = app.with_name("own-tube.old-0.3.0")
    assert (backup / "own-tube.exe").read_bytes() == b"MZ old"
    assert (data / "settings.json").read_text() == '{"theme": "neon"}'
    # следующее обновление: остаётся только одна резервная копия
    staged2 = updater.extract(tmp_path / "u.zip", tmp_path / "stage2")
    assert updater.apply(updater.make_plan(app, staged2, "0.3.2", "0.3.1", pid=_dead_pid()), relaunch=False, wait=5)["ok"]
    assert [p.name for p in updater.backups(app)] == ["own-tube.old-0.3.1"]


def test_apply_rolls_back_on_copy_failure(tmp_path, monkeypatch):
    app, _, staged = _layout(tmp_path)

    def boom(src, dst, **kw):
        Path(dst).mkdir()
        (Path(dst) / "half.bin").write_bytes(b"partial")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(updater.shutil, "copytree", boom)
    res = updater.apply(updater.make_plan(app, staged, "0.3.1", "0.3.0", pid=_dead_pid()), relaunch=False, wait=5)
    assert not res["ok"] and "возвращена прежняя" in res["error"]
    assert (app / "own-tube.exe").read_bytes() == b"MZ old" and not (app / "half.bin").exists()
    assert not updater.backups(app)


def test_apply_waits_for_running_app(tmp_path):
    app, _, staged = _layout(tmp_path)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        res = updater.apply(updater.make_plan(app, staged, "0.3.1", "0.3.0", pid=p.pid), relaunch=False, wait=1)
        assert not res["ok"] and "не закрылась" in res["error"]
        assert (app / "own-tube.exe").read_bytes() == b"MZ old"
    finally:
        p.kill()
        p.wait()


def test_apply_main_writes_result(tmp_path):
    app, data, staged = _layout(tmp_path)
    plan = updater.make_plan(app, staged, "0.3.1", "0.3.0", pid=_dead_pid())
    pp = data / "update-plan.json"
    pp.write_text(json.dumps(plan))
    import unittest.mock as m
    with m.patch.object(updater, "_start") as start:
        assert updater.apply_main(str(pp)) == 0
        start.assert_called_once()
    assert json.loads((data / "update-result.json").read_text())["ok"] and not pp.exists()


def test_api_flow_needs_click(gh, tmp_path, monkeypatch):
    gh()
    store = SettingsStore(tmp_path, SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=store, db=Store(tmp_path / "db.sqlite"), start_downloader=False)
    r = api.update_check(True)
    assert r["ok"] and r["available"] and r["version"] == "9.9.9"
    assert api.update_progress()["stage"] == "idle"  # проверка ничего не скачивает
    assert not api.update_download()["ok"]  # из исходников не ставится
    assert not api.update_apply()["ok"]
    assert api.update_skip("9.9.9")["ok"] and api.update_check(True)["skipped"]
    assert "update_skip" in json.loads((tmp_path / "settings.json").read_text())
    api.save_settings({"update_check": False})
    assert api.update_check(True).get("disabled")
    assert not api.save_settings({"update_skip": "../../x"})["ok"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "staging_root", lambda: tmp_path / "staging")
    monkeypatch.setattr("owntube.api.app_dir", lambda: tmp_path / "app")
    r = api.update_download()
    assert r["ok"]
    for _ in range(100):
        st = api.update_progress()
        if st["stage"] in ("ready", "error"):
            break
        threading.Event().wait(0.05)
    assert st["stage"] == "ready", st
    assert (Path(st["staged"]) / "own-tube.exe").is_file()
    api._shutdown()


def test_no_extra_requests_on_start(gh, tmp_path):
    s = gh()
    store = SettingsStore(tmp_path, SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=store, db=Store(tmp_path / "db.sqlite"), start_downloader=False)
    api.update_check(True)
    assert s.hits == ["/api/latest"]  # один запрос, без скачивания и статистики
    api._shutdown()
