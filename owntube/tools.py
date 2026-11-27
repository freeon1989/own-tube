# -*- coding: utf-8 -*-
"""Где лежат внешние программы: mpv, ffmpeg, deno. Сначала рядом с программой, потом PATH."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

WIN = sys.platform.startswith("win")


def app_dir() -> Path:
    """Папка программы: рядом с exe в сборке, корень репозитория при запуске из исходников."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _exe(name: str) -> str:
    return name + ".exe" if WIN else name


def find_tool(name: str, configured: str = "") -> str | None:
    """name: mpv, ffmpeg или deno. configured — путь из настроек (необязательно)."""
    candidates = []
    if configured:
        candidates.append(Path(configured))
    base = app_dir()
    candidates += [base / "tools" / _exe(name), base / name / _exe(name)]
    for p in candidates:
        try:
            if p.is_file() and p.name.lower().startswith(name) and os.access(p, os.X_OK):
                return str(p)
        except OSError:
            continue
    return shutil.which(name)
