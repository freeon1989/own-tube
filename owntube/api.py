# -*- coding: utf-8 -*-
"""Методы, которые окно отдаёт интерфейсу (window.pywebview.api.*).
Каждый возвращает {ok: true, ...} или {ok: false, error: "текст"}; исключения наружу не уходят."""
from __future__ import annotations

import logging
import threading

from . import player as player_mod
from . import search as search_mod
from . import validate
from .network import ManualProxy, RouteError, RouteProvider
from .settings import SettingsStore

log = logging.getLogger("owntube.api")


class Api:
    def __init__(self, store: SettingsStore | None = None, player: player_mod.Player | None = None,
                 search_fn=None, routes: RouteProvider | None = None):
        self._store = store or SettingsStore()
        self._player = player or player_mod.Player()
        self._search = search_fn or search_mod.search
        self._routes = routes or ManualProxy(lambda: self._store.value.proxy)
        self._lock = threading.Lock()

    def get_settings(self) -> dict:
        return {"ok": True, "settings": self._store.value.public()}

    def save_settings(self, patch) -> dict:
        try:
            with self._lock:
                s = self._store.update(patch)
            return {"ok": True, "settings": s.public()}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def check_route(self) -> dict:
        try:
            return {**self._routes.check()}
        except (ValueError, RouteError) as exc:
            return {"ok": False, "detail": str(exc)}

    def search(self, query) -> dict:
        try:
            q = validate.clean_query(query)
            items = self._search(q, self._routes.current())
            return {"ok": True, "items": items}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # сеть, yt-dlp: показываем коротко, подробности в журнал
            log.exception("Ошибка поиска")
            return {"ok": False, "error": "Поиск не удался: " + str(exc).replace("ERROR: ", "")[:200]}

    def play(self, video_id, quality=None) -> dict:
        try:
            if not validate.is_video_id(video_id):
                raise ValueError("Некорректный идентификатор видео")
            s = self._store.value
            q = validate.clean_quality(quality) if quality is not None else s.quality
            self._player.play(video_id, q, self._routes.current(), s.mpv_path)
            return {"ok": True}
        except (ValueError, RouteError, player_mod.PlayerError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("Ошибка запуска")
            return {"ok": False, "error": "Не удалось запустить: " + str(exc)[:200]}
