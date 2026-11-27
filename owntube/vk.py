# -*- coding: utf-8 -*-
"""VK Видео: открыть ролик по ссылке и получить данные для карточки.

Поиск — через API vkvideo.ru: гостевой доступ, после входа в VK — от имени пользователя (18+ только так).
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

    vk_id = validate.parse_vk_link(link)
    if not vk_id:
        raise ValueError("Нужна ссылка на видео VK, например https://vkvideo.ru/video-123_456")
    with ytdl.YDL(ytdl.base_options(route, format="b")) as ydl:
        info = ydl.extract_info(validate.vk_url(vk_id), download=False)
    return item_from_info(vk_id, info or {})


# ---------- поиск VK Видео ----------
import json as _json
import threading as _threading
import time as _time
from urllib.parse import urlencode as _urlencode

VK_CLIENT_ID = "52461373"   # публичный id веб-клиента vkvideo.ru
VK_APP_ID = "6287487"
VK_API = "https://api.vkvideo.ru/method/"
VK_V = "5.274"
_ORIGIN = {"Origin": "https://vkvideo.ru", "Referer": "https://vkvideo.ru/",
           "Content-Type": "application/x-www-form-urlencoded"}
_tok_lock = _threading.Lock()
_anon = {"token": "", "exp": 0.0}


def _post(url: str, form: dict, route: Route, cookie: str = "") -> dict:
    from . import httpc

    headers = dict(_ORIGIN)
    if cookie:
        headers["Cookie"] = cookie
    resp = httpc.open_url("POST", url, route, headers, _urlencode(form).encode(), timeout=20)
    try:
        raw = resp.read(4 * 1024 * 1024)
        status = resp.status
    finally:
        resp.close()
    if status != 200:
        raise ValueError(f"VK ответил кодом {status}")
    try:
        data = _json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ValueError("VK вернул не JSON") from exc
    return data if isinstance(data, dict) else {}


def anonymous_token(route: Route) -> str:
    with _tok_lock:
        if _anon["token"] and _anon["exp"] > _time.time() + 60:
            return _anon["token"]
    data = _post("https://login.vk.com/?act=get_anonym_token", {
        "client_id": VK_CLIENT_ID, "token_type": "messages", "app_id": VK_APP_ID,
        "client_secret": "", "version": "1",
        "scopes": "audio_anonymous,video_anonymous,photos_anonymous,profile_anonymous",
        "isApiOauthAnonymEnabled": "false"}, route)
    tok = ((data.get("data") or {}).get("access_token") or "") if data.get("type") == "okay" else ""
    if not tok:
        raise ValueError("VK не выдал гостевой доступ для поиска")
    exp = (data.get("data") or {}).get("expired_at") or (_time.time() + 3600)
    with _tok_lock:
        _anon.update(token=tok, exp=float(exp))
    return tok


def user_token(route: Route, cookie: str) -> str:
    """Токен вошедшего пользователя по cookies VK (как делает сам сайт vkvideo.ru)."""
    data = _post("https://login.vk.com/?act=web_token", {"version": "1", "app_id": VK_APP_ID,
                                                          "access_token": ""}, route, cookie)
    return ((data.get("data") or {}).get("access_token") or "") if data.get("type") == "okay" else ""


def _thumb(v: dict) -> str:
    best = ""
    for img in sorted(v.get("image") or [], key=lambda i: (i or {}).get("width") or 0):
        url = (img or {}).get("url")
        if validate.is_thumb_url(url):
            best = url
            if ((img or {}).get("width") or 0) >= 320:
                break
    return best


def parse_search(resp: dict) -> list[dict]:
    names = {}
    for g in resp.get("groups") or []:
        if isinstance(g, dict) and isinstance(g.get("id"), int):
            names[-g["id"]] = g.get("name") or ""
    for p in resp.get("profiles") or []:
        if isinstance(p, dict) and isinstance(p.get("id"), int):
            names[p["id"]] = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    out = []
    for v in resp.get("videos") or []:
        if not isinstance(v, dict) or not isinstance(v.get("owner_id"), int) or not isinstance(v.get("id"), int):
            continue
        vk_id = f"{v['owner_id']}_{v['id']}"
        if not validate.is_vk_id(vk_id):
            continue
        out.append({"source": "vk", "id": vk_id,
                    "title": validate.clean_text(v.get("title"), 300) or "Без названия",
                    "channel": validate.clean_text(names.get(v["owner_id"]), 120), "channel_id": "",
                    "duration": format_duration(v.get("duration")), "views": format_views(v.get("views")),
                    "live": bool(v.get("live")), "thumb": _thumb(v)})
    return out


def search(query: str, route: Route, cookie: str = "", adult: bool = False, count: int = 30) -> dict:
    """Поиск роликов VK. Без входа — гостевой доступ; 18+ — только после входа в VK."""
    q = validate.clean_query(query)
    token, signed = "", False
    if cookie:
        try:
            token = user_token(route, cookie)
            signed = bool(token)
        except Exception:
            token = ""
    if not token:
        token = anonymous_token(route)
    form = {"q": q, "access_token": token, "count": str(max(1, min(count, 50)))}
    if adult and signed:
        form["adult"] = "1"
    data = _post(VK_API + f"catalog.getVideoSearchWeb2?v={VK_V}&client_id={VK_CLIENT_ID}", form, route)
    if "error" in data:
        msg = (data.get("error") or {}).get("error_msg") if isinstance(data.get("error"), dict) else ""
        raise ValueError("VK: " + validate.clean_text(msg or "ошибка поиска", 200))
    return {"items": parse_search(data.get("response") or {}), "signed_in": signed,
            "adult": bool(adult and signed)}
