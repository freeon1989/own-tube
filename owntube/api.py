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

from . import __version__, accounts as acc_mod, diagnostics, search as yt, sponsorblock, takeout, validate, vk, ytdl
from .ai import assistant as ai_assistant
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
            return {"ok": False, "error": validate.redact(str(exc))}
        except Exception as exc:  # сеть, yt-dlp: показываем коротко, подробности в журнал
            log.exception("Ошибка в %s", fn.__name__)
            return {"ok": False, "error": "Не получилось: " + validate.redact(str(exc)).replace("ERROR: ", "")[:200]}
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
        self._routes = routes or ManualProxy(
            lambda: self._store.value.proxy,
            lambda: self._store.value.proxy_user,
            self._store.proxy_password,
        )
        self._search = search_fn or yt.search
        self._lock = threading.Lock()
        self._window = None
        self._dl = Downloader(self._db, lambda: self._store.value, self._routes.current) if start_downloader else None
        self._accounts = acc_mod.Accounts(self._store.secrets, self._store.dir)
        ytdl.set_cookie_provider(self._accounts.cookie_text)
        self._media = None
        self._jobs = ai_assistant.Jobs()
        self._watch: dict = {}

    def _media_server(self):
        with self._lock:
            if self._media is None:
                from .media import MediaServer
                self._media = MediaServer(self._routes.current, lambda: find_tool("ffmpeg"))
            return self._media

    def _attach(self, window) -> None:
        self._window = window

    # --- состояние и настройки ---
    @safe
    def get_state(self):
        return {"settings": self._store.export(), "counts": self._db.counts(), "version": __version__,
                "tools": {n: bool(find_tool(n, self._store.value.mpv_path if n == "mpv" else ""))
                          for n in ("mpv", "ffmpeg", "deno")},
                "categories": {k: v[0] for k, v in yt.CATEGORIES.items()},
                "media": self._media_server().base}

    def get_settings(self) -> dict:
        return {"ok": True, "settings": self._store.export()}

    @safe
    def save_settings(self, patch):
        with self._lock:
            s = self._store.update(patch)
        return {"settings": self._store.export()}

    @safe
    def check_route(self):
        """Проверка по шагам: TCP, вход на прокси, youtube.com, обложки, поиск yt-dlp."""
        if not isinstance(self._routes, ManualProxy):
            return self._routes.check()  # свой провайдер маршрута (например, будущий VPN) проверяет себя сам
        out = diagnostics.run(self._routes.current(), self._search)
        for st in out["steps"]:
            log.info("Проверка связи: %s — %s (%s)", st["name"], "OK" if st["ok"] else "сбой", st["detail"])
        return out

    @safe
    def read_log(self, lines=300):
        """Последние строки журнала. Пароли, ключи и логины в адресах вырезаются."""
        n = _int(lines) if lines is not None else 300
        n = max(10, min(n, 2000))
        path = self._store.dir / "own-tube.log"
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 512 * 1024))
                tail = fh.read().decode("utf-8", "replace").splitlines()[-n:]
        except FileNotFoundError:
            tail = []
        secrets_now = [self._store.proxy_password()] + [self._store.ai_api_key(c["id"]) for c in self._store.value.ai_configs]
        out = []
        for line in tail:
            line = validate.redact(line)
            for sec in secrets_now:
                if sec and len(sec) >= 4:
                    line = line.replace(sec, "***")
            out.append(line)
        return {"lines": out, "path_hint": "own-tube.log в папке данных программы"}

    # --- ленты и поиск ---
    @safe
    def search(self, query, source="yt"):
        source = validate.clean_source(source)
        q = validate.clean_query(query)
        route = self._routes.current()
        if source == "vk":
            if validate.parse_vk_link(q):
                return {"items": [vk.open_link(q, route)]}
            cookie = self._accounts.cookie_header("vk", "login.vk.com")
            res = vk.search(q, route, cookie, adult=self._store.value.vk_adult)
            return res
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
        self._store.update({"download_dir": str(p)})
        return {"settings": self._store.export()}

    @safe
    def import_takeout(self):
        p = self._dialog(False, ("Google Takeout (*.zip;*.csv;*.json)", "Все файлы (*.*)"))
        if p is None:
            return {"cancelled": True}
        return {"summary": takeout.import_path(p, self._db)}

    # --- встроенный плеер ---
    @safe
    def watch(self, item, quality=None):
        """Видео внутри окна: yt-dlp выбирает потоки, ffmpeg без перекодирования отдаёт их в <video>."""
        v = clean_item(item)
        s = self._store.value
        if not find_tool("ffmpeg"):
            raise ValueError("Для встроенного плеера нужен ffmpeg (папка tools рядом с программой)")
        q = validate.clean_quality(quality) if quality is not None else s.quality
        route = self._routes.current()
        streams = player_mod.resolve_streams(v["source"], v["id"], q, route, s.codec, embedded=True)
        if streams.get("live"):
            raise ValueError("Прямые эфиры пока открываются только в отдельном окне (mpv)")
        segs = []
        if v["source"] == "yt" and s.sponsorblock:
            try:
                segs = sponsorblock.segments(v["id"], s.sb_categories, route)
            except Exception:
                segs = []
        media = self._media_server()
        sid = media.add_session(streams)
        with self._lock:
            self._watch = {"sid": sid, "item": v}
        title = streams.get("title") or v["title"]
        self._db.add_history({**item, **v, "title": title})
        return {"sid": sid, "url": media.video_url(sid, 0), "base": media.base, "title": title,
                "channel": streams.get("channel") or v.get("channel") or "",
                "channel_id": streams.get("channel_id") or v.get("channel_id") or "",
                "duration": streams.get("duration") or 0, "height": streams.get("height") or 0,
                "container": streams.get("container"), "segments": [[a, b] for a, b in segs],
                "quality": q}

    @safe
    def seek_url(self, sid, start):
        media = self._media_server()
        if not isinstance(sid, str) or sid != self._watch.get("sid"):
            raise ValueError("Видео уже закрыто")
        if isinstance(start, bool) or not isinstance(start, (int, float)) or start < 0 or start > 86400 * 2:
            raise ValueError("Некорректная позиция")
        return {"url": media.video_url(sid, float(start))}

    @safe
    def stop_watch(self):
        if self._media is not None:
            self._media.stop_all()
        with self._lock:
            self._watch = {}

    @safe
    def related(self, item):
        v = clean_item(item)
        if v["source"] != "yt":
            cookie = self._accounts.cookie_header("vk", "login.vk.com")
            res = vk.search(v["title"][:80] or "видео", self._routes.current(), cookie)
            return {"items": [x for x in res["items"] if x["id"] != v["id"]][:16]}
        q = (v["channel"] + " " + v["title"]).strip()[:100] or v["id"]
        items = self._search(validate.clean_query(q), self._routes.current())
        return {"items": [x for x in items if x.get("id") != v["id"]][:16]}

    @safe
    def fullscreen(self, on=None):
        if self._window is None:
            raise ValueError("Нет окна")
        self._window.toggle_fullscreen()
        return {}

    @safe
    def play_external(self, item, quality=None):
        self.stop_watch()
        return self.play(item, quality)

    # --- помощница ---
    def _active_ai(self):
        cfg = next((c for c in self._store.value.ai_configs if c.get("active")), None)
        if cfg is None:
            cfg = next((c for c in self._store.value.ai_configs), None)
        if cfg is None:
            raise ValueError("Нет конфигурации ИИ. Добавьте её в Настройки → ИИ — API и сделайте активной.")
        return cfg

    @safe
    def assistant_send(self, text, context=None):
        from .ai.providers import chat_stream

        msg = ai_assistant.clean_message(text)
        ctx = ai_assistant.clean_context(context)
        cfg = self._active_ai()
        aid = self._store.value.assistant
        api_key = self._store.ai_api_key(cfg["id"])
        system = ai_assistant.system_prompt(aid, ctx, can_account=self._accounts.status()["google"]["signed_in"])
        self._db.chat_add(aid, "user", msg)
        history = ai_assistant.trim_history(self._db.chat_history(aid), int(cfg.get("context") or 8000), system)
        route = self._routes.current()
        ids = {i["id"] for i in ctx["items"]}

        def gen():
            return chat_stream(cfg, api_key, system, history, route)

        def done(full, error):
            clean, actions = ai_assistant.parse_actions(full, ids)
            if full:
                self._db.chat_add(aid, "assistant", full)
            return actions

        jid = self._jobs.start(gen, done)
        return {"job": jid, "model": cfg.get("model") or "", "config": cfg.get("name") or ""}

    @safe
    def assistant_poll(self, job):
        out = self._jobs.poll(_int(job))
        text, actions = ai_assistant.parse_actions(out["text"]) if out["done"] else (
            ai_assistant.ACTION_RE.sub("", out["text"]), [])
        return {**out, "text": text, "actions": out["actions"] if out["done"] else actions}

    @safe
    def assistant_cancel(self, job):
        self._jobs.cancel(_int(job))

    @safe
    def assistant_history(self):
        aid = self._store.value.assistant
        rows = []
        for m in self._db.chat_history(aid, 60):
            text = ai_assistant.parse_actions(m["content"])[0] if m["role"] == "assistant" else m["content"]
            rows.append({"role": m["role"], "text": text})
        try:
            cfg = self._active_ai()
            ai = {"name": cfg.get("name") or "", "model": cfg.get("model") or ""}
        except ValueError:
            ai = None
        return {"messages": rows, "ai": ai}

    @safe
    def assistant_clear(self):
        self._db.chat_clear(self._store.value.assistant)

    # --- аккаунты Google и VK ---
    @safe
    def account_status(self):
        st = self._accounts.status()
        return {"accounts": st}

    @safe
    def account_login(self, kind):
        kind = validate.clean_choice(kind, acc_mod.KINDS, "аккаунт")
        if self._window is None:
            raise ValueError("Вход доступен только в окне программы")
        self._accounts.start_login(kind)
        return {"accounts": self._accounts.status()}

    @safe
    def account_logout(self, kind):
        kind = validate.clean_choice(kind, acc_mod.KINDS, "аккаунт")
        self._accounts.logout(kind)
        return {"accounts": self._accounts.status()}

    @safe
    def account_import(self, kind):
        kind = validate.clean_choice(kind, acc_mod.KINDS, "аккаунт")
        p = self._dialog(False, ("cookies.txt (*.txt)", "Все файлы (*.*)"))
        if p is None:
            return {"cancelled": True}
        if p.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Файл слишком большой для cookies.txt")
        n = self._accounts.import_text(kind, p.read_text(encoding="utf-8", errors="replace"))
        return {"count": n, "accounts": self._accounts.status()}

    @safe
    def account_profile(self):
        """Имя и аватар Google (по возможности)."""
        if not self._accounts.status()["google"]["signed_in"]:
            return {"profile": {}}
        info = acc_mod.google_profile(self._accounts, self._routes.current())
        return {"profile": {"name": validate.clean_text(info.get("name"), 80),
                            "avatar": info.get("avatar") if validate.is_thumb_url(info.get("avatar")) else ""}}

    @safe
    def account_feed(self, kind, arg=None):
        """Подписки, история, смотреть позже, понравившиеся и плейлисты из аккаунта Google (через yt-dlp)."""
        if not self._accounts.status()["google"]["signed_in"]:
            raise ValueError("Сначала войдите в Google (Настройки → Аккаунты)")
        return yt.account_feed(str(kind), arg, self._routes.current())

    def _shutdown(self) -> None:
        self._player.stop()
        if self._media is not None:
            try:
                self._media.close()
            except Exception:
                pass

    @safe
    def save_ai_config(self, payload):
        with self._lock:
            cfg = self._store.save_ai_config(payload)
        return {"config": cfg, "settings": self._store.export()}

    @safe
    def delete_ai_config(self, cfg_id):
        with self._lock:
            self._store.delete_ai_config(cfg_id)
        return {"settings": self._store.export()}

    @safe
    def check_ai_key(self, payload):
        """Проверка ключа и список моделей. Ключ из формы или уже сохранённый."""
        if not isinstance(payload, dict):
            raise ValueError("Ожидался объект конфигурации")
        from .ai.config import clean_api_key, clean_base_url, clean_id
        from .ai.providers import check_connection

        base_url = payload.get("base_url")
        api_key = payload.get("api_key") or ""
        cfg_id = payload.get("id")
        if cfg_id:
            cfg_id = clean_id(cfg_id)
            existing = next((c for c in self._store.value.ai_configs if c["id"] == cfg_id), None)
            if existing is None:
                raise ValueError("Конфигурация не найдена")
            base_url = base_url or existing["base_url"]
            if not api_key:
                api_key = self._store.ai_api_key(cfg_id)
        api_key = clean_api_key(api_key)  # пустой ключ допустим: локальные серверы (LM Studio, Ollama)
        base_url = clean_base_url(base_url)
        return check_connection(base_url, api_key, self._routes.current())

