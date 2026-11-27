# -*- coding: utf-8 -*-
"""YouTube: поиск, разделы «Обзор», Shorts и лента подписок. Через yt-dlp и RSS, без входа в аккаунт."""
from __future__ import annotations

import re
import logging
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

from . import validate, ytdl
from .network import Route

log = logging.getLogger("owntube.search")
DEFAULT_LIMIT = 24

# Фильтры страницы поиска YouTube (параметр sp)
SP_WEEK = "EgIIAw%3D%3D"       # загружено за неделю
SP_LIVE = "EgJAAQ%3D%3D"       # трансляции сейчас
SP_SHORT = "EgIYAQ%3D%3D"      # короче 4 минут

# Разделы «Обзор»: (заголовок, запрос, фильтр)
CATEGORIES = {
    "music": ("Музыка", "музыка новинки", SP_WEEK),
    "games": ("Игры", "игры обзор", SP_WEEK),
    "news": ("Новости", "новости", SP_WEEK),
    "sport": ("Спорт", "спорт лучшие моменты", SP_WEEK),
    "live": ("Трансляции", "прямой эфир", SP_LIVE),
    "fashion": ("Мода и красота", "мода и красота", SP_WEEK),
    "learn": ("Обучение", "обучение урок", SP_WEEK),
}
HOME_FALLBACK = ("популярное", SP_WEEK)


def format_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_views(n) -> str:
    if not isinstance(n, (int, float)) or n < 0:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".0 ", " ").replace(".", ",") + " просмотров"
    if n >= 1_000:
        return f"{n // 1_000} тыс. просмотров"
    return f"{n} просмотров"


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
        ch = e.get("channel_id")
        out.append({
            "source": "yt",
            "id": vid,
            "title": validate.clean_text(e.get("title"), 300) or "Без названия",
            "channel": validate.clean_text(e.get("channel") or e.get("uploader"), 120),
            "channel_id": ch if validate.is_channel_id(ch) else "",
            "duration": "" if live else format_duration(e.get("duration")),
            "views": format_views(e.get("view_count")),
            "live": live,
            "thumb": validate.thumb_url(vid),
        })
    return out


def _flat(url: str, route: Route, limit: int) -> list[dict]:

    opts = ytdl.base_options(route, extract_flat=True, playlistend=limit, noplaylist=False)
    with ytdl.YDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return normalize((info or {}).get("entries"))


def search(query: str, route: Route = Route(), limit: int = DEFAULT_LIMIT, sp: str = "") -> list[dict]:
    text = validate.clean_query(query)
    limit = max(1, min(int(limit), 120))
    if sp:
        return _flat(f"https://www.youtube.com/results?search_query={quote(text)}&sp={sp}", route, limit)
    return _flat(f"ytsearch{limit}:{text}", route, limit)


def category(key: str, route: Route, limit: int = DEFAULT_LIMIT) -> list[dict]:
    if key not in CATEGORIES:
        raise ValueError("Неизвестный раздел")
    _, query, sp = CATEGORIES[key]
    return search(query, route, limit, sp)


def shorts(route: Route, limit: int = DEFAULT_LIMIT) -> list[dict]:
    items = search("#shorts", route, limit, SP_SHORT)
    return [i for i in items if not i["live"]]


def channel_videos(channel_id: str, route: Route, limit: int = DEFAULT_LIMIT) -> list[dict]:
    if not validate.is_channel_id(channel_id):
        raise ValueError("Некорректный канал")
    return _flat(f"https://www.youtube.com/channel/{channel_id}/videos", route, limit)


# --- лента подписок через RSS (лёгкий запрос на канал) ---
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


def parse_rss(data: bytes) -> list[dict]:
    if b"<!DOCTYPE" in data[:2048] or b"<!ENTITY" in data:
        raise ValueError("Неожиданный ответ RSS")
    root = ET.fromstring(data)
    out = []
    channel = validate.clean_text(root.findtext("a:title", "", NS), 120)
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        if not validate.is_video_id(vid):
            continue
        cid = e.findtext("yt:channelId", "", NS)
        if cid and not cid.startswith("UC"):
            cid = "UC" + cid
        published = 0.0
        try:
            published = datetime.fromisoformat(e.findtext("a:published", "", NS)).timestamp()
        except ValueError:
            pass
        views = None
        stats = e.find("media:group/media:community/media:statistics", NS)
        if stats is not None and (stats.get("views") or "").isdigit():
            views = int(stats.get("views"))
        out.append({
            "source": "yt", "id": vid,
            "title": validate.clean_text(e.findtext("a:title", "", NS), 300) or "Без названия",
            "channel": validate.clean_text(e.findtext("a:author/a:name", "", NS), 120) or channel,
            "channel_id": cid if validate.is_channel_id(cid) else "",
            "duration": "", "views": format_views(views), "live": False,
            "thumb": validate.thumb_url(vid), "published": published,
        })
    return out


_rss_cache: dict[str, tuple[float, list[dict]]] = {}
RSS_TTL = 15 * 60


def channel_rss(channel_id: str, route: Route) -> list[dict]:
    if not validate.is_channel_id(channel_id):
        raise ValueError("Некорректный канал")
    hit = _rss_cache.get(channel_id)
    if hit and time.time() - hit[0] < RSS_TTL:
        return hit[1]
    data = ytdl.fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", route, "application/atom+xml")
    items = parse_rss(data)
    _rss_cache[channel_id] = (time.time(), items)
    return items


def subscriptions_feed(channels, route: Route, per_channel: int = 4, limit: int = 60) -> list[dict]:
    """channels: список ID или словарь {ID: название}. Сначала RSS, при ошибке — вкладка «Видео» канала."""
    names = channels if isinstance(channels, dict) else {c: "" for c in channels}
    ids = [c for c in names if validate.is_channel_id(c)][:120]
    items: list[dict] = []

    def one(cid):
        try:
            return channel_rss(cid, route)[:per_channel]
        except Exception as exc:
            log.info("RSS %s: %s", cid, exc)
        try:
            got = channel_videos(cid, route, per_channel)
        except Exception as exc:
            log.info("Канал %s: %s", cid, exc)
            return []
        for i in got:
            i["channel"] = i["channel"] or validate.clean_text(names.get(cid), 120)
            i["channel_id"] = i["channel_id"] or cid
        return got

    with ThreadPoolExecutor(max_workers=8) as pool:
        for chunk in pool.map(one, ids):
            items.extend(chunk)
    items.sort(key=lambda i: i.get("published", 0), reverse=True)
    return items[:limit]


# ---------- ленты аккаунта Google (нужны cookies входа) ----------
ACCOUNT_FEEDS = {
    "subs": "https://www.youtube.com/feed/subscriptions",
    "history": "https://www.youtube.com/feed/history",
    "later": "https://www.youtube.com/playlist?list=WL",
    "liked": "https://www.youtube.com/playlist?list=LL",
    "recommended": "https://www.youtube.com/",
}
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
RELOGIN = "Войдите заново: сессия Google истекла или YouTube её не принял (Настройки → Аккаунты → Google → «Войти»)."
_AUTH_HINTS = re.compile(r"log(ged)?[ -]?in|sign[ -]?in|cookies|authenticat|account|does not exist|private|не вошли|confirm you", re.I)


class AccountError(ValueError):
    """Лента аккаунта недоступна; текст можно показать пользователю (и он подсказывает, что делать)."""

    def __init__(self, message: str, relogin: bool = False):
        super().__init__(message)
        self.relogin = relogin


def account_items(kind: str, route: Route, limit: int, arg=None) -> list[dict]:
    """Список из аккаунта Google: kind — recommended | subs | history | later | liked | playlist (arg — id плейлиста).
    Ошибки входа превращаются в AccountError с понятным текстом; cookies в сообщения не попадают."""
    import yt_dlp

    if kind == "playlist":
        if not isinstance(arg, str) or not PLAYLIST_ID_RE.fullmatch(arg):
            raise ValueError("Некорректный плейлист")
        url = f"https://www.youtube.com/playlist?list={arg}"
    elif kind in ACCOUNT_FEEDS:
        url = ACCOUNT_FEEDS[kind]
    else:
        raise ValueError("Неизвестная лента аккаунта")
    try:
        items = _flat(url, route, max(1, min(int(limit), 300)))
    except yt_dlp.utils.DownloadError as exc:
        text = validate.redact(str(exc))
        if _AUTH_HINTS.search(text):
            raise AccountError(RELOGIN, True) from exc
        raise AccountError("Лента аккаунта не загрузилась: " + text.replace("ERROR: ", "")[:200]) from exc
    return items


def account_feed(kind: str, arg, route: Route, limit: int = 60) -> dict:
    if kind in ACCOUNT_FEEDS or kind == "playlist":
        return {"items": account_items(kind, route, 200 if kind == "playlist" else limit, arg)}
    if kind == "playlists":
        import yt_dlp

        opts = ytdl.base_options(route, extract_flat=True, playlistend=100, noplaylist=False)
        try:
            with ytdl.YDL(opts) as ydl:
                info = ydl.extract_info("https://www.youtube.com/feed/playlists", download=False)
        except yt_dlp.utils.DownloadError as exc:
            text = validate.redact(str(exc))
            if _AUTH_HINTS.search(text):
                raise AccountError(RELOGIN, True) from exc
            raise AccountError("Плейлисты аккаунта не загрузились: " + text.replace("ERROR: ", "")[:200]) from exc
        out = []
        for e in (info or {}).get("entries") or []:
            if not isinstance(e, dict):
                continue
            pid = e.get("id")
            if isinstance(pid, str) and PLAYLIST_ID_RE.fullmatch(pid):
                thumbs = e.get("thumbnails") or []
                thumb = next((t.get("url") for t in reversed(thumbs) if isinstance(t, dict)
                              and validate.is_thumb_url(t.get("url"))), "")
                out.append({"id": pid, "title": validate.clean_text(e.get("title"), 200) or "Плейлист",
                            "count": e.get("playlist_count") or "", "thumb": thumb})
        return {"playlists": out}
    raise ValueError("Неизвестная лента аккаунта")
