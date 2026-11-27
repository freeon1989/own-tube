# -*- coding: utf-8 -*-
"""Методы, которые окно отдаёт интерфейсу (window.pywebview.api.*).

Каждый возвращает {ok: true, ...} или {ok: false, error: "текст"}; исключения наружу не уходят.
Пути к файлам интерфейс не передаёт: файлы и папки выбираются в системном диалоге на стороне Python.
"""
from __future__ import annotations

import functools
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import __version__, search as yt, sponsorblock, takeout, validate, vk
from . import player as player_mod
from .db import Store, clean_item
from .downloads import Downloader
from .network import ManualProxy, RouteError, RouteProvider
from .settings import SettingsStore
from .tools import find_tool

log = logging.getLogger("owntube.api")


def safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            out = fn(self, *args)
            return {"ok": True, **(out or {})}
        except (ValueError, RouteError, player_mod.PlayerError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # сеть, yt-dlp: показываем коротко, подробности в журнал
            log.exception("Ошибка в %s", fn.__name__)
            return {"ok": False, "error": "Не получилось: " + str(exc).replace("ERROR: ", "")[:200]}
    return wrapper


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Ожидалось число")
    return value


class Api:
    def __init__(self, store: SettingsStore | None = None, db: Store | None = None,
                 player: player_mod.Player | None = None, routes: RouteProvider | None = None,
                 search_fn=None, start_downloader: bool = True):
        self._store = store or SettingsStore()
        self._db = db or Store(self._store.dir / "own-tube.db")
        self._player = player or player_mod.Player()
        self._routes = routes or ManualProxy(lambda: self._store.value.proxy)
        self._search = search_fn or yt.search
        self._lock = threading.Lock()
        self._window = None
        self._dl = Downloader(self._db, lambda: self._store.value, self._routes.current) if start_downloader else None

    def _attach(self, window) -> None:
        self._window = window

    # --- состояние и настройки ---
    @safe
    def get_state(self):
        return {"settings": self._store.value.public(), "counts": self._db.counts(), "version": __version__,
                "tools": {n: bool(find_tool(n, self._store.value.mpv_path if n == "mpv" else ""))
                          for n in ("mpv", "ffmpeg", "deno")},
                "categories": {k: v[0] for k, v in yt.CATEGORIES.items()}}

    def get_settings(self) -> dict:
        return {"ok": True, "settings": self._store.value.public()}

    @safe
    def save_settings(self, patch):
        with self._lock:
            s = self._store.update(patch)
        return {"settings": s.public()}

    @safe
    def check_route(self):
        return self._routes.check()

    # --- ленты и поиск ---
    @safe
    def search(self, query, source="yt"):
        source = validate.clean_source(source)
        q = validate.clean_query(query)
        route = self._routes.current()
        if source == "vk":
            return {"items": [vk.open_link(q, route)]}
        vid = validate.parse_youtube_link(q)
        if vid:
            return {"items": [{"source": "yt", "id": vid, "title": "Видео по ссылке", "channel": "",
                               "channel_id": "", "duration": "", "views": "", "live": False,
                               "thumb": validate.thumb_url(vid)}]}
        return {"items": self._search(q, route)}

    @safe
    def feed(self, kind, arg=None):
        route = self._routes.current()
        if kind == "home":
            subs = {s["channel_id"]: s["title"] for s in self._db.subscriptions()}
            if subs:
                return {"items": yt.subscriptions_feed(subs, route, per_channel=3, limit=36), "from": "subs"}
            q, sp = yt.HOME_FALLBACK
            return {"items": yt.search(q, route, 24, sp), "from": "popular"}
        if kind == "subs":
            subs = {s["channel_id"]: s["title"] for s in self._db.subscriptions()}
            return {"items": yt.subscriptions_feed(subs, route) if subs else []}
        if kind == "shorts":
            return {"items": yt.shorts(route)}
        if kind == "category":
            return {"items": yt.category(str(arg), route)}
        if kind == "channel":
            return {"items": yt.channel_videos(str(arg), route)}
        raise ValueError("Неизвестный раздел")

    # --- библиотека (локально) ---
    @safe
    def library(self, kind, arg=None):
        if kind == "history":
            return {"items": self._db.history()}
        if kind in ("later", "favorite"):
            return {"items": self._db.marks(kind)}
        if kind == "playlists":
            return {"playlists": self._db.playlists()}
        if kind == "playlist":
            return {"items": self._db.playlist_items(_int(arg))}
        if kind == "subscriptions":
            return {"channels": self._db.subscriptions()}
        if kind == "downloads":
            live = self._dl.live_progress() if self._dl else {}
            rows = self._db.downloads()
            for r in rows:
                if r["dl_id"] in live:
                    r["progress"] = round(live[r["dl_id"]], 1)
                r["path"] = Path(r["path"]).name if r.get("path") else ""
            return {"items": rows}
        raise ValueError("Неизвестный список")

    @safe
    def item_state(self, item):
        v = clean_item(item)
        return {"marks": self._db.marks_of(v["source"], v["id"]),
                "subscribed": bool(v["channel_id"]) and self._db.is_subscribed(v["channel_id"])}

    @safe
    def set_mark(self, kind, item, on):
        return {"on": self._db.set_mark(str(kind), item, bool(on))}

    @safe
    def create_playlist(self, name, item=None):
        pid = self._db.create_playlist(name)
        if item:
            self._db.add_to_playlist(pid, item)
        return {"id": pid}

    @safe
    def add_to_playlist(self, playlist_id, item):
        self._db.add_to_playlist(_int(playlist_id), item)

    @safe
    def remove_from_playlist(self, playlist_id, item):
        v = clean_item(item)
        self._db.remove_from_playlist(_int(playlist_id), v["source"], v["id"])

    @safe
    def delete_playlist(self, playlist_id):
        self._db.delete_playlist(_int(playlist_id))

    @safe
    def clear_history(self):
        self._db.clear_history()

    @safe
    def subscribe(self, channel_id, title, on=True):
        if on:
            self._db.subscribe(channel_id, title)
        elif validate.is_channel_id(channel_id):
            self._db.unsubscribe(channel_id)
        return {"subscribed": bool(on)}

    # --- воспроизведение ---
    @safe
    def play(self, item, quality=None):
        v = clean_item(item)
        s = self._store.value
        q = validate.clean_quality(quality) if quality is not None else s.quality
        route = self._routes.current()
        segs = ""
        if v["source"] == "yt" and s.sponsorblock:
            segs = sponsorblock.script_opt(sponsorblock.segments(v["id"], s.sb_categories, route))
        out = self._player.play(v["source"], v["id"], q, route, s.mpv_path, s.codec, segs)
        self._db.add_history({**item, **v, "title": out.get("title") or v["title"]})
        return {"skips": segs.count("-")}

    # --- загрузки ---
    @safe
    def download(self, item, quality=None):
        if not self._dl:
            raise ValueError("Загрузки выключены")
        q = validate.clean_quality(quality) if quality is not None else self._store.value.quality
        return {"id": self._dl.enqueue(clean_item(item), q)}

    @safe
    def cancel_download(self, dl_id):
        if self._dl:
            self._dl.cancel(_int(dl_id))

    @safe
    def remove_download(self, dl_id):
        self._db.delete_download(_int(dl_id))  # файл на диске не трогаем

    @safe
    def open_download_folder(self):
        folder = Path(self._store.value.download_dir)
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # noqa: S606 — путь из настроек, проверен как абсолютный
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(folder)], shell=False)

    def _dialog(self, folder: bool, types=()):
        if self._window is None:
            raise ValueError("Диалог выбора файла доступен только в окне программы")
        import webview

        kind = webview.FOLDER_DIALOG if folder else webview.OPEN_DIALOG
        res = self._window.create_file_dialog(kind, allow_multiple=False, file_types=types)
        if not res:
            return None
        return Path(res[0] if isinstance(res, (list, tuple)) else res)

    @safe
    def choose_download_dir(self):
        p = self._dialog(True)
        if p is None:
            return {"cancelled": True}
        return {"settings": self._store.update({"download_dir": str(p)}).public()}

    @safe
    def import_takeout(self):
        p = self._dialog(False, ("Google Takeout (*.zip;*.csv;*.json)", "Все файлы (*.*)"))
        if p is None:
            return {"cancelled": True}
        return {"summary": takeout.import_path(p, self._db)}

    # --- помощница (ИИ подключается на этапе 2) ---
    @safe
    def assistant_say(self, text):
        validate.clean_query(text)
        return {"reply": "Я пока без мозгов: ИИ подключим на втором этапе. "
                         "Уже сейчас могу улыбаться и махать рукой."}

    @safe
    def account_status(self):
        return {"google": False, "vk": False, "note": "Вход в Google и VK — второй этап"}

    def _shutdown(self) -> None:
        self._player.stop()
