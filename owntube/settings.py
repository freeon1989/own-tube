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
from .ai.config import PRESETS, ai_secret_name, clean_config, public_config
from .secretstore import SecretStore

log = logging.getLogger("owntube.settings")
ASSISTANT_IDS = ("rei", "yuki", "kate", "lila", "nina")
_MISSING = object()
# Эти ключи приходят из интерфейса, но в settings.json не пишутся
_EPHEMERAL = (
    "proxy_password", "clear_proxy_password", "proxy_type",
    "proxy_password_set", "proxy_password_note", "proxy_password_persist",
    "api_key", "api_key_set",
)


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
    proxy_user: str = ""     # логин прокси; пароль хранится отдельно и зашифрован
    ai_configs: list = field(default_factory=list)  # без ключей API
    quality: str = "best"
    codec: str = "auto"      # auto | h264
    mpv_path: str = ""       # пусто: искать mpv рядом с программой и в PATH
    player_mode: str = "embedded"  # embedded: в окне программы | mpv: отдельное окно
    vk_adult: bool = False   # 18+ в поиске VK (только после входа в VK)
    sponsorblock: bool = True
    sb_categories: list = field(default_factory=lambda: ["sponsor", "selfpromo", "interaction"])
    theme: str = "assistant"  # assistant: тема выбранной помощницы
    assistant: str = "rei"
    download_dir: str = field(default_factory=default_download_dir)

    def public(self) -> dict:
        return asdict(self)



def _clean_ai_configs(value: object) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Список конфигураций ИИ должен быть массивом")
    out, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Конфигурация ИИ должна быть объектом")
        # ключ, если вдруг попал в JSON, отбрасываем
        item = {k: v for k, v in item.items() if k not in ("api_key", "key", "token")}
        cfg = clean_config(item, existing_id=item.get("id"))
        if cfg["id"] in seen:
            continue
        seen.add(cfg["id"])
        out.append(cfg)
    # только одна активная
    active = [c for c in out if c.get("active")]
    if len(active) > 1:
        keep = active[-1]["id"]
        for c in out:
            c["active"] = c["id"] == keep
    return out


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
    take("proxy_user", validate.clean_proxy_user)
    take("ai_configs", _clean_ai_configs)
    take("quality", validate.clean_quality)
    take("player_mode", lambda v: validate.clean_choice(v, ("embedded", "mpv"), "режим плеера"))
    take("codec", lambda v: validate.clean_choice(v, validate.CODECS, "кодек"))
    take("theme", lambda v: validate.clean_choice(v, validate.THEMES, "тема"))
    take("assistant", lambda v: validate.clean_choice(v, ASSISTANT_IDS, "помощница"))

    def boolean(v):
        if not isinstance(v, bool):
            raise ValueError("Ожидалось да/нет")
        return v

    take("sponsorblock", boolean)
    take("vk_adult", boolean)

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


def _default_scheme(proxy_type, current_proxy: str) -> str:
    if proxy_type is not None:
        return validate.clean_proxy_type(proxy_type)
    if current_proxy:
        scheme = current_proxy.split("://", 1)[0].lower()
        if scheme in validate.PROXY_SCHEMES:
            return "http" if scheme in ("http", "https") else "socks5"
    return "socks5"


class SettingsStore:
    def __init__(self, directory: Path | None = None, secrets: SecretStore | None = None):
        self.dir = directory or data_dir()
        self.path = self.dir / "settings.json"
        self.secrets = secrets or SecretStore(self.dir)
        self.value = self._load()

    def proxy_password(self) -> str:
        return self.secrets.get()

    def export(self) -> dict:
        data = self.value.public()
        data["proxy_password_set"] = bool(self.secrets.get())
        data["proxy_password_note"] = self.secrets.note
        data["proxy_password_persist"] = self.secrets.can_persist
        data["proxy_type"] = _default_scheme(None, data.get("proxy") or "")
        data["ai_configs"] = self.list_ai_configs()
        data["ai_presets"] = PRESETS
        data["secrets_note"] = self.secrets.note
        data["secrets_persist"] = self.secrets.can_persist
        return data

    def _load(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                leaked = [k for k in ("proxy_password", "password", "proxy_pass", "api_key") if k in raw]
                for k in list(raw):
                    if k in _EPHEMERAL or k in ("proxy_password", "password", "proxy_pass"):
                        raw.pop(k, None)
                settings = _validated(raw)
                if leaked:
                    # Если пароль когда-то попал в JSON — сразу перезаписываем файл без него.
                    self.value = settings
                    self._save()
                    log.warning("Из settings.json убран ключ, которого там быть не должно")
                return settings
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("Файл настроек повреждён, беру значения по умолчанию")
        return Settings()

    def update(self, patch: dict) -> Settings:
        if not isinstance(patch, dict):
            raise ValueError("Ожидался объект настроек")
        patch = dict(patch)
        password = patch.pop("proxy_password", _MISSING)
        clear = bool(patch.pop("clear_proxy_password", False))
        proxy_type = patch.pop("proxy_type", _MISSING)
        for key in _EPHEMERAL:
            patch.pop(key, None)

        merged = self.value.public()
        for key in merged:
            if key in patch:
                merged[key] = patch[key]

        if "proxy" in patch or proxy_type is not _MISSING:
            scheme = _default_scheme(None if proxy_type is _MISSING else proxy_type, merged.get("proxy") or "")
            merged["proxy"] = validate.clean_proxy(merged.get("proxy") or "", default_scheme=scheme)

        # Ошибки значения не глотаем: пользователь должен увидеть, что не так
        self.value = _validated(merged, strict=True)

        if clear:
            self.secrets.delete()
        elif password is not _MISSING:
            pwd = validate.clean_proxy_password(password)
            if pwd:
                self.secrets.set(pwd)

        self._save()
        return self.value


    def list_ai_configs(self) -> list:
        return [public_config(c, self.secrets.has(ai_secret_name(c["id"]))) for c in self.value.ai_configs]

    def save_ai_config(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("Ожидался объект конфигурации")
        raw = dict(raw)
        api_key = raw.pop("api_key", None)
        clear_key = bool(raw.pop("clear_api_key", False))
        existing_id = raw.get("id") or None
        existing = None
        if existing_id:
            from .ai.config import clean_id
            existing_id = clean_id(existing_id)
            existing = next((c for c in self.value.ai_configs if c["id"] == existing_id), None)
            if existing is None:
                raise ValueError("Конфигурация не найдена")
        cfg = clean_config(raw, existing_id=existing_id)
        if cfg["active"]:
            for other in self.value.ai_configs:
                other["active"] = False
        configs = [c for c in self.value.ai_configs if c["id"] != cfg["id"]]
        configs.append(cfg)
        self.value.ai_configs = _clean_ai_configs(configs)

        key_name = ai_secret_name(cfg["id"])
        if clear_key:
            self.secrets.delete(key_name)
        elif api_key:
            from .ai.config import clean_api_key
            key = clean_api_key(api_key)
            if key:
                self.secrets.set(key, name=key_name)
        self._save()
        return public_config(cfg, self.secrets.has(key_name))

    def delete_ai_config(self, cfg_id: str) -> None:
        from .ai.config import clean_id
        cfg_id = clean_id(cfg_id)
        before = len(self.value.ai_configs)
        self.value.ai_configs = [c for c in self.value.ai_configs if c["id"] != cfg_id]
        if len(self.value.ai_configs) == before:
            raise ValueError("Конфигурация не найдена")
        self.secrets.delete(ai_secret_name(cfg_id))
        self._save()

    def ai_api_key(self, cfg_id: str) -> str:
        return self.secrets.get(ai_secret_name(cfg_id))

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = self.value.public()
        # Ещё раз убеждаемся, что пароль не уезжает на диск открытым текстом
        for key in ("proxy_password", "password", "proxy_pass", "api_key", "key", "token"):
            payload.pop(key, None)
        for cfg in payload.get("ai_configs") or []:
            if isinstance(cfg, dict):
                for key in ("api_key", "key", "token"):
                    cfg.pop(key, None)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
