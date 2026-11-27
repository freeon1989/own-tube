# -*- coding: utf-8 -*-
"""Обновление own-tube из релизов GitHub (freeon1989/own-tube).

Что делает и чего не делает:
- Один запрос к api.github.com/repos/freeon1989/own-tube/releases/latest (через маршрут программы: прокси или VPN).
  Если лимит API исчерпан — номер версии берётся из перенаправления github.com/…/releases/latest. Больше ничего не отправляется.
- Ничего не ставится без нажатия «Обновить».
- Скачивается own-tube-win-x64.zip и own-tube-win-x64.zip.sha256 того же релиза, только https и только с хостов GitHub;
  SHA-256 сверяется до распаковки. Архив распаковывается в папку подготовки с проверкой путей.
- Замену выполняет новая версия, запущенная из папки подготовки с ключом --apply-update: ждёт выхода программы,
  переименовывает текущую папку в резервную (own-tube.old-<версия>), копирует новую, при сбое возвращает старую,
  запускает программу. Данные в %APPDATA%\\own-tube не трогаются.

Каждый релиз обязан содержать own-tube-win-x64.zip и own-tube-win-x64.zip.sha256 (см. scripts/README.md).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

from . import __version__, httpc
from .network import Route

log = logging.getLogger("owntube.updater")

REPO = "freeon1989/own-tube"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_LATEST = f"https://github.com/{REPO}/releases/latest"
ASSET = "own-tube-win-x64.zip"
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")
MAX_ZIP = 1024 * 1024 * 1024
MAX_UNPACKED = 3 * 1024 * 1024 * 1024
EXE = "own-tube.exe"
_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """Ошибка обновления с текстом для пользователя."""


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = _VER.match((v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def _check_url(url: str) -> None:
    p = urlsplit(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS or p.username or p.port not in (None, 443):
        raise UpdateError("Адрес обновления вне GitHub — отказ")


def _headers() -> dict:
    return {"User-Agent": f"own-tube/{__version__} (+https://github.com/{REPO})", "Accept": "application/vnd.github+json"}


def _open(url: str, route: Route, timeout: float = 30, hops: int = 6):
    """GET с ручным следованием перенаправлениям: каждый адрес проверяется по списку хостов GitHub."""
    for _ in range(hops):
        _check_url(url)
        resp = httpc.open_url("GET", url, route, {**_headers(), "Accept": "*/*"}, timeout=timeout)
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location") or ""
            resp.close()
            url = urljoin(url, loc)
            continue
        return resp
    raise UpdateError("Слишком много перенаправлений")


def latest(route: Route) -> dict:
    """Сведения о последнем релизе: version, notes, url, published, assets{name: url}."""
    _check_url(API_LATEST)
    try:
        status, _, body = httpc.fetch("GET", API_LATEST, route, _headers(), limit=2 * 1024 * 1024, timeout=20)
    except OSError as exc:
        raise UpdateError("GitHub не отвечает: " + (getattr(exc, "strerror", None) or str(exc))[:120]) from None
    if status == 200:
        data = json.loads(body.decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or [] if isinstance(a, dict)}
        return {"version": tag.lstrip("v"), "tag": tag, "notes": str(data.get("body") or "")[:20000],
                "url": str(data.get("html_url") or PAGE_LATEST), "published": str(data.get("published_at") or ""),
                "assets": {k: v for k, v in assets.items() if isinstance(k, str) and isinstance(v, str)}}
    if status in (403, 429):
        # лимит API (60 запросов в час на адрес) — версия из перенаправления страницы релиза
        resp = httpc.open_url("GET", PAGE_LATEST, route, {"User-Agent": _headers()["User-Agent"]}, timeout=20)
        loc = resp.getheader("Location") or ""
        resp.close()
        m = re.search(r"/releases/tag/(v?\d+\.\d+\.\d+)$", loc)
        if not m:
            raise UpdateError("GitHub ограничил запросы — попробуйте позже")
        tag = m.group(1)
        base = f"https://github.com/{REPO}/releases/download/{tag}/"
        return {"version": tag.lstrip("v"), "tag": tag, "notes": "", "url": f"https://github.com/{REPO}/releases/tag/{tag}",
                "published": "", "assets": {ASSET: base + ASSET, ASSET + ".sha256": base + ASSET + ".sha256"}}
    raise UpdateError(f"GitHub ответил HTTP {status}")


def check(route: Route, skip: str = "", current: str = __version__) -> dict:
    info = latest(route)
    if not parse_version(info["version"]):
        raise UpdateError("Номер версии релиза не распознан")
    info["current"] = current
    info["available"] = is_newer(info["version"], current)
    info["skipped"] = bool(skip) and skip == info["version"]
    info["has_assets"] = ASSET in info["assets"] and ASSET + ".sha256" in info["assets"]
    return info


def _expected_sha(route: Route, url: str) -> str:
    resp = _open(url, route, timeout=20)
    try:
        if resp.status != 200:
            raise UpdateError(f"Нет файла контрольной суммы (HTTP {resp.status})")
        text = resp.read(4096).decode("utf-8", "replace")
    finally:
        resp.close()
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("Файл контрольной суммы повреждён")
    return m.group(1).lower()


def download(info: dict, route: Route, dest: Path, progress=lambda done, total: None, cancel=lambda: False) -> Path:
    """Скачивает архив и сверяет SHA-256. Возвращает путь к проверенному zip."""
    assets = info.get("assets") or {}
    if ASSET not in assets or ASSET + ".sha256" not in assets:
        raise UpdateError("В релизе нет архива own-tube-win-x64.zip или его .sha256")
    want = _expected_sha(route, assets[ASSET + ".sha256"])
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / ASSET
    part = dest / (ASSET + ".part")
    resp = _open(assets[ASSET], route, timeout=60)
    h = hashlib.sha256()
    done = 0
    try:
        if resp.status != 200:
            raise UpdateError(f"Архив не скачался (HTTP {resp.status})")
        total = int(resp.getheader("Content-Length") or 0)
        if total > MAX_ZIP:
            raise UpdateError("Архив слишком большой")
        with open(part, "wb") as f:
            while True:
                if cancel():
                    raise UpdateError("Отменено")
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_ZIP:
                    raise UpdateError("Архив слишком большой")
                h.update(chunk)
                f.write(chunk)
                progress(done, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        resp.close()
    got = h.hexdigest()
    if got != want:
        part.unlink(missing_ok=True)
        log.warning("Обновление %s: SHA-256 не совпал, архив удалён", info.get("version"))
        raise UpdateError("Контрольная сумма архива не совпала — обновление отменено, файл удалён")
    os.replace(part, target)
    log.info("Обновление %s скачано и проверено (SHA-256 %s…)", info.get("version"), got[:12])
    return target


def extract(zip_path: Path, dest: Path) -> Path:
    """Распаковывает с проверкой путей. Возвращает папку с own-tube.exe."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            name = m.filename.replace("\\", "/")
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name) or name.startswith("/"):
                raise UpdateError("В архиве недопустимый путь — отказ")
            if (m.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError("В архиве символическая ссылка — отказ")
            total += m.file_size
            if total > MAX_UNPACKED:
                raise UpdateError("Архив распаковывается слишком большим — отказ")
        z.extractall(dest)
    for cand in [dest, *sorted(x for x in dest.iterdir() if x.is_dir())]:
        if (cand / EXE).is_file():
            return cand
    raise UpdateError("В архиве нет own-tube.exe")


# ---------- замена файлов ----------
def staging_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") if sys.platform.startswith("win") else None
    root = Path(base) / "own-tube" if base else Path(tempfile.gettempdir()) / "own-tube"
    return root / "update"


def backup_dir(app_dir: Path, version: str) -> Path:
    return app_dir.with_name(f"{app_dir.name}.old-{version}")


def backups(app_dir: Path) -> list[Path]:
    out = [p for p in app_dir.parent.glob(app_dir.name + ".old-*") if p.is_dir() and (p / EXE).is_file()]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def can_write(folder: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".own-tube-w", delete=True):
            return True
    except OSError:
        return False


def make_plan(app_dir: Path, staged: Path, new_version: str, old_version: str = __version__, pid: int | None = None) -> dict:
    return {"pid": pid or os.getpid(), "app_dir": str(app_dir), "staged": str(staged), "to": new_version,
            "from": old_version, "exe": EXE, "created": time.time()}


def launch(plan: dict, data_dir: Path) -> None:
    """Запускает новую версию из папки подготовки в режиме замены. Вызывающий после этого закрывает окно."""
    plan_path = data_dir / "update-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    exe = Path(plan["staged"]) / plan["exe"]
    flags = 0x00000008 | 0x00000200 if sys.platform.startswith("win") else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), "--apply-update", str(plan_path)], cwd=str(Path(plan["staged"])), shell=False,
                     creationflags=flags, close_fds=True)


def _wait_pid(pid: int, timeout: float) -> bool:
    end = time.time() + timeout
    if sys.platform.startswith("win"):
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return True
        try:
            return k.WaitForSingleObject(h, int(timeout * 1000)) == 0
        finally:
            k.CloseHandle(h)
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def _retry(fn, seconds: float = 30):
    end = time.time() + seconds
    while True:
        try:
            return fn()
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.5)  # антивирус или индексатор ещё держат файл


def apply(plan: dict, relaunch: bool = True, wait: float = 60) -> dict:
    """Шаги замены. Возвращает итог (он же пишется рядом с планом)."""
    app_dir, staged = Path(plan["app_dir"]), Path(plan["staged"])
    result = {"ok": False, "from": plan.get("from"), "to": plan.get("to")}
    if not _wait_pid(int(plan["pid"]), wait):
        result["error"] = "Программа не закрылась — обновление отложено"
        return result
    if not (staged / plan["exe"]).is_file() or not app_dir.is_dir():
        result["error"] = "Нет подготовленных файлов"
        return result
    backup = backup_dir(app_dir, str(plan.get("from") or "prev"))
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    try:
        _retry(lambda: os.replace(app_dir, backup))
    except OSError as exc:
        result["error"] = "Папка программы занята: " + (exc.strerror or str(exc))
        return result
    try:
        shutil.copytree(staged, app_dir)
    except Exception as exc:
        shutil.rmtree(app_dir, ignore_errors=True)
        _retry(lambda: os.replace(backup, app_dir))
        result["error"] = "Не удалось скопировать новую версию, возвращена прежняя: " + str(exc)[:160]
        if relaunch:
            _start(app_dir / plan["exe"])
        return result
    for old in backups(app_dir):  # храним одну резервную копию — предыдущую версию
        if old != backup:
            shutil.rmtree(old, ignore_errors=True)
    result.update(ok=True, backup=str(backup))
    if relaunch:
        _start(app_dir / plan["exe"])
    return result


def _start(exe: Path) -> None:
    flags = 0x00000008 if sys.platform.startswith("win") else 0
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), shell=False, creationflags=flags, close_fds=True)
    except OSError as exc:
        log.warning("Не удалось запустить программу после обновления: %s", exc)


def apply_main(plan_path: str) -> int:
    """Точка входа: own-tube.exe --apply-update <план> (запускается из папки подготовки)."""
    p = Path(plan_path)
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 2
    if Path(sys.executable).resolve().parent == Path(plan["app_dir"]).resolve():
        return 3  # из папки программы замену не делаем: она сама будет заменена
    res = apply(plan)
    try:
        p.with_name("update-result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        p.unlink()
    except OSError:
        pass
    return 0 if res["ok"] else 1


def cleanup_staging() -> None:
    """При запуске: удалить старые папки подготовки (программа из них уже не работает)."""
    root = staging_root()
    if root.is_dir() and root.resolve() not in Path(sys.executable).resolve().parents:
        shutil.rmtree(root, ignore_errors=True)


class Job:
    """Фоновое скачивание и подготовка: состояние для интерфейса."""

    def __init__(self):
        self.state = {"stage": "idle"}
        self._cancel = False
        self._lock = threading.Lock()

    def start(self, info: dict, route: Route) -> None:
        with self._lock:
            if self.state.get("stage") in ("download", "extract"):
                return
            self._cancel = False
            self.state = {"stage": "download", "version": info["version"], "done": 0, "total": 0}
        threading.Thread(target=self._run, args=(info, route), daemon=True, name="update").start()

    def cancel(self) -> None:
        self._cancel = True

    def _run(self, info: dict, route: Route) -> None:
        root = staging_root() / info["version"]
        try:
            z = download(info, route, root, lambda d, t: self.state.update(done=d, total=t), lambda: self._cancel)
            self.state.update(stage="extract")
            staged = extract(z, root / "files")
            z.unlink(missing_ok=True)
            self.state.update(stage="ready", staged=str(staged))
        except UpdateError as exc:
            self.state.update(stage="error", error=str(exc))
        except Exception as exc:
            log.warning("Обновление не подготовлено: %s", exc)
            self.state.update(stage="error", error="Не удалось подготовить обновление: " + str(exc)[:160])
