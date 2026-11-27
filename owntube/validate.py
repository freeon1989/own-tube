# -*- coding: utf-8 -*-
"""Проверка всего, что приходит из интерфейса или из сети, до использования."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
VK_ID_RE = re.compile(r"^-?\d{1,12}_\d{1,12}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SOURCES = ("yt", "vk")
QUALITIES = ("best", "1080", "720")
CODECS = ("auto", "h264")
THEMES = ("assistant", "violet", "light", "neon", "sakura", "minimal")
SB_CATEGORIES = ("sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic", "filler")
MAX_QUERY_LEN = 200
PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")

# Хосты, с которых разрешено брать поток (CDN YouTube и VK)
STREAM_HOSTS = {
    "yt": (".googlevideo.com", ".youtube.com"),
    "vk": (".okcdn.ru", ".vkuser.net", ".vk.com", ".vkvideo.ru", ".userapi.com", ".mycdn.me", ".vk-cdn.net"),
}
# Хосты картинок (совпадает с CSP в index.html)
THUMB_HOSTS = ("i.ytimg.com",) + tuple(h.lstrip(".") for h in (".userapi.com", ".okcdn.ru", ".mycdn.me", ".vkuser.net"))
VK_PAGE_HOSTS = ("vk.com", "vkvideo.ru", "m.vk.com", "m.vkvideo.ru", "www.vk.com", "www.vkvideo.ru", "vk.ru", "www.vk.ru")


def is_video_id(value: object) -> bool:
    return isinstance(value, str) and VIDEO_ID_RE.fullmatch(value) is not None


def is_vk_id(value: object) -> bool:
    return isinstance(value, str) and VK_ID_RE.fullmatch(value) is not None


def is_item_id(source: object, value: object) -> bool:
    if source == "yt":
        return is_video_id(value)
    if source == "vk":
        return is_vk_id(value)
    return False


def clean_source(value: object) -> str:
    if value not in SOURCES:
        raise ValueError("Неизвестный источник")
    return str(value)


def is_channel_id(value: object) -> bool:
    return isinstance(value, str) and CHANNEL_ID_RE.fullmatch(value) is not None


def clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


def clean_query(value: object) -> str:
    """Строка поиска: только текст, без управляющих символов, ограниченной длины."""
    if not isinstance(value, str):
        raise ValueError("Запрос должен быть строкой")
    text = clean_text(value, MAX_QUERY_LEN)
    if not text:
        raise ValueError("Введите, что искать")
    return text


def clean_choice(value: object, allowed: tuple, what: str) -> str:
    if value not in allowed:
        raise ValueError(f"Недопустимое значение: {what}")
    return str(value)


def clean_quality(value: object) -> str:
    return clean_choice(value, QUALITIES, "качество")


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


def _https_host(value: object) -> str:
    if not isinstance(value, str) or len(value) > 16384 or any(ord(ch) < 33 for ch in value):
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.username or parts.password:
        return ""
    return (parts.hostname or "").lower()


def host_matches(host: str, suffixes) -> bool:
    return any(host == s.lstrip(".") or host.endswith("." + s.lstrip(".")) for s in suffixes)


def is_stream_url(value: object, source: str = "yt") -> bool:
    """Поток можно открывать только по https и только с серверов источника."""
    host = _https_host(value)
    return bool(host) and host_matches(host, STREAM_HOSTS.get(source, ()))


def is_thumb_url(value: object) -> bool:
    host = _https_host(value)
    return bool(host) and host_matches(host, THUMB_HOSTS)


def watch_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("Некорректный идентификатор видео")
    return f"https://www.youtube.com/watch?v={video_id}"


def vk_url(vk_id: str) -> str:
    if not is_vk_id(vk_id):
        raise ValueError("Некорректный идентификатор видео VK")
    return f"https://vkvideo.ru/video{vk_id}"


def item_url(source: str, item_id: str) -> str:
    return watch_url(item_id) if source == "yt" else vk_url(item_id)


def thumb_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("Некорректный идентификатор видео")
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def parse_vk_link(value: object) -> str | None:
    """Достаёт ID видео VK из ссылки vk.com / vkvideo.ru (video-1_2, ?z=video-1_2, clip-1_2)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "://" not in text:
        text = "https://" + text
    host = _https_host(text)
    if not host or host not in VK_PAGE_HOSTS:
        return None
    parts = urlsplit(text)
    candidates = [parts.path] + parse_qs(parts.query).get("z", [])
    for c in candidates:
        m = re.search(r"(?:video|clip)(-?\d{1,12}_\d{1,12})", c)
        if m:
            return m.group(1)
    return None


def parse_youtube_link(value: object) -> str | None:
    host = _https_host(value)
    if not host:
        return None
    parts = urlsplit(value)
    if host in ("youtu.be",):
        vid = parts.path.strip("/").split("/")[0]
    elif host_matches(host, ("youtube.com",)):
        if parts.path == "/watch":
            vid = (parse_qs(parts.query).get("v") or [""])[0]
        elif parts.path.startswith(("/shorts/", "/live/", "/embed/")):
            vid = parts.path.split("/")[2]
        else:
            return None
    else:
        return None
    return vid if is_video_id(vid) else None
