# -*- coding: utf-8 -*-
"""Проверка всего, что приходит из интерфейса или из сети, до использования."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
QUALITIES = ("best", "1080", "720")
MAX_QUERY_LEN = 200
PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")
# Хосты, с которых разрешено брать поток (YouTube CDN и манифесты)
STREAM_HOST_SUFFIXES = (".googlevideo.com", ".youtube.com")


def is_video_id(value: object) -> bool:
    return isinstance(value, str) and VIDEO_ID_RE.fullmatch(value) is not None


def clean_query(value: object) -> str:
    """Строка поиска: только текст, без управляющих символов, ограниченной длины."""
    if not isinstance(value, str):
        raise ValueError("Запрос должен быть строкой")
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    if not text:
        raise ValueError("Введите, что искать")
    return text[:MAX_QUERY_LEN]


def clean_quality(value: object) -> str:
    if value not in QUALITIES:
        raise ValueError("Неизвестное качество")
    return str(value)


def clean_proxy(value: object) -> str:
    """Адрес прокси вида scheme://host:port. Логин и пароль не принимаем:
    секреты не должны лежать в файле настроек. Пустая строка — без прокси."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Прокси должен быть строкой")
    text = value.strip()
    if not text:
        return ""
    if len(text) > 200 or any(ch.isspace() or not ch.isprintable() for ch in text):
        raise ValueError("Некорректный адрес прокси")
    parts = urlsplit(text)
    if parts.scheme.lower() not in PROXY_SCHEMES:
        raise ValueError("Прокси: схема http, https, socks4, socks5 или socks5h")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("Логин и пароль в адресе прокси не поддерживаются")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or port is None:
        raise ValueError("Укажите адрес и порт, например socks5://127.0.0.1:1080")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("В адресе прокси должны быть только схема, адрес и порт")
    return f"{parts.scheme.lower()}://{parts.netloc}"


def is_http_proxy(proxy: str) -> bool:
    return proxy.lower().startswith(("http://", "https://"))


def is_stream_url(value: object) -> bool:
    """Поток можно открывать только по https и только с серверов YouTube."""
    if not isinstance(value, str) or len(value) > 16384:
        return False
    if any(ord(ch) < 33 for ch in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.endswith(STREAM_HOST_SUFFIXES)


def watch_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("Некорректный идентификатор видео")
    return f"https://www.youtube.com/watch?v={video_id}"


def thumb_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("Некорректный идентификатор видео")
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
