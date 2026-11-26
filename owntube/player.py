# -*- coding: utf-8 -*-
"""Запуск воспроизведения: yt-dlp достаёт адреса потоков, mpv играет.

mpv запускается отдельным процессом со списком аргументов (без shell).
Собственный ytdl-хук mpv отключён: всё, что идёт в сеть, идёт через наш yt-dlp и наш прокси.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import validate
from .network import Route, RouteError

log = logging.getLogger("owntube.player")

FORMATS = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
}


class PlayerError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


def find_mpv(configured: str = "") -> str:
    candidates = []
    if configured:
        candidates.append(configured)
    exe = "mpv.exe" if sys.platform.startswith("win") else "mpv"
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    candidates += [str(base / "mpv" / exe), str(Path(sys.executable).resolve().parent / "mpv" / exe)]
    for c in candidates:
        p = Path(c)
        if p.is_file() and p.name.lower().startswith("mpv") and os.access(p, os.X_OK):
            return str(p)
    found = shutil.which("mpv")
    if found:
        return found
    raise PlayerError("Не найден mpv. Положите mpv.exe в папку mpv рядом с программой "
                      "или укажите путь к нему в настройках.")


def resolve_streams(video_id: str, quality: str, route: Route = Route()) -> dict:
    """Возвращает {title, video, audio|None, user_agent}. Все адреса проверены."""
    import yt_dlp

    url = validate.watch_url(video_id)
    quality = validate.clean_quality(quality)
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
        "cachedir": False, "socket_timeout": 20, "retries": 2,
        "format": FORMATS[quality],
    }
    opts.update(route.ytdlp_options())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise PlayerError("YouTube не отдал видео: " + str(exc).replace("ERROR: ", "")[:300]) from exc
    return pick_streams(info or {})


def pick_streams(info: dict) -> dict:
    fmts = info.get("requested_formats") or [info]
    video = audio = None
    for f in fmts:
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        if has_v and video is None:
            video = f
        elif has_a and not has_v and audio is None:
            audio = f
    if video is None or not validate.is_stream_url(video.get("url")):
        raise PlayerError("Не удалось получить безопасный адрес потока")
    if audio is not None and not validate.is_stream_url(audio.get("url")):
        raise PlayerError("Не удалось получить безопасный адрес звука")
    headers = video.get("http_headers") or info.get("http_headers") or {}
    ua = headers.get("User-Agent")
    return {
        "title": str(info.get("title") or "own-tube")[:200],
        "video": video["url"],
        "audio": audio["url"] if audio else None,
        "user_agent": ua if isinstance(ua, str) and ua.isprintable() and len(ua) < 300 else None,
    }


def build_command(mpv: str, streams: dict, route: Route = Route()) -> list[str]:
    """Список аргументов для mpv. Адрес идёт после «--», чтобы не принять его за опцию."""
    cmd = [mpv, "--ytdl=no", "--force-window=immediate", "--keep-open=no",
           "--force-media-title=" + streams["title"], "--no-terminal"]
    if streams.get("user_agent"):
        cmd.append("--user-agent=" + streams["user_agent"])
    try:
        cmd += route.mpv_args()
    except RouteError as exc:
        raise PlayerError(str(exc)) from exc
    if streams.get("audio"):
        cmd.append("--audio-file=" + streams["audio"])
    cmd += ["--", streams["video"]]
    return cmd


class Player:
    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def play(self, video_id: str, quality: str, route: Route, mpv_path: str = "") -> None:
        mpv = find_mpv(mpv_path)
        streams = resolve_streams(video_id, quality, route)
        cmd = build_command(mpv, streams, route)
        self.stop()
        log.info("Запускаю mpv для %s (%s)", video_id, quality)
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, shell=False)
        except OSError as exc:
            raise PlayerError(f"Не удалось запустить mpv: {exc.strerror or exc}") from exc

    def stop(self) -> None:
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.terminate()
