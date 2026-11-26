# -*- coding: utf-8 -*-
"""Поиск по YouTube через yt-dlp как библиотеку. Ничего не скачивается."""
from __future__ import annotations

from . import validate
from .network import Route

DEFAULT_LIMIT = 24


def _format_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _format_views(n) -> str:
    if not isinstance(n, (int, float)) or n < 0:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".0 ", " ").replace(".", ",") + " просмотров"
    if n >= 1_000:
        return f"{n // 1_000} тыс. просмотров"
    return f"{n} просмотров"


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def normalize(entries) -> list[dict]:
    """Приводит записи yt-dlp к безопасному виду. Адрес картинки строим сами из ID."""
    out, seen = [], set()
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        if not validate.is_video_id(vid) or vid in seen:
            continue
        seen.add(vid)
        live = e.get("live_status") == "is_live"
        out.append({
            "id": vid,
            "title": _text(e.get("title"), 300) or "Без названия",
            "channel": _text(e.get("channel") or e.get("uploader"), 120),
            "duration": "" if live else _format_duration(e.get("duration")),
            "views": _format_views(e.get("view_count")),
            "live": live,
            "thumb": validate.thumb_url(vid),
        })
    return out


def ydl_options(route: Route) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 2,
        "check_formats": False,
    }
    opts.update(route.ytdlp_options())
    return opts


def search(query: str, route: Route = Route(), limit: int = DEFAULT_LIMIT) -> list[dict]:
    import yt_dlp  # импорт здесь: модуль без сети грузится быстро и тестируется отдельно

    text = validate.clean_query(query)
    limit = max(1, min(int(limit), 50))
    with yt_dlp.YoutubeDL(ydl_options(route)) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{text}", download=False)
    return normalize((info or {}).get("entries"))
