# -*- coding: utf-8 -*-
"""Конфигурации ИИ-моделей: поля формы, проверка URL и значений. Ключ здесь не хранится."""
from __future__ import annotations

import ipaddress
import re
import secrets
from urllib.parse import urlsplit

from .. import validate

# Публичные адреса API (не секреты). Свой сервер — пустой URL, пользователь впишет сам.
PRESETS = (
    ("openai", "OpenAI", "https://api.openai.com/v1"),
    ("anthropic", "Anthropic Claude", "https://api.anthropic.com/v1"),
    ("xai", "xAI Grok", "https://api.x.ai/v1"),
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1"),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
    ("custom", "Свой сервер (OpenAI-совместимый)", ""),
)

DEFAULT_CONTEXT = 130000
DEFAULT_THREADS = 1
MAX_NAME = 80
MAX_MODEL = 200
MAX_URL = 400
MAX_KEY = 4096
LOCAL_HOSTS = ("localhost",)
LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home", ".intranet")
AI_KEY_PREFIX = "ai-key-"


def new_id() -> str:
    return secrets.token_hex(8)


def ai_secret_name(config_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{16}", config_id or ""):
        raise ValueError("Некорректный идентификатор конфигурации")
    return AI_KEY_PREFIX + config_id


def _is_private_host(host: str) -> bool:
    name = (host or "").lower().rstrip(".")
    if name in LOCAL_HOSTS:
        return True
    if any(name.endswith(suf) for suf in LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def clean_base_url(value: object) -> str:
    """https — любой хост; http — только частные/локальные адреса (LAN, VPN, localhost)."""
    if not isinstance(value, str):
        raise ValueError("URL сервера должен быть строкой")
    text = value.strip()
    if not text:
        raise ValueError("Укажите URL сервера, например https://api.openai.com/v1")
    if len(text) > MAX_URL or any(ch.isspace() or not ch.isprintable() for ch in text):
        raise ValueError("Некорректный URL сервера")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("URL сервера: схема http или https")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ValueError("Ключ впишите в отдельное поле, не в URL")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ValueError("В URL сервера нет адреса")
    if parts.scheme == "http" and not _is_private_host(host):
        raise ValueError("Для внешних серверов нужен https. http разрешён только для локальных и частных адресов")
    if parts.query or parts.fragment:
        raise ValueError("В URL сервера не должно быть параметров и якоря")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Некорректный порт в URL сервера") from exc
    host_out = f"[{host}]" if ":" in host else host
    netloc = f"{host_out}:{port}" if port else host_out
    path = parts.path or ""
    if path == "/":
        path = ""
    else:
        path = path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def clean_config_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Название конфигурации должно быть строкой")
    text = " ".join(value.split())
    if not text:
        raise ValueError("Укажите название конфигурации")
    if len(text) > MAX_NAME or any(ord(ch) < 32 for ch in text):
        raise ValueError("Некорректное название конфигурации")
    return text


def clean_model_name(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Название модели должно быть строкой")
    text = value.strip()
    if not text:
        return ""
    if len(text) > MAX_MODEL or any(ord(ch) < 32 for ch in text):
        raise ValueError("Некорректное название модели")
    return text


def clean_api_key(value: object) -> str:
    """Ключ не подрезаем по краям и не включаем в текст ошибки."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("API-ключ должен быть строкой")
    if len(value) > MAX_KEY or "\x00" in value:
        raise ValueError("Некорректный API-ключ")
    return value


def clean_threads(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_THREADS
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Число потоков должно быть целым числом") from exc
    if value < 1 or value > 64:
        raise ValueError("Число потоков: от 1 до 64")
    return value


def clean_context(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_CONTEXT
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Объём данных для анализа должен быть целым числом") from exc
    if value < 1024 or value > 2_000_000:
        raise ValueError("Объём данных для анализа: от 1024 до 2000000 токенов")
    return value


def clean_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{16}", value):
        raise ValueError("Некорректный идентификатор конфигурации")
    return value


def clean_config(raw: dict, *, existing_id: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Ожидался объект конфигурации")
    cfg_id = existing_id or raw.get("id") or new_id()
    if existing_id:
        cfg_id = clean_id(existing_id)
    elif raw.get("id"):
        cfg_id = clean_id(raw["id"])
    else:
        cfg_id = new_id()
    vision = raw.get("vision", False)
    active = raw.get("active", False)
    if not isinstance(vision, bool) or not isinstance(active, bool):
        raise ValueError("Флаги должны быть да/нет")
    return {
        "id": cfg_id,
        "name": clean_config_name(raw.get("name")),
        "base_url": clean_base_url(raw.get("base_url")),
        "model": clean_model_name(raw.get("model")),
        "vision": vision,
        "threads": clean_threads(raw.get("threads", DEFAULT_THREADS)),
        "context": clean_context(raw.get("context", DEFAULT_CONTEXT)),
        "active": active,
    }


def public_config(cfg: dict, key_set: bool) -> dict:
    return {
        "id": cfg["id"],
        "name": cfg["name"],
        "base_url": cfg["base_url"],
        "model": cfg.get("model") or "",
        "vision": bool(cfg.get("vision")),
        "threads": int(cfg.get("threads") or DEFAULT_THREADS),
        "context": int(cfg.get("context") or DEFAULT_CONTEXT),
        "active": bool(cfg.get("active")),
        "api_key_set": bool(key_set),
    }


PublicConfig = dict
