# -*- coding: utf-8 -*-
"""Локальные данные в SQLite: история, «Смотреть позже», избранное, плейлисты, подписки, загрузки.

Все запросы параметризованы. Видео хранится один раз в таблице videos, списки ссылаются на него.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from . import validate

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS videos(
  source TEXT NOT NULL, id TEXT NOT NULL, title TEXT, channel TEXT, channel_id TEXT,
  duration TEXT, thumb TEXT, updated REAL, PRIMARY KEY(source, id));
CREATE TABLE IF NOT EXISTS history(
  source TEXT NOT NULL, id TEXT NOT NULL, watched REAL NOT NULL, PRIMARY KEY(source, id));
CREATE TABLE IF NOT EXISTS marks(
  kind TEXT NOT NULL CHECK(kind IN ('later','favorite')), source TEXT NOT NULL, id TEXT NOT NULL,
  added REAL NOT NULL, PRIMARY KEY(kind, source, id));
CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS playlist_items(
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  source TEXT NOT NULL, id TEXT NOT NULL, added REAL NOT NULL, PRIMARY KEY(playlist_id, source, id));
CREATE TABLE IF NOT EXISTS subscriptions(channel_id TEXT PRIMARY KEY, title TEXT, added REAL NOT NULL);
CREATE TABLE IF NOT EXISTS downloads(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, vid TEXT NOT NULL, quality TEXT NOT NULL,
  status TEXT NOT NULL, progress REAL DEFAULT 0, path TEXT, error TEXT, created REAL NOT NULL);
"""
SCHEMA_VERSION = "1"
HISTORY_LIMIT = 5000


def clean_item(item: object) -> dict:
    """Карточка из интерфейса или из сети -> безопасная запись. Картинку YouTube строим из ID сами."""
    if not isinstance(item, dict):
        raise ValueError("Ожидалась карточка видео")
    source = validate.clean_source(item.get("source", "yt"))
    vid = item.get("id")
    if not validate.is_item_id(source, vid):
        raise ValueError("Некорректный идентификатор видео")
    if source == "yt":
        thumb = validate.thumb_url(vid)
    else:
        thumb = item.get("thumb") if validate.is_thumb_url(item.get("thumb")) else ""
    ch = item.get("channel_id")
    return {
        "source": source, "id": vid,
        "title": validate.clean_text(item.get("title"), 300) or "Без названия",
        "channel": validate.clean_text(item.get("channel"), 120),
        "channel_id": ch if validate.is_channel_id(ch) else "",
        "duration": validate.clean_text(item.get("duration"), 12),
        "thumb": thumb,
    }


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema',?)", (SCHEMA_VERSION,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _x(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    def _tx(self, fn):
        with self._lock:
            self._db.execute("BEGIN")
            try:
                out = fn()
                self._db.execute("COMMIT")
                return out
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # --- видео ---
    def upsert_video(self, item: dict) -> dict:
        v = clean_item(item)
        self._x("""INSERT INTO videos(source,id,title,channel,channel_id,duration,thumb,updated)
                   VALUES(:source,:id,:title,:channel,:channel_id,:duration,:thumb,:t)
                   ON CONFLICT(source,id) DO UPDATE SET
                     title=CASE WHEN excluded.title IN ('Без названия','Видео из Takeout') THEN videos.title ELSE excluded.title END,
                     channel=CASE WHEN excluded.channel<>'' THEN excluded.channel ELSE videos.channel END,
                     channel_id=CASE WHEN excluded.channel_id<>'' THEN excluded.channel_id ELSE videos.channel_id END,
                     duration=CASE WHEN excluded.duration<>'' THEN excluded.duration ELSE videos.duration END,
                     thumb=CASE WHEN excluded.thumb<>'' THEN excluded.thumb ELSE videos.thumb END,
                     updated=excluded.updated""", {**v, "t": time.time()})
        return v

    _SELECT = """SELECT v.source, v.id, v.title, v.channel, v.channel_id, v.duration, v.thumb {extra}
                 FROM {table} JOIN videos v ON v.source=t.source AND v.id=t.id"""

    # --- история ---
    def add_history(self, item: dict, when: float | None = None) -> None:
        v = self.upsert_video(item)
        self._x("INSERT INTO history(source,id,watched) VALUES(?,?,?) "
                "ON CONFLICT(source,id) DO UPDATE SET watched=MAX(history.watched, excluded.watched)",
                (v["source"], v["id"], when or time.time()))

    def trim_history(self) -> None:
        self._x("DELETE FROM history WHERE rowid NOT IN (SELECT rowid FROM history ORDER BY watched DESC LIMIT ?)",
                (HISTORY_LIMIT,))

    def history(self, limit: int = 200) -> list[dict]:
        return self._q(self._SELECT.format(extra=", t.watched", table="history t") +
                       " ORDER BY t.watched DESC LIMIT ?", (limit,))

    def clear_history(self) -> None:
        self._x("DELETE FROM history")

    # --- «Смотреть позже» и «Избранное» ---
    def set_mark(self, kind: str, item: dict, on: bool) -> bool:
        if kind not in ("later", "favorite"):
            raise ValueError("Неизвестный список")
        v = self.upsert_video(item) if on else clean_item(item)
        if on:
            self._x("INSERT OR IGNORE INTO marks(kind,source,id,added) VALUES(?,?,?,?)", (kind, v["source"], v["id"], time.time()))
        else:
            self._x("DELETE FROM marks WHERE kind=? AND source=? AND id=?", (kind, v["source"], v["id"]))
        return on

    def marks(self, kind: str, limit: int = 500) -> list[dict]:
        return self._q(self._SELECT.format(extra="", table="marks t") +
                       " WHERE t.kind=? ORDER BY t.added DESC LIMIT ?", (kind, limit))

    def marks_of(self, source: str, vid: str) -> list[str]:
        return [r["kind"] for r in self._q("SELECT kind FROM marks WHERE source=? AND id=?", (source, vid))]

    # --- плейлисты ---
    def create_playlist(self, name: object) -> int:
        title = validate.clean_text(name, 80)
        if not title:
            raise ValueError("Введите название плейлиста")
        existing = self._q("SELECT id FROM playlists WHERE name=?", (title,))
        if existing:
            return existing[0]["id"]
        return self._x("INSERT INTO playlists(name,created) VALUES(?,?)", (title, time.time()))

    def playlists(self) -> list[dict]:
        return self._q("""SELECT p.id, p.name, COUNT(i.id) AS count,
                          (SELECT v.thumb FROM playlist_items j JOIN videos v ON v.source=j.source AND v.id=j.id
                           WHERE j.playlist_id=p.id ORDER BY j.added DESC LIMIT 1) AS thumb
                          FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id=p.id
                          GROUP BY p.id ORDER BY p.created DESC""")

    def add_to_playlist(self, playlist_id: object, item: dict) -> None:
        if not isinstance(playlist_id, int) or not self._q("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)):
            raise ValueError("Плейлист не найден")
        v = self.upsert_video(item)
        self._x("INSERT OR IGNORE INTO playlist_items(playlist_id,source,id,added) VALUES(?,?,?,?)",
                (playlist_id, v["source"], v["id"], time.time()))

    def remove_from_playlist(self, playlist_id: int, source: str, vid: str) -> None:
        self._x("DELETE FROM playlist_items WHERE playlist_id=? AND source=? AND id=?", (playlist_id, source, vid))

    def delete_playlist(self, playlist_id: int) -> None:
        self._x("DELETE FROM playlists WHERE id=?", (playlist_id,))

    def playlist_items(self, playlist_id: int) -> list[dict]:
        return self._q(self._SELECT.format(extra="", table="playlist_items t") +
                       " WHERE t.playlist_id=? ORDER BY t.added DESC", (playlist_id,))

    # --- подписки ---
    def subscribe(self, channel_id: object, title: object) -> None:
        if not validate.is_channel_id(channel_id):
            raise ValueError("Некорректный канал")
        self._x("INSERT INTO subscriptions(channel_id,title,added) VALUES(?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title",
                (channel_id, validate.clean_text(title, 120) or channel_id, time.time()))

    def unsubscribe(self, channel_id: str) -> None:
        self._x("DELETE FROM subscriptions WHERE channel_id=?", (channel_id,))

    def subscriptions(self) -> list[dict]:
        return self._q("SELECT channel_id, title, added FROM subscriptions ORDER BY title COLLATE NOCASE")

    def is_subscribed(self, channel_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM subscriptions WHERE channel_id=?", (channel_id,)))

    # --- загрузки ---
    def add_download(self, item: dict, quality: str) -> int:
        v = self.upsert_video(item)
        return self._x("INSERT INTO downloads(source,vid,quality,status,created) VALUES(?,?,?,?,?)",
                       (v["source"], v["id"], quality, "queued", time.time()))

    def update_download(self, dl_id: int, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("status", "progress", "path", "error")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)  # имена колонок из белого списка выше
        self._x(f"UPDATE downloads SET {sets} WHERE id=?", (*allowed.values(), dl_id))

    def downloads(self) -> list[dict]:
        return self._q("""SELECT d.id AS dl_id, d.status, d.progress, d.path, d.error, d.quality, d.created,
                          v.source, v.id, v.title, v.channel, v.channel_id, v.duration, v.thumb
                          FROM downloads d JOIN videos v ON v.source=d.source AND v.id=d.vid
                          ORDER BY d.created DESC LIMIT 300""")

    def get_download(self, dl_id: int) -> dict | None:
        rows = self._q("SELECT * FROM downloads WHERE id=?", (dl_id,))
        return rows[0] if rows else None

    def delete_download(self, dl_id: int) -> None:
        self._x("DELETE FROM downloads WHERE id=?", (dl_id,))

    def fail_unfinished_downloads(self) -> None:
        self._x("UPDATE downloads SET status='error', error='Прервано при закрытии программы' "
                "WHERE status IN ('queued','running')")

    def counts(self) -> dict:
        one = lambda sql, a=(): self._q(sql, a)[0]["n"]  # noqa: E731
        return {
            "history": one("SELECT COUNT(*) n FROM history"),
            "later": one("SELECT COUNT(*) n FROM marks WHERE kind='later'"),
            "favorite": one("SELECT COUNT(*) n FROM marks WHERE kind='favorite'"),
            "playlists": one("SELECT COUNT(*) n FROM playlists"),
            "subscriptions": one("SELECT COUNT(*) n FROM subscriptions"),
            "downloads": one("SELECT COUNT(*) n FROM downloads WHERE status IN ('queued','running')"),
        }
