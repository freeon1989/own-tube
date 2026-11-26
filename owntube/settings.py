# -*- coding: utf-8 -*-
"""Настройки в JSON в папке пользователя. Секретов здесь нет: ни паролей, ни токенов."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import validate

log = logging.getLogger("owntube.settings")


def data_dir() -> Path:
    override = os.environ.get("OWNTUBE_HOME")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "own-tube"


@dataclass
class Settings:
    proxy: str = ""      # scheme://host:port, без логина и пароля
    quality: str = "best"
    mpv_path: str = ""   # пусто: искать mpv рядом с программой и в PATH

    def public(self) -> dict:
        return asdict(self)


def _validated(raw: dict) -> Settings:
    s = Settings()
    try:
        s.proxy = validate.clean_proxy(raw.get("proxy", ""))
    except ValueError:
        s.proxy = ""
    try:
        s.quality = validate.clean_quality(raw.get("quality", "best"))
    except ValueError:
        s.quality = "best"
    mpv = raw.get("mpv_path", "")
    s.mpv_path = mpv.strip()[:500] if isinstance(mpv, str) else ""
    return s


class SettingsStore:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or data_dir()
        self.path = self.dir / "settings.json"
        self.value = self._load()

    def _load(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return _validated(raw)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("Файл настроек повреждён, беру значения по умолчанию")
        return Settings()

    def update(self, patch: dict) -> Settings:
        if not isinstance(patch, dict):
            raise ValueError("Ожидался объект настроек")
        merged = self.value.public()
        for key in ("proxy", "quality", "mpv_path"):
            if key in patch:
                merged[key] = patch[key]
        # Ошибки значения не глотаем: пользователь должен увидеть, что не так
        merged["proxy"] = validate.clean_proxy(merged["proxy"])
        merged["quality"] = validate.clean_quality(merged["quality"])
        if not isinstance(merged["mpv_path"], str):
            raise ValueError("Путь к mpv должен быть строкой")
        self.value = _validated(merged)
        self._save()
        return self.value

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.value.public(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
