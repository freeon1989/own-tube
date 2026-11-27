# -*- coding: utf-8 -*-
"""Списки видео постранично: поиск, разделы, Shorts, канал, подписки. Кэш «показать старое, обновить в фоне».

Первая страница: innertube (быстро, один запрос), при сбое — yt-dlp. Дальше — по курсору «ещё».
Курсор — небольшой словарь; интерфейс возвращает его без изменений, мы его перепроверяем."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import innertube, search as yt, validate
from .network import Route

log = logging.getLogger("owntube.lists")
PAGE = 24
YDL_MAX = 120
KINDS = ("it", "y", "mem")
SPS = {yt.SP_WEEK, yt.SP_LIVE, yt.SP_SHORT, ""}


# ---------- курсор ----------
def clean_cursor(c) -> dict | None:
    if c is None:
        return None
    if not isinstance(c, dict) or c.get("k") not in KINDS + ("vk",):
        raise ValueError("Некорректное продолжение списка")
    out = {"k": c["k"]}
    for key, limit in (("t", 4000), ("q", 200), ("sp", 40), ("ep", 12), ("cid", 40), ("kind", 12), ("arg", 60),
                       ("key", 80), ("sec", 600)):
        v = c.get(key)
        if v is not None:
            if not isinstance(v, str) or len(v) > limit:
                raise ValueError("Некорректное продолжение списка")
            out[key] = v
    for key in ("n",):
        v = c.get(key)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 10000:
                raise ValueError("Некорректное продолжение списка")
            out[key] = v
    if "sh" in c:
        out["sh"] = bool(c["sh"])
    if out.get("sp", "") not in SPS:
        raise ValueError("Некорректное продолжение списка")
    if "cid" in out and not validate.is_channel_id(out["cid"]):
        raise ValueError("Некорректный канал")
    return out


# ---------- память для длинных списков (подписки): режем на страницы ----------
_mem: OrderedDict[str, list] = OrderedDict()
_mem_lock = threading.Lock()


def _mem_put(items: list) -> str:
    key = f"m{int(time.time() * 1000)}{len(items)}"
    with _mem_lock:
        _mem[key] = items
        while len(_mem) > 12:
            _mem.popitem(last=False)
    return key


def mem_has(key: str) -> bool:
    with _mem_lock:
        return key in _mem


def _slice(key: str, n: int) -> tuple[list, dict | None]:
    with _mem_lock:
        items = _mem.get(key)
    if items is None:
        return [], None
    part = items[n:n + PAGE]
    nxt = {"k": "mem", "key": key, "n": n + PAGE} if n + PAGE < len(items) else None
    return part, nxt


# ---------- ленты аккаунта Google: yt-dlp отдаёт список целиком, режем на страницы и дозапрашиваем глубже ----------
ACCT_KINDS = ("recommended", "subs", "history", "later", "liked", "playlist")
ACCT_FIRST = 48
ACCT_MAX = 300
ACCT_TTL = 300
_acct: dict[tuple, tuple[float, list, bool]] = {}
_acct_lock = threading.Lock()


def account_page(kind: str, arg, route: Route, n: int = 0, force: bool = False) -> tuple[list, dict | None]:
    """Страница ленты аккаунта. Список целиком держим 5 минут; если нужна страница глубже — берём больше (до 300)."""
    if kind not in ACCT_KINDS:
        raise ValueError("Неизвестная лента аккаунта")
    key = (kind, str(arg or ""))
    with _acct_lock:
        hit = _acct.get(key)
    fresh = bool(hit) and time.time() - hit[0] < ACCT_TTL and not (force and n == 0)
    if not fresh or (len(hit[1]) < n + PAGE and not hit[2]):
        have = len(hit[1]) if hit else 0
        limit = ACCT_FIRST if n == 0 else min(ACCT_MAX, max(n + 2 * PAGE, 2 * have))
        items = yt.account_items(kind, route, limit, arg)
        exhausted = len(items) < limit or limit >= ACCT_MAX
        with _acct_lock:
            _acct[key] = (time.time(), items, exhausted)
            while len(_acct) > 12:
                _acct.pop(next(iter(_acct)))
        hit = (time.time(), items, exhausted)
    items, exhausted = hit[1], hit[2]
    part = items[n:n + PAGE]
    more = None
    if n + PAGE < len(items) or (not exhausted and n + PAGE <= ACCT_MAX):
        more = {"k": "y", "kind": "acct", "q": kind, "arg": str(arg or "")[:60], "n": n + PAGE}
    return part, more


def account_forget() -> None:
    with _acct_lock:
        _acct.clear()


# ---------- запросы ----------
def _it_search(q: str, route: Route, sp: str, shorts: bool, token: str = "") -> tuple[list, dict | None]:
    items, nxt = innertube.search(q, route, sp, token, shorts)
    cur = {"k": "it", "ep": "search", "q": q, "sp": sp, "sh": shorts, "t": nxt} if nxt else None
    return items, cur


def _ydl_search(q: str, route: Route, sp: str, shorts: bool, n: int = 0) -> tuple[list, dict | None]:
    got = yt.search(q, route, min(n + PAGE, YDL_MAX), sp)
    if shorts:
        got = [i for i in got if not i["live"]]
    items = got[n:]
    more = {"k": "y", "kind": "search", "q": q, "sp": sp, "sh": shorts, "n": n + PAGE} \
        if len(got) >= n + PAGE - 4 and n + PAGE < YDL_MAX else None
    return items, more


def _search_page(q: str, route: Route, sp: str = "", shorts: bool = False) -> tuple[list, dict | None]:
    try:
        items, cur = _it_search(q, route, sp, shorts)
        if items:
            return items, cur
        log.info("innertube вернул пустой список, пробую yt-dlp")
    except Exception as exc:
        log.info("innertube не ответил (%s), пробую yt-dlp", validate.redact(str(exc))[:120])
    return _ydl_search(q, route, sp, shorts)


def _channel_page(cid: str, route: Route) -> tuple[list, dict | None]:
    try:
        items, nxt = innertube.channel(cid, route)
        if items:
            return items, ({"k": "it", "ep": "channel", "cid": cid, "t": nxt} if nxt else None)
    except Exception as exc:
        log.info("innertube (канал) не ответил (%s), пробую yt-dlp", validate.redact(str(exc))[:120])
    got = yt.channel_videos(cid, route, 60)
    key = _mem_put(got)
    return _slice(key, 0)


def _subs_page(subs: dict, route: Route, per: int, total: int) -> tuple[list, dict | None]:
    items = yt.subscriptions_feed(subs, route, per_channel=per, limit=total)
    return _slice(_mem_put(items), 0)


def first(kind: str, arg, route: Route, subs: dict | None = None) -> tuple[list, dict | None, str]:
    """Первая страница. Третье значение — откуда взят список ('subs', 'popular' или '')."""
    if kind == "home":
        if subs:
            items, cur = _subs_page(subs, route, 3, 90)
            return items, cur, "subs"
        q, sp = yt.HOME_FALLBACK
        items, cur = _search_page(q, route, sp)
        return items, cur, "popular"
    if kind == "subs":
        if not subs:
            return [], None, ""
        items, cur = _subs_page(subs, route, 5, 120)
        return items, cur, ""
    if kind == "shorts":
        items, cur = _search_page("#shorts", route, yt.SP_SHORT, True)
        return items, cur, ""
    if kind == "category":
        if str(arg) not in yt.CATEGORIES:
            raise ValueError("Неизвестный раздел")
        _, q, sp = yt.CATEGORIES[str(arg)]
        items, cur = _search_page(q, route, sp)
        return items, cur, ""
    if kind == "channel":
        if not validate.is_channel_id(str(arg)):
            raise ValueError("Некорректный канал")
        items, cur = _channel_page(str(arg), route)
        return items, cur, ""
    if kind == "search":
        items, cur = _search_page(validate.clean_query(str(arg)), route)
        return items, cur, ""
    raise ValueError("Неизвестный раздел")


def more(cursor: dict, route: Route) -> tuple[list, dict | None]:
    k = cursor["k"]
    if k == "mem":
        return _slice(cursor.get("key", ""), cursor.get("n", 0))
    if k == "it":
        if cursor.get("ep") == "channel":
            items, nxt = innertube.channel(cursor["cid"], route, cursor["t"])
            return items, ({**cursor, "t": nxt} if nxt else None)
        items, nxt = innertube.search("", route, "", cursor["t"], bool(cursor.get("sh")))
        return items, ({**cursor, "t": nxt} if nxt else None)
    if k == "y" and cursor.get("kind") == "acct":
        return account_page(cursor.get("q", ""), cursor.get("arg") or None, route, cursor.get("n", 0))
    if k == "y":
        return _ydl_search(validate.clean_query(cursor.get("q", "")), route, cursor.get("sp", ""),
                           bool(cursor.get("sh")), cursor.get("n", 0))
    raise ValueError("Некорректное продолжение списка")


# ---------- кэш первых страниц ----------
class FeedCache:
    """Последние первые страницы списков: мгновенно показываем старое, пока грузится свежее."""

    def __init__(self, path: Path | None = None, max_entries: int = 40, max_age: float = 3 * 86400):
        self.path, self.max_entries, self.max_age = path, max_entries, max_age
        self._d: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in (raw if isinstance(raw, dict) else {}).items():
                    if isinstance(v, dict) and isinstance(v.get("items"), list) and isinstance(v.get("ts"), (int, float)):
                        self._d[str(k)] = v
            except (OSError, ValueError):
                log.info("Кэш списков не прочитан, начинаю заново")

    def get(self, key: str) -> dict | None:
        with self._lock:
            v = self._d.get(key)
            if v and time.time() - v["ts"] > self.max_age:
                return None
            return dict(v) if v else None

    def put(self, key: str, items: list, cursor, extra: dict | None = None) -> None:
        with self._lock:
            self._d[key] = {"items": items, "more": cursor, "ts": time.time(), "extra": extra or {}}
            self._d.move_to_end(key)
            while len(self._d) > self.max_entries:
                self._d.popitem(last=False)
            snapshot = dict(self._d)
        if self.path:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log.info("Кэш списков не записан: %s", exc)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
        if self.path:
            try:
                self.path.unlink()
            except OSError:
                pass
