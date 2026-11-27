# -*- coding: utf-8 -*-
"""VK Видео: открыть ролик по ссылке и получить данные для карточки.

Поиск — через API vkvideo.ru: гостевой доступ, после входа в VK — от имени пользователя (18+ только так).
"""
from __future__ import annotations

import logging

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

log = logging.getLogger("owntube.vk")
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
    status, _, raw = httpc.fetch("POST", url, route, headers, _urlencode(form).encode(), limit=4 * 1024 * 1024, timeout=20)
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
    out, seen = [], set()
    pool = list(resp.get("videos") or []) + [v for v in (resp.get("items") or []) if isinstance(v, dict) and "owner_id" in v]
    for c in resp.get("catalog_videos") or []:
        if isinstance(c, dict) and isinstance(c.get("video"), dict):
            pool.append(c["video"])
    for v in pool:
        if not isinstance(v, dict) or not isinstance(v.get("owner_id"), int) or not isinstance(v.get("id"), int):
            continue
        vk_id = f"{v['owner_id']}_{v['id']}"
        if not validate.is_vk_id(vk_id) or vk_id in seen:
            continue
        seen.add(vk_id)
        out.append({"source": "vk", "id": vk_id,
                    "title": validate.clean_text(v.get("title"), 300) or "Без названия",
                    "channel": validate.clean_text(names.get(v["owner_id"]), 120), "channel_id": "",
                    "duration": format_duration(v.get("duration")), "views": format_views(v.get("views")),
                    "live": bool(v.get("live")), "thumb": _thumb(v)})
    return out


def _token(route: Route, cookie: str) -> tuple[str, bool]:
    if cookie:
        try:
            tok = user_token(route, cookie)
            if tok:
                return tok, True
            log.info("VK: вход по cookies не дал токен пользователя, поиск идёт как у гостя")
        except Exception as exc:
            log.info("VK: токен пользователя не получен (%s), поиск идёт как у гостя", validate.redact(str(exc))[:100])
    return anonymous_token(route), False


RELOGIN = "Войдите заново в VK (Настройки → Аккаунты → VK → «Войти»): сессия истекла или VK её не принял."


def _check(data: dict) -> dict:
    if "error" in data:
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        if err.get("error_code") in (5, 7, 15, 17, 27, 28):  # авторизация / нет доступа / токен
            raise ValueError(RELOGIN)
        raise ValueError("VK: " + validate.clean_text(err.get("error_msg") or "ошибка запроса", 200))
    return data.get("response") or {}


def _cursor(resp: dict) -> dict | None:
    secs = ((resp.get("catalog") or {}).get("sections") or [{}])
    sec = secs[0] if secs and isinstance(secs[0], dict) else {}
    section = resp.get("section") if isinstance(resp.get("section"), dict) else sec
    nxt, sid = section.get("next_from"), section.get("id") or sec.get("id")
    if isinstance(nxt, str) and nxt and isinstance(sid, str) and sid:
        return {"k": "vk", "sec": sid[:600], "t": nxt[:2000]}
    return None


def search(query: str, route: Route, cookie: str = "", adult: bool = False, count: int = 30, cursor: dict | None = None) -> dict:
    """Поиск роликов VK. Без входа — гостевой доступ; 18+ — только после входа в VK.
    cursor — «ещё»: следующая страница раздела (catalog.getSection)."""
    token, signed = _token(route, cookie)
    if cursor and cursor["sec"] == MY_SECTION:
        return _my_videos(token, route, int(cursor["t"]) if str(cursor["t"]).isdigit() else 0, signed)
    if cursor:
        form = {"access_token": token, "section_id": cursor["sec"], "start_from": cursor["t"]}
        resp = _check(_post(VK_API + f"catalog.getSection?v={VK_V}&client_id={VK_CLIENT_ID}", form, route))
    else:
        q = validate.clean_query(query)
        form = {"q": q, "access_token": token, "count": str(max(1, min(count, 50)))}
        if adult and signed:
            form["adult"] = "1"
        resp = _check(_post(VK_API + f"catalog.getVideoSearchWeb2?v={VK_V}&client_id={VK_CLIENT_ID}", form, route))
    return {"items": parse_search(resp), "signed_in": signed, "adult": bool(adult and signed), "more": _cursor(resp)}


# ---------- ленты вошедшего пользователя: рекомендации, подписки, «Мои видео» ----------
MY_SECTION = "@my"
FEED_TITLES = {"recommended": ("рекомен", "для вас", "главн", "популяр"), "subs": ("подпис",)}


def _sections(resp: dict) -> list[dict]:
    cat = resp.get("catalog") if isinstance(resp.get("catalog"), dict) else {}
    return [x for x in (cat.get("sections") or []) if isinstance(x, dict) and isinstance(x.get("id"), str)]


def pick_section(sections: list[dict], kind: str) -> dict | None:
    """Раздел каталога vkvideo.ru по названию: «Рекомендации» / «Подписки». Нет подходящего — None
    (для рекомендаций берём первый раздел, для подписок честно говорим, что раздела нет)."""
    words = FEED_TITLES.get(kind, ())
    for sec in sections:
        title = str(sec.get("title") or "").lower()
        sid = str(sec.get("id") or "").lower()
        if any(w in title or w in sid for w in words):
            return sec
    return sections[0] if kind == "recommended" and sections else None


def _my_videos(token: str, route: Route, offset: int, signed: bool) -> dict:
    """«Мои видео»: video.get владельца-пользователя (users.get без id отдаёт текущего)."""
    me = _check(_post(VK_API + f"users.get?v={VK_V}&client_id={VK_CLIENT_ID}", {"access_token": token}, route))
    row = me[0] if isinstance(me, list) and me and isinstance(me[0], dict) else {}
    uid = row.get("id")
    if not isinstance(uid, int):
        raise ValueError(RELOGIN)
    count = 40
    resp = _check(_post(VK_API + f"video.get?v={VK_V}&client_id={VK_CLIENT_ID}",
                        {"access_token": token, "owner_id": str(uid), "count": str(count), "offset": str(offset),
                         "extended": "1"}, route))
    items = parse_search(resp)
    total = resp.get("count") if isinstance(resp.get("count"), int) else 0
    nxt = {"k": "vk", "sec": MY_SECTION, "t": str(offset + count)} if offset + count < total else None
    return {"items": items, "signed_in": signed, "adult": False, "more": nxt}


def feed(kind: str, route: Route, cookie: str, adult: bool = False) -> dict:
    """Первая страница ленты вошедшего пользователя. Без входа — ошибка с подсказкой, а не пустой список."""
    if kind not in ("recommended", "subs", "my"):
        raise ValueError("Неизвестная лента VK")
    token, signed = _token(route, cookie)
    if not signed:
        raise ValueError(RELOGIN if cookie else "Сначала войдите в VK (Настройки → Аккаунты)")
    if kind == "my":
        return _my_videos(token, route, 0, True)
    top = _check(_post(VK_API + f"catalog.getVideo?v={VK_V}&client_id={VK_CLIENT_ID}", {"access_token": token}, route))
    sec = pick_section(_sections(top), kind)
    if sec is None:
        raise ValueError("VK не показывает раздел «Подписки» для этого аккаунта")
    resp = _check(_post(VK_API + f"catalog.getSection?v={VK_V}&client_id={VK_CLIENT_ID}",
                        {"access_token": token, "section_id": sec["id"]}, route))
    items = parse_search(resp)
    if not items:
        items = parse_search(top) if kind == "recommended" else []
    return {"items": items, "signed_in": True, "adult": False, "more": _cursor(resp), "title": validate.clean_text(sec.get("title"), 80)}
