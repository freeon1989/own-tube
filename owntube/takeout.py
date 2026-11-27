# -*- coding: utf-8 -*-
"""Импорт из Google Takeout (YouTube): подписки, история просмотра, плейлисты.

Принимает архив .zip целиком или отдельные файлы subscriptions.csv / watch-history.json / *.csv плейлистов.
Архив не распаковывается на диск: читаются только нужные файлы, с ограничением размера.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from . import validate
from .db import Store

MAX_MEMBER = 300 * 1024 * 1024
UC_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
TITLE_PREFIXES = ("Watched ", "Вы посмотрели ", "Просмотрено: ", "Просмотрено ")


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def parse_subscriptions(text: str) -> list[tuple[str, str]]:
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        cid = next((c.strip() for c in row if UC_RE.fullmatch(c.strip())), None)
        if not cid:
            continue
        title = row[2].strip() if len(row) > 2 else cid
        out.append((cid, title))
    return out


def parse_history(text: str) -> list[dict]:
    data = json.loads(text)
    out = []
    if not isinstance(data, list):
        return out
    for e in data:
        if not isinstance(e, dict):
            continue
        url = e.get("titleUrl")
        if not isinstance(url, str):
            continue
        q = parse_qs(urlsplit(url).query)
        vid = (q.get("v") or [""])[0]
        if not validate.is_video_id(vid):
            continue
        title = e.get("title") if isinstance(e.get("title"), str) else ""
        for p in TITLE_PREFIXES:
            if title.startswith(p):
                title = title[len(p):]
                break
        channel = channel_id = ""
        subs = e.get("subtitles")
        if isinstance(subs, list) and subs and isinstance(subs[0], dict):
            channel = subs[0].get("name") or ""
            m = UC_RE.search(str(subs[0].get("url") or ""))
            channel_id = m.group(0) if m else ""
        when = None
        try:
            when = datetime.fromisoformat(str(e.get("time", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        out.append({"source": "yt", "id": vid, "title": title, "channel": channel,
                    "channel_id": channel_id, "when": when})
    return out


def parse_playlist(text: str) -> list[str]:
    ids = []
    for row in csv.reader(io.StringIO(text)):
        if row and validate.is_video_id(row[0].strip()):
            ids.append(row[0].strip())
    return ids


def _playlist_name(name: str) -> str | None:
    stem = PurePosixPath(name).stem
    for suffix in ("-videos", "-видео"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)] or None
    return None


def _classify(name: str) -> str | None:
    low = name.lower().replace("\\", "/")
    base = low.rsplit("/", 1)[-1]
    if base.endswith(".csv") and ("subscriptions" in base or "подписки" in base):
        return "subs"
    if base.endswith(".json") and ("watch-history" in base or "история просмотра" in base):
        return "history"
    if base.endswith(".csv") and _playlist_name(name):
        return "playlist"
    return None


def import_path(path: Path, store: Store) -> dict:
    """Импортирует и возвращает сводку: сколько подписок, записей истории, плейлистов и видео в них."""
    files: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or not _classify(info.filename):
                    continue
                if info.file_size > MAX_MEMBER:
                    raise ValueError(f"Слишком большой файл в архиве: {info.filename}")
                files.append((info.filename, zf.read(info)))
    else:
        if path.stat().st_size > MAX_MEMBER:
            raise ValueError("Слишком большой файл")
        files.append((path.name, path.read_bytes()))
    summary = {"subscriptions": 0, "history": 0, "playlists": 0, "playlist_videos": 0, "files": 0}
    for name, data in files:
        kind = _classify(name)
        if kind is None:
            continue
        summary["files"] += 1
        text = _decode(data)
        if kind == "subs":
            for cid, title in parse_subscriptions(text):
                store.subscribe(cid, title)
                summary["subscriptions"] += 1
        elif kind == "history":
            for e in parse_history(text):
                store.add_history(e, e.pop("when"))
                summary["history"] += 1
            store.trim_history()
        elif kind == "playlist":
            ids = parse_playlist(text)
            if not ids:
                continue
            pid = store.create_playlist(_playlist_name(name))
            summary["playlists"] += 1
            for vid in ids:
                store.add_to_playlist(pid, {"source": "yt", "id": vid, "title": "Видео из Takeout"})
                summary["playlist_videos"] += 1
    if not summary["files"]:
        raise ValueError("В файле не нашлось данных YouTube из Takeout (подписки, история, плейлисты)")
    return summary
