# -*- coding: utf-8 -*-
"""Запуск воспроизведения: yt-dlp достаёт адреса потоков, mpv играет.

mpv запускается отдельным процессом со списком аргументов (без shell).
Собственный ytdl-хук mpv отключён: всё, что идёт в сеть, идёт через наш yt-dlp и наш маршрут.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

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


def embedded_selector(quality: str, codec: str = "auto") -> str:
    """Форматы, которые WebView2 (Chromium) играет без перекодирования: VP9+Opus в WebM или H.264+AAC в MP4.
    AV1 не берём: в Windows для него нужен отдельный кодек."""
    quality = validate.clean_quality(quality)
    codec = validate.clean_choice(codec, validate.CODECS, "кодек")
    h = "" if quality == "best" else f"[height<={quality}]"
    h264 = f"bv*{h}[vcodec^=avc1]+ba[acodec^=mp4a]/b{h}[vcodec^=avc1][acodec^=mp4a]/b[vcodec^=avc1]"
    if codec == "h264":
        return h264 + "/b"
    return f"bv*{h}[vcodec^=vp9]+ba[acodec=opus]/bv*{h}[vcodec^=vp09]+ba[acodec=opus]/" + h264 + "/b"


def container_for(vcodec: str | None, acodec: str | None) -> str:
    v = (vcodec or "").lower()
    a = (acodec or "").lower()
    if v.startswith(("vp9", "vp09", "vp8", "av01")) and (not a or a == "none" or a.startswith(("opus", "vorbis"))):
        return "webm"
    return "mp4"


def ffmpeg_command(ffmpeg: str, streams: dict, start: float = 0.0, http_proxy: str = "") -> list[str]:
    """Перепаковка без перекодирования в поток для <video>. Адреса проверены, процесс — без shell."""
    if not validate.is_stream_url(streams.get("video"), streams.get("source", "yt")):
        raise PlayerError("Небезопасный адрес потока")
    start = max(0.0, float(start or 0))
    common = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4"]
    if streams.get("user_agent"):
        common += ["-user_agent", streams["user_agent"]]
    if streams.get("referer"):
        common += ["-referer", streams["referer"]]
    if http_proxy:
        common += ["-http_proxy", http_proxy]
    seek = ["-ss", f"{start:.2f}"] if start > 0 else []
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    cmd += common + seek + ["-i", streams["video"]]
    if streams.get("audio"):
        if not validate.is_stream_url(streams["audio"], streams.get("source", "yt")):
            raise PlayerError("Небезопасный адрес звука")
        cmd += common + seek + ["-i", streams["audio"], "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-sn", "-dn"]
    if streams.get("container") == "webm":
        cmd += ["-f", "webm", "-cluster_time_limit", "2000", "-live", "1"]
    else:
        cmd += ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof"]
    return cmd + ["pipe:1"]


def find_mpv(configured: str = "") -> str:
    found = find_tool("mpv", configured)
    if not found:
        raise PlayerError("Не найден mpv. Он лежит в папке tools рядом с программой; "
                          "или укажите путь к mpv.exe в настройках.")
    return found


def alt_codec(codec: str) -> str:
    """Другой кодек для повторной попытки: если не открылся основной формат, пробуем H.264/MP4 (или авто)."""
    return "auto" if codec in ("h264", "avc") else "h264"


def resolve_streams(source: str, item_id: str, quality: str, route: Route = Route(), codec: str = "auto",
                    embedded: bool = False, ipv4: bool = False) -> dict:
    """Возвращает {title, video, audio|None, user_agent, referer, ...}. Все адреса проверены.

    ipv4=True: запрос идёт только по IPv4 (адрес потока привязан к IP выдачи; если IPv6 у компьютера не работает до серверов видео,
    адрес, выданный по IPv6, даёт 403)."""
    import yt_dlp

    url = validate.item_url(source, item_id)
    fmt = embedded_selector(quality, codec) if embedded else format_selector(quality, codec)
    opts = ytdl.base_options(route, format=fmt)
    if ipv4 and route.direct:
        opts["source_address"] = "0.0.0.0"
    try:
        with ytdl.YDL(opts) as ydl:
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
    # Запятая допустима («KHTML, like Gecko»): значение уходит отдельным аргументом, без shell и без списков опций
    ok = lambda v, n: isinstance(v, str) and v.isprintable() and len(v) < n and not v.startswith("-")  # noqa: E731
    dur = info.get("duration")
    vcodec = video.get("vcodec") if video.get("vcodec") not in (None, "none") else None
    acodec = (audio or {}).get("acodec") or (video.get("acodec") if video.get("acodec") not in (None, "none") else None)
    return {
        "title": validate.clean_text(info.get("title"), 200) or "own-tube",
        "channel": validate.clean_text(info.get("channel") or info.get("uploader"), 120),
        "channel_id": info.get("channel_id") if validate.is_channel_id(info.get("channel_id")) else "",
        "duration": float(dur) if isinstance(dur, (int, float)) and dur > 0 else 0.0,
        "height": video.get("height") if isinstance(video.get("height"), int) else None,
        "vcodec": vcodec, "acodec": acodec,
        "container": container_for(vcodec, acodec),
        "live": bool(info.get("is_live")),
        "source": source,
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


class StreamCache:
    """Готовые адреса потоков на короткое время: повторное открытие и «подготовка» следующего ролика мгновенны.

    Адреса YouTube привязаны к IP и живут часы; держим 20 минут. Параллельные запросы одного ролика ждут один разбор."""
    TTL = 20 * 60
    MAX = 40

    def __init__(self):
        self._d: dict[tuple, tuple[float, dict]] = {}
        self._busy: dict[tuple, threading.Event] = {}
        self._lock = threading.Lock()

    def peek(self, key: tuple) -> dict | None:
        with self._lock:
            hit = self._d.get(key)
            return dict(hit[1]) if hit and time.time() - hit[0] < self.TTL else None

    def get(self, key: tuple, producer, fresh: bool = False) -> tuple[dict, bool]:
        """(потоки, из_кэша)."""
        while True:
            with self._lock:
                hit = self._d.get(key)
                if hit and not fresh and time.time() - hit[0] < self.TTL:
                    return dict(hit[1]), True
                ev = self._busy.get(key)
                if ev is None:
                    self._busy[key] = threading.Event()
                    break
            ev.wait(60)
            fresh = False
        try:
            value = producer()
            with self._lock:
                self._d[key] = (time.time(), value)
                if len(self._d) > self.MAX:
                    for k, _ in sorted(self._d.items(), key=lambda kv: kv[1][0])[: len(self._d) - self.MAX]:
                        self._d.pop(k, None)
            return dict(value), False
        finally:
            with self._lock:
                self._busy.pop(key).set()

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
