# -*- coding: utf-8 -*-
"""Пометка «получено из интернета» (Mark-of-the-Web, поток Zone.Identifier) на файлах программы.

Проводник Windows кладёт эту пометку на каждый файл zip, скачанного браузером. .NET Framework отказывается
загружать такие сборки (pythonnet: «Failed to resolve Python.Runtime.Loader.Initialize»), и окно не открывается.
Здесь пометка снимается ТОЛЬКО с файлов внутри папки программы — до импорта webview. Ссылки и точки
повторной обработки (junction) не обходятся, наружу папки ничего не трогается.
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path
from typing import Callable

log = logging.getLogger("owntube.motw")

STREAM = ":Zone.Identifier"
CONFIG_NAME = "own-tube.exe.config"


def _remove_stream(path: str) -> bool | None:
    """True — пометка была и снята; False — была, но снять не удалось; None — пометки нет."""
    target = path + STREAM
    try:
        os.remove(target)
        return True
    except FileNotFoundError:
        return None
    except OSError as exc:
        # winerror 2/3 — потока нет; 123/87 — файловая система без потоков (FAT, сетевая папка)
        if getattr(exc, "winerror", 0) in (2, 3, 87, 123):
            return None
        return False


def _is_link(entry: "os.DirEntry") -> bool:
    try:
        if entry.is_symlink():
            return True
        st = entry.stat(follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def strip(root: Path | str, remover: Callable[[str], bool | None] = _remove_stream) -> dict:
    """Снимает пометки со всех файлов под root. Возвращает {"removed": n, "failed": [имена], "files": n}."""
    base = Path(root).resolve()
    out = {"removed": 0, "failed": [], "files": 0}
    stack = [str(base)]
    while stack:
        cur = stack.pop()
        try:
            it = list(os.scandir(cur))
        except OSError:
            continue
        for e in it:
            if _is_link(e):
                continue
            try:
                inside = Path(e.path).resolve().is_relative_to(base)
            except OSError:
                inside = False
            if not inside:
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
            elif e.is_file(follow_symlinks=False):
                out["files"] += 1
                r = remover(e.path)
                if r is True:
                    out["removed"] += 1
                elif r is False:
                    out["failed"].append(str(Path(e.path).relative_to(base)))
    return out


def strip_app_dir() -> dict | None:
    """Запуск из собранной программы на Windows: снять пометки в папке с own-tube.exe. Иначе ничего не делает."""
    if not (sys.platform.startswith("win") and getattr(sys, "frozen", False)):
        return None
    try:
        res = strip(Path(sys.executable).resolve().parent)
    except Exception as exc:  # запуск не должен падать из-за уборки
        log.warning("Снятие пометок «из интернета» не удалось: %s", exc)
        return None
    if res["removed"] or res["failed"]:
        log.info("Снята пометка «из интернета»: %d файлов, не удалось: %d", res["removed"], len(res["failed"]))
    return res


def has_runtime_config(app_dir: Path) -> bool:
    """Есть ли own-tube.exe.config с loadFromRemoteSources=true (сборка обязана его класть)."""
    p = app_dir / CONFIG_NAME
    try:
        import xml.etree.ElementTree as ET
        node = ET.parse(p).getroot().find("./runtime/loadFromRemoteSources")
        return node is not None and node.get("enabled", "").lower() == "true"
    except Exception:
        return False


FIX_TEXT = (
    "own-tube не смог запустить окно (компонент .NET / pythonnet).\n\n"
    "Чаще всего причина — Windows пометила файлы как «скачанные из интернета» (так бывает после распаковки zip "
    "проводником), и .NET отказывается их загружать.\n\n"
    "Что сделать:\n"
    "1. Закройте это окно. Откройте PowerShell и выполните (путь замените на свою папку с own-tube.exe):\n"
    "   Get-ChildItem -Recurse \"C:\\путь\\к\\own-tube\" | Unblock-File\n"
    "2. Либо удалите папку, правой кнопкой по zip → «Свойства» → поставьте «Разблокировать» → «OK», и только потом распакуйте.\n"
    "3. Запустите own-tube.exe снова.\n\n"
    "Если не помогло — пришлите файл own-tube.log из %APPDATA%\\own-tube (в нём нет паролей и ключей).\n\n"
    "Техническая причина: {reason}"
)


GENERIC_TEXT = ("own-tube не смог запустить окно.\n\nПодробности записаны в own-tube.log в папке %APPDATA%\\own-tube "
                "(в нём нет паролей и ключей). Пришлите этот файл, если ошибка повторяется.\n\nТехническая причина: {reason}")


def is_dotnet_failure(exc: BaseException) -> bool:
    import traceback
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return any(k in text for k in ("clr_loader", "pythonnet", "Python.Runtime", "import clr", "\\clr.py"))


def friendly_message(exc: BaseException, strip_result: dict | None = None) -> str:
    reason = (str(exc).strip().splitlines() or [type(exc).__name__])[-1][:300]
    msg = (FIX_TEXT if is_dotnet_failure(exc) else GENERIC_TEXT).format(reason=f"{type(exc).__name__}: {reason}")
    if strip_result and strip_result.get("failed"):
        msg += f"\n\nНе удалось снять пометку с {len(strip_result['failed'])} файлов (например {strip_result['failed'][0]})."
    return msg


def show_error(text: str, title: str = "own-tube не запустился") -> None:
    """Окно с текстом ошибки (Windows); иначе — в stderr."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(title + "\n" + text, file=sys.stderr)
