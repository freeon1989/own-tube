# -*- coding: utf-8 -*-
"""VK Видео: открыть ролик по ссылке и получить данные для карточки.

Поиск по VK требует входа в VK (официальный API video.search) и появится на втором этапе.
"""
from __future__ import annotations

from . import validate, ytdl
from .network import Route
from .search import format_duration, format_views


def _best_thumb(info: dict) -> str:
    thumbs = info.get("thumbnails") or []
    for t in reversed(thumbs):
        url = t.get("url") if isinstance(t, dict) else None
        if validate.is_thumb_url(url):
            return url
    url = info.get("thumbnail")
    return url if validate.is_thumb_url(url) else ""


def item_from_info(vk_id: str, info: dict) -> dict:
    return {
        "source": "vk", "id": vk_id,
        "title": validate.clean_text(info.get("title"), 300) or "Без названия",
        "channel": validate.clean_text(info.get("uploader") or info.get("channel"), 120),
        "channel_id": "",
        "duration": format_duration(info.get("duration")),
        "views": format_views(info.get("view_count")),
        "live": info.get("live_status") == "is_live",
        "thumb": _best_thumb(info),
    }


def open_link(link: str, route: Route) -> dict:
    import yt_dlp

    vk_id = validate.parse_vk_link(link)
    if not vk_id:
        raise ValueError("Нужна ссылка на видео VK, например https://vkvideo.ru/video-123_456")
    with yt_dlp.YoutubeDL(ytdl.base_options(route, format="b")) as ydl:
        info = ydl.extract_info(validate.vk_url(vk_id), download=False)
    return item_from_info(vk_id, info or {})
