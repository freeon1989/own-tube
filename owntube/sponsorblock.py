# -*- coding: utf-8 -*-
"""SponsorBlock: отрезки рекламы внутри роликов YouTube.

Запрос анонимный и с «префиксом хеша»: серверу уходят только первые 4 символа SHA-256 от ID,
так что он не знает, какое именно видео вы смотрите. Нужные отрезки выбираем локально.
"""
from __future__ import annotations

import hashlib
import json
import logging
from urllib.parse import quote

from . import validate, ytdl
from .network import Route

log = logging.getLogger("owntube.sponsorblock")
API = "https://sponsor.ajay.app/api/skipSegments/"


def segments(video_id: str, categories: list[str], route: Route) -> list[tuple[float, float]]:
    if not validate.is_video_id(video_id) or not categories:
        return []
    cats = [c for c in categories if c in validate.SB_CATEGORIES]
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    url = API + prefix + "?categories=" + quote(json.dumps(cats))
    try:
        data = ytdl.fetch_json(url, route)
    except Exception as exc:  # 404 — для этого префикса ничего нет; сеть — просто играем без пропусков
        log.info("SponsorBlock: %s", exc)
        return []
    return pick(data, video_id)


def pick(data, video_id: str) -> list[tuple[float, float]]:
    out = []
    if not isinstance(data, list):
        return out
    for v in data:
        if not isinstance(v, dict) or v.get("videoID") != video_id:
            continue
        for s in v.get("segments") or []:
            if not isinstance(s, dict) or s.get("actionType", "skip") != "skip":
                continue
            seg = s.get("segment")
            if (isinstance(seg, list) and len(seg) == 2 and all(isinstance(x, (int, float)) for x in seg)
                    and 0 <= seg[0] < seg[1] < 24 * 3600):
                out.append((round(float(seg[0]), 2), round(float(seg[1]), 2)))
    out.sort()
    return out[:100]


def script_opt(segs: list[tuple[float, float]]) -> str:
    """Отрезки для нашего Lua-скрипта в mpv: «10.5-20,40-55.2»."""
    return ",".join(f"{a:g}-{b:g}" for a, b in segs)
