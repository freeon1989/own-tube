# -*- coding: utf-8 -*-
"""Запуск воспроизведения: yt-dlp достаёт адреса потоков, mpv играет.

mpv запускается отдельным процессом со списком аргументов (без shell).
Собственный ytdl-хук mpv отключён: всё, что идёт в сеть, идёт через наш yt-dlp и наш маршрут.
"""
from __future__ import annotations

import logging
import subprocess
import sys

from . import validate, ytdl
from .network import Route, RouteError
from .tools import find_tool, resource_dir

log = logging.getLogger("owntube.player")
SKIP_SCRIPT = resource_dir() / "owntube" / "mpv" / "owntube_skip.lua"


class PlayerError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


def format_selector(quality: str, codec: str = "auto") -> str:
    quality = validate.clean_quality(quality)
    codec = validate.clean_choice(codec, validate.CODECS, "кодек")
    h = "" if quality == "best" else f"[height<={quality}]"
    plain = f"bv*{h}+ba/b{h}/b"
    if codec == "h264":
        # H.264 декодируется аппаратно почти везде; если его нет — берём что есть
        return f"bv*{h}[vcodec^=avc1]+ba[acodec^=mp4a]/bv*{h}[vcodec^=avc1]+ba/b{h}[vcodec^=avc1]/" + plain
    return plain


def find_mpv(configured: str = "") -> str:
    found = find_tool("mpv", configured)
    if not found:
        raise PlayerError("Не найден mpv. Он лежит в папке tools рядом с программой; "
                          "или укажите путь к mpv.exe в настройках.")
    return found


def resolve_streams(source: str, item_id: str, quality: str, route: Route = Route(), codec: str = "auto") -> dict:
    """Возвращает {title, video, audio|None, user_agent, referer}. Все адреса проверены."""
    import yt_dlp

    url = validate.item_url(source, item_id)
    opts = ytdl.base_options(route, format=format_selector(quality, codec))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise PlayerError("Видео не открылось: " + str(exc).replace("ERROR: ", "")[:300]) from exc
    return pick_streams(info or {}, source)


def pick_streams(info: dict, source: str = "yt") -> dict:
    fmts = info.get("requested_formats") or [info]
    video = audio = None
    for f in fmts:
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        if has_v and video is None:
            video = f
        elif has_a and not has_v and audio is None:
            audio = f
    if video is None and fmts and fmts[0].get("url"):
        video = fmts[0]  # у части форматов VK кодек не указан
    if video is None or not validate.is_stream_url(video.get("url"), source):
        raise PlayerError("Не удалось получить безопасный адрес потока")
    if audio is not None and not validate.is_stream_url(audio.get("url"), source):
        raise PlayerError("Не удалось получить безопасный адрес звука")
    headers = video.get("http_headers") or info.get("http_headers") or {}
    ua = headers.get("User-Agent")
    ref = headers.get("Referer")
    ok = lambda v, n: isinstance(v, str) and v.isprintable() and len(v) < n and "," not in v  # noqa: E731
    return {
        "title": validate.clean_text(info.get("title"), 200) or "own-tube",
        "video": video["url"],
        "audio": audio["url"] if audio else None,
        "user_agent": ua if ok(ua, 300) else None,
        "referer": ref if ok(ref, 300) and ref.startswith("https://") else None,
    }


def build_command(mpv: str, streams: dict, route: Route = Route(), segments: str = "") -> list[str]:
    """Список аргументов для mpv. Адрес идёт после «--», чтобы не принять его за опцию."""
    cmd = [mpv, "--ytdl=no", "--force-window=immediate", "--keep-open=no",
           "--force-media-title=" + streams["title"], "--no-terminal"]
    if streams.get("user_agent"):
        cmd.append("--user-agent=" + streams["user_agent"])
    if streams.get("referer"):
        cmd.append("--referrer=" + streams["referer"])
    try:
        cmd += route.mpv_args()
    except RouteError as exc:
        raise PlayerError(str(exc)) from exc
    if segments and SKIP_SCRIPT.is_file():
        cmd += ["--script=" + str(SKIP_SCRIPT), "--script-opts=owntube_segments=" + segments.replace(",", "+")]
    if streams.get("audio"):
        cmd.append("--audio-file=" + streams["audio"])
    cmd += ["--", streams["video"]]
    return cmd


class Player:
    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def play(self, source: str, item_id: str, quality: str, route: Route, mpv_path: str = "",
             codec: str = "auto", segments: str = "") -> dict:
        mpv = find_mpv(mpv_path)
        streams = resolve_streams(source, item_id, quality, route, codec)
        cmd = build_command(mpv, streams, route, segments)
        self.stop()
        log.info("Запускаю mpv для %s:%s (%s, %s)", source, item_id, quality, codec)
        flags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW: без консоли
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, shell=False, creationflags=flags)
        except OSError as exc:
            raise PlayerError(f"Не удалось запустить mpv: {exc.strerror or exc}") from exc
        return {"title": streams["title"]}

    def stop(self) -> None:
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.terminate()
