# -*- coding: utf-8 -*-
"""Загрузки: очередь в отдельном потоке, yt-dlp + ffmpeg (склейка видео и звука).

Файлы пишутся только в выбранную папку загрузок; имя файла строит yt-dlp с безопасными для Windows символами.
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Callable

from . import validate, ytdl
from .db import Store
from .network import Route
from .player import format_selector
from .tools import find_tool

log = logging.getLogger("owntube.downloads")


class Cancelled(Exception):
    pass


class Downloader:
    def __init__(self, store: Store, settings: Callable, route: Callable[[], Route]):
        self._store = store
        self._settings = settings
        self._route = route
        self._q: queue.Queue[int] = queue.Queue()
        self._cancel: set[int] = set()
        self._progress: dict[int, float] = {}
        store.fail_unfinished_downloads()
        threading.Thread(target=self._worker, name="owntube-downloads", daemon=True).start()

    def enqueue(self, item: dict, quality: str) -> int:
        quality = validate.clean_quality(quality)
        dl_id = self._store.add_download(item, quality)
        self._q.put(dl_id)
        return dl_id

    def cancel(self, dl_id: int) -> None:
        self._cancel.add(dl_id)
        row = self._store.get_download(dl_id)
        if row and row["status"] == "queued":
            self._store.update_download(dl_id, status="cancelled")

    def live_progress(self) -> dict[int, float]:
        return dict(self._progress)

    def _worker(self) -> None:
        while True:
            dl_id = self._q.get()
            try:
                self._run(dl_id)
            except Exception:
                log.exception("Загрузка %s", dl_id)
            finally:
                self._progress.pop(dl_id, None)
                self._cancel.discard(dl_id)

    def _run(self, dl_id: int) -> None:
        import yt_dlp

        row = self._store.get_download(dl_id)
        if not row or row["status"] != "queued" or dl_id in self._cancel:
            return
        s = self._settings()
        out_dir = Path(s.download_dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._store.update_download(dl_id, status="error", error=f"Папка загрузок недоступна: {exc}")
            return
        ffmpeg = find_tool("ffmpeg")
        fmt = format_selector(row["quality"], s.codec)
        if not ffmpeg:
            fmt = "b"  # без ffmpeg склеить видео и звук нельзя — берём готовый файл
        self._store.update_download(dl_id, status="running", progress=0)

        def hook(d):
            if dl_id in self._cancel:
                raise Cancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    self._progress[dl_id] = min(99.0, 100.0 * d.get("downloaded_bytes", 0) / total)

        opts = ytdl.base_options(
            self._route(), skip_download=False, format=fmt,
            outtmpl={"default": str(out_dir / "%(title).90B [%(id)s].%(ext)s")},
            windowsfilenames=True, restrictfilenames=False, overwrites=False, continuedl=True,
            progress_hooks=[hook], merge_output_format="mp4/mkv",
        )
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(validate.item_url(row["source"], row["vid"]), download=True)
            path = ""
            for r in (info or {}).get("requested_downloads") or []:
                path = r.get("filepath") or path
            self._store.update_download(dl_id, status="done", progress=100, path=path)
        except Cancelled:
            self._store.update_download(dl_id, status="cancelled")
        except Exception as exc:
            if dl_id in self._cancel:
                self._store.update_download(dl_id, status="cancelled")
            else:
                self._store.update_download(dl_id, status="error", error=str(exc).replace("ERROR: ", "")[:300])
