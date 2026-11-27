# -*- coding: utf-8 -*-
"""Быстрые списки YouTube: тот же внутренний JSON-API, что использует сайт (youtubei/v1).

Один запрос по пулу keep-alive соединений вместо запуска yt-dlp: поиск открывается за доли секунды,
а следующая страница берётся по токену продолжения («Показать ещё», бесконечная прокрутка).
Если ответ изменился или запрос не удался, вызывающий код переходит на yt-dlp."""
from __future__ import annotations

import gzip
import json
import logging
import re

from . import httpc, validate
from .network import Route

log = logging.getLogger("owntube.innertube")
BASE = "https://www.youtube.com/youtubei/v1/"
CLIENT = {"clientName": "WEB", "clientVersion": "2.20251120.00.00", "hl": "ru"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
VIDEOS_TAB = "EgZ2aWRlb3PyBgQKAjoA"  # вкладка «Видео» канала
SHORTS_PREFIX = "shorts-shelf-item-"
_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class InnertubeError(RuntimeError):
    pass


def _walk(o, key: str, out: list) -> list:
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                out.append(v)
            else:
                _walk(v, key, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, out)
    return out


def _text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    if isinstance(node.get("content"), str):
        return node["content"]
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


def _hms(text: str) -> str:
    return text if re.fullmatch(r"\d{1,2}(:\d{2}){1,2}", text or "") else ""


def _item(vid, title, channel="", channel_id="", duration="", views="", live=False, short=False, age="") -> dict | None:
    if not validate.is_video_id(vid):
        return None
    return {"source": "yt", "id": vid, "title": validate.clean_text(title, 300) or "Без названия",
            "channel": validate.clean_text(channel, 120),
            "channel_id": channel_id if validate.is_channel_id(channel_id) else "",
            "duration": "" if live else duration, "views": validate.clean_text(views, 60),
            "live": bool(live), "thumb": validate.thumb_url(vid), "short": bool(short),
            "age": validate.clean_text(age, 40)}


def _video_renderer(v: dict) -> dict | None:
    owner = (v.get("ownerText") or v.get("longBylineText") or {}).get("runs") or [{}]
    cid = (((owner[0].get("navigationEndpoint") or {}).get("browseEndpoint")) or {}).get("browseId", "")
    live = any((b.get("metadataBadgeRenderer") or {}).get("style") == "BADGE_STYLE_TYPE_LIVE_NOW"
               for b in v.get("badges") or []) or any(
        (o.get("thumbnailOverlayTimeStatusRenderer") or {}).get("style") == "LIVE" for o in v.get("thumbnailOverlays") or [])
    return _item(v.get("videoId"), _text(v.get("title")), _text(v.get("ownerText") or v.get("longBylineText")), cid,
                 _hms(_text(v.get("lengthText"))), _text(v.get("viewCountText")), live,
                 age=_text(v.get("publishedTimeText")))


def _shorts_lockup(v: dict) -> dict | None:
    vid = (((v.get("onTap") or {}).get("innertubeCommand") or {}).get("reelWatchEndpoint") or {}).get("videoId")
    if not vid:
        ent = v.get("entityId") or ""
        vid = ent[len(SHORTS_PREFIX):] if ent.startswith(SHORTS_PREFIX) else ""
    meta = v.get("overlayMetadata") or {}
    return _item(vid, _text(meta.get("primaryText")), views=_text(meta.get("secondaryText")), short=True, duration="")


def _lockup(v: dict, channel: str, channel_id: str) -> dict | None:
    if v.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None
    meta = (v.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    parts = []
    for row in _walk(meta.get("metadata"), "metadataParts", []):
        parts.extend(_text(p.get("text")) for p in row if isinstance(p, dict))
    dur = ""
    for b in _walk(v.get("contentImage"), "thumbnailBadgeViewModel", []):
        d = _hms(b.get("text", ""))
        if d:
            dur = d
            break
    live = any("LIVE" in str(b.get("badgeStyle", "")) for b in _walk(v.get("contentImage"), "thumbnailBadgeViewModel", []))
    views = next((p for p in parts if "просмотр" in p or "watching" in p or "смотр" in p), "")
    age = next((p for p in parts if "назад" in p or "ago" in p), "")
    return _item(v.get("contentId"), _text(meta.get("title")), channel, channel_id, dur, views, live, age=age)


def parse(data: dict, shorts: bool = False) -> tuple[list[dict], str]:
    """(видео, токен продолжения). Короткие ролики отдаются только когда их просили (shorts=True)."""
    chan = ""
    cid = ""
    meta = _walk(data, "channelMetadataRenderer", [])
    if meta:
        chan, cid = meta[0].get("title", ""), meta[0].get("externalId", "")
    items: list[dict] = []
    seen: set[str] = set()

    def add(it):
        if it and it["id"] not in seen:
            seen.add(it["id"])
            items.append(it)

    # порядок обхода важен: берём элементы в порядке выдачи
    def visit(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "videoRenderer" and isinstance(v, dict):
                    add(_video_renderer(v))
                elif k == "shortsLockupViewModel" and isinstance(v, dict):
                    if shorts:
                        add(_shorts_lockup(v))
                elif k == "lockupViewModel" and isinstance(v, dict):
                    add(_lockup(v, chan, cid))
                else:
                    visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(data)
    token = ""
    for node in _walk(data, "continuationItemRenderer", []):
        t = (((node.get("continuationEndpoint") or {}).get("continuationCommand")) or {}).get("token")
        if isinstance(t, str) and t:
            token = t
    if shorts:
        items = [i for i in items if i["short"]]
    else:
        items = [i for i in items if not i["short"]]
    return items, token


def _post(endpoint: str, body: dict, route: Route, timeout: float = 20) -> dict:
    payload = json.dumps({**body, "context": {"client": CLIENT}}).encode()
    status, headers, data = httpc.fetch(
        "POST", BASE + endpoint + "?prettyPrint=false", route,
        {"Content-Type": "application/json", "User-Agent": UA, "Accept-Language": "ru",
         "Accept-Encoding": "gzip", "Origin": "https://www.youtube.com"},
        payload, limit=12 * 1024 * 1024, timeout=timeout)
    if status != 200:
        raise InnertubeError(f"YouTube ответил HTTP {status}")
    if headers.get("content-encoding") == "gzip":
        data = gzip.decompress(data)
    try:
        return json.loads(data)
    except ValueError as exc:
        raise InnertubeError("Ответ YouTube не удалось разобрать") from exc


def search(query: str, route: Route, sp: str = "", token: str = "", shorts: bool = False) -> tuple[list[dict], str]:
    body = {"continuation": token} if token else {"query": query}
    if sp and not token:
        body["params"] = sp.replace("%3D", "=")
    items, nxt = parse(_post("search", body, route), shorts=shorts)
    return items, nxt


def channel(channel_id: str, route: Route, token: str = "") -> tuple[list[dict], str]:
    if not validate.is_channel_id(channel_id):
        raise ValueError("Некорректный канал")
    body = {"continuation": token} if token else {"browseId": channel_id, "params": VIDEOS_TAB}
    return parse(_post("browse", body, route))
