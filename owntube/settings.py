# -*- coding: utf-8 -*-
"""Настройки в JSON в папке пользователя. Секретов здесь нет: ни паролей, ни токенов."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import validate

log = logging.getLogger("owntube.settings")
ASSISTANT_IDS = ("rei", "yuki", "kate", "lila", "nina")


def data_dir() -> Path:
    override = os.environ.get("OWNTUBE_HOME")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "own-tube"


def default_download_dir() -> str:
    return str(Path.home() / "Videos" / "own-tube")


@dataclass
class Settings:
    proxy: str = ""          # scheme://host:port, без логина и пароля
    quality: str = "best"
    codec: str = "auto"      # auto | h264
    mpv_path: str = ""       # пусто: искать mpv рядом с программой и в PATH
    sponsorblock: bool = True
    sb_categories: list = field(default_factory=lambda: ["sponsor", "selfpromo", "interaction"])
    theme: str = "assistant"  # assistant: тема выбранной помощницы
    assistant: str = "rei"
    download_dir: str = field(default_factory=default_download_dir)

    def public(self) -> dict:
        return asdict(self)


def _validated(raw: dict, strict: bool = False) -> Settings:
    s = Settings()

    def take(name, fn):
        if name not in raw:
            return
        try:
            setattr(s, name, fn(raw[name]))
        except ValueError:
            if strict:
                raise

    take("proxy", validate.clean_proxy)
    take("quality", validate.clean_quality)
    take("codec", lambda v: validate.clean_choice(v, validate.CODECS, "кодек"))
    take("theme", lambda v: validate.clean_choice(v, validate.THEMES, "тема"))
    take("assistant", lambda v: validate.clean_choice(v, ASSISTANT_IDS, "помощница"))

    def boolean(v):
        if not isinstance(v, bool):
            raise ValueError("Ожидалось да/нет")
        return v

    take("sponsorblock", boolean)

    def cats(v):
        if not isinstance(v, list) or any(c not in validate.SB_CATEGORIES for c in v):
            raise ValueError("Неизвестная категория SponsorBlock")
        return list(dict.fromkeys(v))

    take("sb_categories", cats)

    def path_text(v):
        if not isinstance(v, str) or len(v) > 500 or any(not ch.isprintable() for ch in v):
            raise ValueError("Некорректный путь")
        return v.strip()

    take("mpv_path", path_text)

    def dl_dir(v):
        v = path_text(v)
        if not v or not Path(v).is_absolute():
            raise ValueError("Папка загрузок должна быть полным путём")
        return v

    take("download_dir", dl_dir)
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
        for key in merged:
            if key in patch:
                merged[key] = patch[key]
        # Ошибки значения не глотаем: пользователь должен увидеть, что не так
        self.value = _validated(merged, strict=True)
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
