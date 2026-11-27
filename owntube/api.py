# -*- coding: utf-8 -*-
"""Методы, которые окно отдаёт интерфейсу (window.pywebview.api.*).

Каждый возвращает {ok: true, ...} или {ok: false, error: "текст"}; исключения наружу не уходят.
Пути к файлам интерфейс не передаёт: файлы и папки выбираются в системном диалоге на стороне Python.
"""
from __future__ import annotations

import functools
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, accounts as acc_mod, diagnostics, lists, voice, search as yt, sponsorblock, takeout, validate, vk, ytdl
from .ai import assistant as ai_assistant
from . import player as player_mod
from .db import Store, clean_item
from .downloads import Downloader
from . import awg as awg_mod, updater
from .network import ManualProxy, RouteError, RouteProvider
from .settings import SettingsStore
from .tools import app_dir, find_tool

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
        self._awg = awg_mod.AwgProvider(self._awg_config)
        self._routes = routes or awg_mod.SwitchRoutes(
            lambda: self._store.value.net_mode,
            ManualProxy(lambda: self._store.value.proxy, lambda: self._store.value.proxy_user, self._store.proxy_password),
            self._awg,
        )
        self._search_fn = search_fn
        self._cache = lists.FeedCache(self._store.dir / "feedcache.json")
        self._lock = threading.Lock()
        self._window = None
        self._dl = Downloader(self._db, lambda: self._store.value, self._routes.current) if start_downloader else None
        self._accounts = acc_mod.Accounts(self._store.secrets, self._store.dir)
        ytdl.set_cookie_provider(self._accounts.cookie_text)
        self._media = None
        self._jobs = ai_assistant.Jobs()
        self._watch: dict = {}
        self._streamcache = player_mod.StreamCache()
        self._installer = voice.Installer()
        self._stt_lock = threading.Lock()
        self._prefetching: set = set()
        self._update_job = updater.Job()
        self._update_info = None

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
            self._store.update(patch)
        return {"settings": self._store.export()}

    def warmup(self) -> None:
        """Фоновая подготовка при запуске: импорт yt-dlp, deno, готовые соединения с YouTube через прокси."""
        def run():
            t = time.monotonic()
            try:
                importlib.import_module("yt_dlp")  # первый импорт занимает ~0,5 с
                route = self._routes.current()
                from . import httpc
                for url in ("https://www.youtube.com/robots.txt", "https://i.ytimg.com/generate_204"):
                    try:
                        httpc.fetch("GET", url, route, limit=64 * 1024, timeout=10, attempts=1)
                    except Exception as exc:
                        log.info("Прогрев соединения не удался: %s", validate.redact(str(exc))[:100])
                deno = find_tool("deno")
                if deno:
                    subprocess.run([deno, "--version"], capture_output=True, timeout=20, shell=False,
                                   creationflags=0x08000000 if sys.platform.startswith("win") else 0)
            except Exception as exc:
                log.info("Прогрев: %s", validate.redact(str(exc))[:100])
            log.info("Прогрев занял %.1f с", time.monotonic() - t)
        threading.Thread(target=run, name="owntube-warmup", daemon=True).start()

    def _diag_search(self, q, route, n=3):
        if self._search_fn:
            return self._search_fn(q, route)[:n], "yt-dlp"
        return lists.first("search", q, route)[0][:n], "быстрый поиск YouTube"

    def _diag_pipe(self, streams: dict, nbytes: int = 128 * 1024):
        """Те же потоки через встроенный плеер (ffmpeg + локальный сервер): сколько байт пришло в <video>."""
        import urllib.request
        media = self._media_server()
        sid = media.add_session(streams, exclusive=False)
        try:
            with urllib.request.urlopen(media.video_url(sid, 0), timeout=60) as r:
                got = r.read(nbytes)
            return len(got)
        finally:
            media.drop_session(sid)

    def _diag_video(self, route, codec="auto"):
        """Реальный поток тестового ролика (через yt-dlp) для шага «Проверка видео»."""
        return player_mod.resolve_streams("yt", "dQw4w9WgXcQ", "360", route, codec, embedded=True)

    @safe
    def check_route(self):
        """Проверка по шагам: (VPN), TCP, вход на прокси, youtube.com, обложки, поиск, видео."""
        vpn = isinstance(self._routes, awg_mod.SwitchRoutes) and self._routes.vpn
        if not vpn and not isinstance(self._routes, (ManualProxy, awg_mod.SwitchRoutes)):
            return self._routes.check()  # свой провайдер маршрута проверяет себя сам
        first = None
        if vpn:
            t = time.monotonic()
            res = self._awg.check()
            first = {"name": "Встроенный VPN (AmneziaWG)", "ok": res["ok"], "detail": res["detail"],
                     "ms": int((time.monotonic() - t) * 1000)}
            if not res["ok"]:
                out = {"ok": False, "steps": [first]}
                log.info("Проверка связи: %s — сбой (%s)", first["name"], first["detail"])
                return out
        out = diagnostics.run(self._routes.current(), self._diag_search,
                              None if self._search_fn else self._diag_video, None if self._search_fn else self._diag_pipe)
        if first:
            out["steps"].insert(0, first)
        for st in out["steps"]:
            log.info("Проверка связи: %s — %s (%s)", st["name"], "OK" if st["ok"] else "сбой", st["detail"])
        return out

    # --- встроенный VPN (AmneziaWG) ---
    def _awg_config(self):
        raw = self._store.secrets.get(awg_mod.SECRET_NAME)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _vpn_state(self, probe: bool = False) -> dict:
        cfg = self._awg_config()
        return {"enabled": self._store.value.net_mode == "awg", "imported": bool(cfg),
                "config": awg_mod.summary(cfg) if cfg else None,
                "helper": bool(self._awg._find()),
                "persist": self._store.secrets.can_persist,
                "status": self._awg.status(probe) if cfg else {"running": False}}

    # --- обновление из релизов GitHub ---
    @safe
    def update_check(self, auto=False):
        """Один запрос к api.github.com через маршрут программы. auto=True — проверка при запуске (учитывает настройку)."""
        st = self._store.value
        if auto and not st.update_check:
            return {"disabled": True}
        try:
            info = updater.check(self._routes.current(), st.update_skip)
        except updater.UpdateError as exc:
            if auto:
                log.info("Проверка обновлений: %s", exc)
                return {"error": str(exc)}
            raise ValueError(str(exc)) from None
        self._update_info = info
        if info["available"]:
            log.info("Доступна новая версия %s", info["version"])
        return {k: info[k] for k in ("version", "current", "available", "skipped", "notes", "url", "published", "has_assets")} | {
            "frozen": bool(getattr(sys, "frozen", False)), "backup": self._update_backup()}

    @safe
    def update_open_page(self):
        """Открыть страницу релиза в браузере (только github.com/freeon1989/own-tube)."""
        info = self._update_info or {}
        url = str(info.get("url") or updater.PAGE_LATEST)
        if not url.startswith(f"https://github.com/{updater.REPO}/releases"):
            raise ValueError("Неверный адрес")
        import webbrowser
        webbrowser.open(url)
        return {}

    def _update_backup(self):
        if not getattr(sys, "frozen", False):
            return None
        b = updater.backups(app_dir())
        return b[0].name.rsplit(".old-", 1)[-1] if b else None

    @safe
    def update_skip(self, version):
        with self._lock:
            self._store.update({"update_skip": str(version or "")})
        return {"settings": self._store.export()}

    @safe
    def update_download(self):
        info = getattr(self, "_update_info", None)
        if not info or not info.get("available"):
            raise ValueError("Сначала проверьте обновления")
        if not getattr(sys, "frozen", False):
            raise ValueError("Обновление ставится только в собранную программу (own-tube.exe); из исходников — git pull")
        if not updater.can_write(app_dir().parent):
            raise ValueError("Нет прав на запись рядом с папкой программы — скачайте архив со страницы релиза вручную")
        self._update_job.start(info, self._routes.current())
        return self._update_job.state

    @safe
    def update_progress(self):
        return dict(self._update_job.state)

    @safe
    def update_cancel(self):
        self._update_job.cancel()
        return dict(self._update_job.state)

    @safe
    def update_apply(self):
        """Только по нажатию «Обновить»: запускает замену и закрывает программу."""
        st = self._update_job.state
        if st.get("stage") != "ready":
            raise ValueError("Обновление ещё не готово")
        plan = updater.make_plan(app_dir(), Path(st["staged"]), st["version"])
        updater.launch(plan, self._store.dir)
        log.info("Обновление до %s: программа закрывается для замены файлов", st["version"])
        threading.Timer(0.5, self._close_window).start()
        return {"closing": True}

    @safe
    def update_rollback(self):
        """Вернуть предыдущую версию из резервной копии (её же механизмом замены)."""
        if not getattr(sys, "frozen", False):
            raise ValueError("Откат доступен только в собранной программе")
        b = updater.backups(app_dir())
        if not b:
            raise ValueError("Резервной копии нет")
        ver = b[0].name.rsplit(".old-", 1)[-1]
        root = updater.staging_root() / ("rollback-" + ver)
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(b[0], root)
        updater.launch(updater.make_plan(app_dir(), root, ver), self._store.dir)
        threading.Timer(0.5, self._close_window).start()
        return {"closing": True}

    @safe
    def update_result(self):
        """Итог последней замены (показывается один раз после перезапуска)."""
        p = self._store.dir / "update-result.json"
        try:
            res = json.loads(p.read_text(encoding="utf-8"))
            p.unlink()
            return {"result": res}
        except (OSError, ValueError):
            return {"result": None}

    def _close_window(self):
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass

    @safe
    def vpn_state(self, probe=False):
        return self._vpn_state(bool(probe))

    def _vpn_store(self, cfg: dict) -> dict:
        text = json.dumps(cfg, ensure_ascii=False)
        saved = self._store.secrets.set(text, awg_mod.SECRET_NAME)
        self._awg.stop()  # новая конфигурация — новый туннель при следующем запросе
        log.info("VPN: конфигурация импортирована (%s)%s", awg_mod.summary(cfg)["kind"], "" if saved else ", только до закрытия программы")
        return self._vpn_state()

    @safe
    def vpn_import_text(self, text):
        if not isinstance(text, str):
            raise ValueError("Нужна ссылка vpn:// или текст конфигурации")
        return self._vpn_store(awg_mod.parse_any(text))

    @safe
    def vpn_import_file(self):
        p = self._dialog(False, ("AmneziaWG (*.conf;*.txt;*.vpn)", "Все файлы (*.*)"))
        if p is None:
            return {"cancelled": True}
        if p.stat().st_size > 200_000:
            raise ValueError("Файл слишком большой для конфигурации")
        return self._vpn_store(awg_mod.parse_any(p.read_text(encoding="utf-8", errors="replace")))

    @safe
    def vpn_enable(self, on):
        if not isinstance(on, bool):
            raise ValueError("Ожидалось да/нет")
        if on and not self._awg_config():
            raise ValueError("Сначала импортируйте конфигурацию AmneziaWG")
        with self._lock:
            self._store.update({"net_mode": "awg" if on else "proxy"})
        if on:
            self._awg.current()  # поднимаем сразу, чтобы ошибка была видна здесь же
        else:
            self._awg.stop()
        return {**self._vpn_state(probe=on), "settings": self._store.export()}

    @safe
    def vpn_restart(self):
        self._awg.stop()
        self._awg.current()
        return self._vpn_state(probe=True)

    @safe
    def vpn_forget(self):
        self._awg.stop()
        self._store.secrets.delete(awg_mod.SECRET_NAME)
        with self._lock:
            self._store.update({"net_mode": "proxy"})
        log.info("VPN: конфигурация удалена")
        return {**self._vpn_state(), "settings": self._store.export()}

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
    def _search_items(self, q: str, route) -> list:
        if self._search_fn:
            return self._search_fn(q, route)
        return lists.first("search", q, route)[0]

    def _cached_list(self, key: str, mode: str, producer) -> dict:
        """mode='cache' — отдать сохранённый список сразу (или items=None); иначе загрузить свежий и сохранить."""
        if mode == "cache":
            c = self._cache.get(key)
            if not c:
                return {"items": None}
            more = c.get("more")
            if more and more.get("k") == "mem" and not lists.mem_has(more.get("key", "")):
                more = None
            return {"items": c["items"], "more": more, "stale": True, "age": int(time.time() - c["ts"]), **(c.get("extra") or {})}
        items, cur, extra = producer()
        self._cache.put(key, items, cur, extra)
        return {"items": items, "more": cur, **extra}

    @safe
    def search(self, query, source="yt", more=None, mode="fresh"):
        source = validate.clean_source(source)
        q = validate.clean_query(query)
        mode = "cache" if mode == "cache" else "fresh"
        route = self._routes.current()
        if source == "vk":
            if validate.parse_vk_link(q):
                return {"items": [vk.open_link(q, route)], "more": None}
            cookie = self._accounts.cookie_header("vk", "login.vk.com")
            cur = lists.clean_cursor(more)
            if cur and cur["k"] != "vk":
                raise ValueError("Некорректное продолжение списка")
            if cur:
                return vk.search(q, route, cookie, adult=self._store.value.vk_adult, cursor=cur)
            def produce():
                res = vk.search(q, route, cookie, adult=self._store.value.vk_adult)
                return res["items"], res.get("more"), {"signed_in": res.get("signed_in"), "adult": res.get("adult")}
            return self._cached_list("vk:" + q.lower(), mode, produce)
        vid = validate.parse_youtube_link(q)
        if vid:
            return {"items": [{"source": "yt", "id": vid, "title": "Видео по ссылке", "channel": "",
                               "channel_id": "", "duration": "", "views": "", "live": False,
                               "thumb": validate.thumb_url(vid)}], "more": None}
        if self._search_fn:
            return {"items": self._search_fn(q, route), "more": None}
        cur = lists.clean_cursor(more)
        if cur:
            items, nxt = lists.more(cur, route)
            return {"items": items, "more": nxt}
        def produce_yt():
            items, nxt, _ = lists.first("search", q, route)
            return items, nxt, {}
        return self._cached_list("search:" + q.lower(), mode, produce_yt)

    @safe
    def feed(self, kind, arg=None, more=None, mode="fresh"):
        route = self._routes.current()
        mode = "cache" if mode == "cache" else "fresh"
        if kind not in ("home", "subs", "shorts", "category", "channel"):
            raise ValueError("Неизвестный раздел")
        cur = lists.clean_cursor(more)
        if cur:
            items, nxt = lists.more(cur, route)
            return {"items": items, "more": nxt}
        subs = {s["channel_id"]: s["title"] for s in self._db.subscriptions()} if kind in ("home", "subs") else None
        sig = str(abs(hash(tuple(sorted(subs)))) % 10**8) if subs else ""
        key = f"{kind}:{arg or ''}:{sig}"

        def produce():
            items, nxt, origin = lists.first(kind, arg, route, subs)
            return items, nxt, ({"from": origin} if origin else {})
        return self._cached_list(key, mode, produce)

    @safe
    def more(self, cursor, source="yt", query=""):
        """«Показать ещё» для любого списка по курсору, который вернул search/feed."""
        cur = lists.clean_cursor(cursor)
        if not cur:
            return {"items": [], "more": None}
        route = self._routes.current()
        if cur["k"] == "vk":
            cookie = self._accounts.cookie_header("vk", "login.vk.com")
            return vk.search(validate.clean_query(query or "видео"), route, cookie, adult=self._store.value.vk_adult, cursor=cur)
        items, nxt = lists.more(cur, route)
        return {"items": items, "more": nxt}

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
    def _streams(self, v: dict, quality: str, route, codec: str, fresh: bool = False, ipv4: bool = False) -> tuple[dict, bool]:
        key = (v["source"], v["id"], quality, codec, route.proxy or "", route.user or "", ipv4)
        return self._streamcache.get(
            key, lambda: player_mod.resolve_streams(v["source"], v["id"], quality, route, codec, embedded=True,
                                                    **({"ipv4": True} if ipv4 else {})), fresh)

    def _effective_quality(self, quality, short: bool) -> str:
        q = validate.clean_quality(quality) if quality is not None else self._store.value.quality
        if short and q in ("best", "1080"):
            return "720"  # вертикальным роликам больше не нужно, зато открываются заметно быстрее
        return q

    @safe
    def watch(self, item, quality=None, short=False):
        """Видео внутри окна: yt-dlp выбирает потоки, ffmpeg без перекодирования отдаёт их в <video>.
        short=True — режим Shorts: сеансы не сбрасываются, соседние ролики могут быть подготовлены заранее."""
        v = clean_item(item)
        s = self._store.value
        if not find_tool("ffmpeg"):
            raise ValueError("Для встроенного плеера нужен ffmpeg (папка tools рядом с программой)")
        short = bool(short)
        q = self._effective_quality(quality, short)
        route = self._routes.current()
        t0 = time.monotonic()
        streams, cached = self._streams(v, q, route, s.codec)
        if streams.get("live"):
            raise ValueError("Прямые эфиры пока открываются только в отдельном окне (mpv)")
        log.info("Потоки для %s: %s, %.1f с, %s", v["source"], "из кэша" if cached else "получены",
                 time.monotonic() - t0, f"{streams.get('height') or '?'}p {streams.get('container')}")
        segs = []
        if v["source"] == "yt" and s.sponsorblock and not short:
            try:
                segs = sponsorblock.segments(v["id"], s.sb_categories, route)
            except Exception:
                segs = []
        media = self._media_server()

        def refresh():
            alt = player_mod.alt_codec(s.codec)
            cur = self._routes.current()
            return self._streams(v, q, cur, alt, fresh=True, ipv4=cur.direct)[0]

        sid = media.add_session(streams, refresh=refresh, exclusive=not short)
        with self._lock:
            if not short:
                self._watch = {"sid": sid, "item": v}
        title = streams.get("title") or v["title"]
        self._db.add_history({**item, **v, "title": title})
        return {"sid": sid, "url": media.video_url(sid, 0), "base": media.base, "title": title,
                "channel": streams.get("channel") or v.get("channel") or "",
                "channel_id": streams.get("channel_id") or v.get("channel_id") or "",
                "duration": streams.get("duration") or 0, "height": streams.get("height") or 0,
                "container": streams.get("container"), "segments": [[a, b] for a, b in segs],
                "quality": q, "cached": cached}

    @safe
    def prefetch(self, item, short=False):
        """Заранее разобрать потоки ролика (наведение на карточку, следующий Short). Ничего не открывает."""
        v = clean_item(item)
        if v["source"] not in ("yt", "vk") or not find_tool("ffmpeg"):
            return {"started": False}
        q = self._effective_quality(None, bool(short))
        route = self._routes.current()
        key = (v["source"], v["id"])
        with self._lock:
            if key in self._prefetching or len(self._prefetching) >= 3:
                return {"started": False}
            self._prefetching.add(key)

        def run():
            try:
                self._streams(v, q, route, self._store.value.codec)
            except Exception as exc:
                log.debug("Подготовка %s не удалась: %s", v["id"], validate.redact(str(exc))[:100])
            finally:
                with self._lock:
                    self._prefetching.discard(key)
        threading.Thread(target=run, name="owntube-prefetch", daemon=True).start()
        return {"started": True}

    @safe
    def drop_watch(self, sid):
        if isinstance(sid, str) and sid:
            self._media_server().drop_session(sid)
        return {}

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
        items = self._search_items(validate.clean_query(q), self._routes.current())
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

    # --- голос: распознавание речи ---
    def _whisper_dir(self) -> Path:
        d = self._store.value.whisper_dir
        return Path(d) if d else voice.default_dir(self._store.dir)

    @safe
    def stt_status(self):
        d = self._whisper_dir()
        try:
            cfg = self._active_ai()
            from .ai.providers import kind_of
            api_ok = kind_of(cfg.get("base_url") or "") == "openai"
        except ValueError:
            api_ok = False
        return {"local": voice.local_ready(d), "dir": str(d), "install": self._installer.snapshot(), "api_possible": api_ok,
                "ffmpeg": bool(find_tool("ffmpeg")), "engine": self._store.value.stt_engine,
                "size_mb": round(voice.WHISPER_MODEL["size"] / 1048576) + round(voice.WHISPER_ZIP["size"] / 1048576)}

    @safe
    def stt_install(self):
        started = self._installer.start(self._whisper_dir(), self._routes.current())
        return {"started": started, "install": self._installer.snapshot()}

    @safe
    def stt_transcribe(self, audio, mime=""):
        """audio — запись с микрофона в base64 (WebM/Opus). Возвращает {text, engine}."""
        import base64
        import binascii
        if not isinstance(audio, str) or not audio or len(audio) > voice.MAX_AUDIO * 4 // 3 + 16:
            raise ValueError("Запись пустая или слишком длинная")
        try:
            raw = base64.b64decode(audio, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Запись повреждена") from exc
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise ValueError("Для распознавания речи нужен ffmpeg (папка tools рядом с программой)")
        s = self._store.value
        route = self._routes.current()
        local_ok = voice.local_ready(self._whisper_dir())
        engine = s.stt_engine
        if engine == "auto":
            engine = "local" if local_ok else "api"
        if not self._stt_lock.acquire(blocking=False):
            raise ValueError("Предыдущая запись ещё распознаётся")
        wav = None
        try:
            wav = voice.to_wav(ffmpeg, raw, voice.temp_dir(self._store.dir))
            t = time.monotonic()
            if engine == "local":
                text = voice.transcribe_local(self._whisper_dir(), wav, "ru")
            else:
                cfg = self._active_ai()
                try:
                    text = voice.transcribe_api(cfg["base_url"], self._store.ai_api_key(cfg["id"]), wav, s.stt_model, "ru", route)
                except voice.VoiceError as exc:
                    hint = "" if local_ok else " Можно установить локальное распознавание: Настройки → Голос."
                    raise voice.VoiceError(str(exc) + hint) from exc
            log.info("Распознавание (%s): %d симв. за %.1f с", engine, len(text), time.monotonic() - t)
            if not text:
                raise ValueError("Речь не распознана — попробуйте сказать громче или ближе к микрофону")
            return {"text": text, "engine": engine}
        except voice.VoiceError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            self._stt_lock.release()
            if wav is not None:
                try:
                    wav.unlink()
                except OSError:
                    pass

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
        self._awg.stop()
        try:
            from . import httpc
            httpc.close_pool()
        except Exception:
            pass
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

