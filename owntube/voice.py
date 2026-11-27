# -*- coding: utf-8 -*-
"""Голос: распознавание речи (микрофон → текст) для чата с помощницей.

Озвучка ответов — в самом окне (speechSynthesis WebView2), здесь только распознавание:
1) API активной конфигурации ИИ, если оно поддерживает POST /audio/transcriptions (OpenAI-совместимое);
2) локальный whisper.cpp (whisper-cli.exe + модель ggml-base.bin) — ставится кнопкой в настройках в папку
   программы или в выбранную папку (например E:\\AI\\whisper). Файлы скачиваются только с GitHub/Hugging Face,
   проверяются по SHA-256 и ничего не запускают при установке.
Запись с микрофона приходит из окна как WebM/Opus; ffmpeg превращает её в WAV 16 кГц моно. Файлы временные."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import httpc, validate
from .network import Route

log = logging.getLogger("owntube.voice")

MAX_AUDIO = 12 * 1024 * 1024
WHISPER_VERSION = "v1.8.2"
WHISPER_ZIP = {
    "url": "https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.2/whisper-bin-x64.zip",
    "size": 3_832_432,
    "sha256": "b1514ebc099765e39fa37eb780b92a140a94c86bb0b3b3d98226b38825979732",
    "files": ("whisper-cli.exe", "whisper.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu.dll"),
}
WHISPER_MODEL = {
    "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
    "size": 147_951_465,
    "sha256": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    "name": "ggml-base.bin",
}
ALLOWED_DL_SUFFIXES = ("github.com", "githubusercontent.com", "huggingface.co", "hf.co")
CLI = "whisper-cli.exe" if sys.platform.startswith("win") else "whisper-cli"


class VoiceError(Exception):
    """Текст ошибки можно показывать пользователю."""


# ---------- где лежит whisper ----------
def default_dir(data: Path) -> Path:
    return Path(data) / "whisper"


def local_paths(directory: str | Path) -> tuple[Path, Path]:
    d = Path(directory)
    return d / CLI, d / WHISPER_MODEL["name"]


def local_ready(directory: str | Path) -> bool:
    cli, model = local_paths(directory)
    return cli.is_file() and model.is_file() and model.stat().st_size > 1_000_000


# ---------- аудио -> wav ----------
def _flags() -> int:
    return 0x08000000 if sys.platform.startswith("win") else 0


def to_wav(ffmpeg: str, audio: bytes, workdir: Path) -> Path:
    """WebM/Opus из окна -> WAV 16 кГц моно во временном файле (удаляет вызывающий)."""
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / f"in-{uuid.uuid4().hex}.webm"
    dst = workdir / f"out-{uuid.uuid4().hex}.wav"
    src.write_bytes(audio)
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
                            "-f", "wav", str(dst)], capture_output=True, timeout=60, creationflags=_flags(), shell=False)
        if r.returncode != 0 or not dst.is_file() or dst.stat().st_size < 1000:
            tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg не смог прочитать запись"]
            raise VoiceError("Не удалось обработать запись с микрофона: " + validate.redact(tail[0])[:160])
        return dst
    except subprocess.TimeoutExpired as exc:
        raise VoiceError("Обработка записи заняла слишком много времени") from exc
    finally:
        try:
            src.unlink()
        except OSError:
            pass


# ---------- распознавание ----------
def transcribe_api(base_url: str, api_key: str, wav: Path, model: str, lang: str, route: Route) -> str:
    from .ai.providers import kind_of

    if kind_of(base_url) != "openai":
        raise VoiceError("Эта конфигурация (Anthropic/Gemini) не умеет распознавать речь по API")
    boundary = "----owntube" + uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

    field("model", model)
    field("language", lang)
    field("response_format", "json")
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                  "Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    url = base_url.rstrip("/") + "/audio/transcriptions"
    try:
        status, _, data = httpc.fetch("POST", url, route, headers, b"".join(parts), limit=1024 * 1024, timeout=90, attempts=2)
    except Exception as exc:
        raise VoiceError("API распознавания недоступно: " + validate.redact(str(exc).replace(api_key, "***") if api_key else str(exc))[:160]) from exc
    if status in (404, 405, 501):
        raise VoiceError("Этот API не поддерживает распознавание речи (нет /audio/transcriptions)")
    if status in (401, 403):
        raise VoiceError("API отклонил ключ при распознавании речи")
    if status != 200:
        raise VoiceError(f"API распознавания ответило кодом {status}")
    try:
        text = json.loads(data.decode("utf-8", "replace")).get("text", "")
    except ValueError as exc:
        raise VoiceError("API распознавания вернуло не JSON") from exc
    return validate.clean_text(text, 2000)


def transcribe_local(directory: str | Path, wav: Path, lang: str, command: list[str] | None = None) -> str:
    cli, model = local_paths(directory)
    cmd = command or [str(cli), "-m", str(model), "-f", str(wav), "-l", lang, "-nt", "-np", "-t", str(min(8, os.cpu_count() or 4))]
    if not command and not local_ready(directory):
        raise VoiceError("Локальное распознавание не установлено (Настройки → Голос → Установить)")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=_flags(), shell=False, cwd=str(cli.parent) if cli.parent.is_dir() else None)
    except subprocess.TimeoutExpired as exc:
        raise VoiceError("Распознавание заняло слишком много времени") from exc
    except OSError as exc:
        raise VoiceError("whisper не запустился: " + str(exc)[:120]) from exc
    if r.returncode != 0:
        tail = (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["ошибка"])[-1]
        raise VoiceError("whisper завершился с ошибкой: " + validate.redact(tail)[:160])
    text = " ".join(line.strip() for line in r.stdout.decode("utf-8", "replace").splitlines() if line.strip() and not line.startswith("["))
    return validate.clean_text(text, 2000)


# ---------- установка whisper.cpp ----------
def _host_ok(url: str) -> bool:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and not p.username and any(host == s or host.endswith("." + s) for s in ALLOWED_DL_SUFFIXES)


def download(url: str, dest: Path, size: int, sha256: str, route: Route, progress, attempts: int = 3) -> None:
    """Скачивает с проверкой хоста (в том числе после редиректов), размера и SHA-256. Возобновления нет: файл целиком."""
    last: Exception | None = None
    for attempt in range(attempts):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            cur = url
            for _ in range(6):
                if not _host_ok(cur):
                    raise VoiceError("Адрес скачивания не из списка разрешённых")
                resp = httpc.open_url("GET", cur, route, {"Accept": "*/*"}, timeout=60)
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location") or ""
                    resp.read(0)
                    resp.close()
                    cur = urljoin(cur, loc)
                    continue
                break
            else:
                raise VoiceError("Слишком много перенаправлений")
            try:
                if resp.status != 200:
                    raise VoiceError(f"Сервер скачивания ответил кодом {resp.status}")
                h = hashlib.sha256()
                got = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > size + 1024:
                            raise VoiceError("Файл больше ожидаемого")
                        h.update(chunk)
                        f.write(chunk)
                        progress(got, size)
            finally:
                resp.close()
            if got != size or h.hexdigest() != sha256:
                raise VoiceError("Скачанный файл не совпал с ожидаемым (размер или SHA-256)")
            os.replace(tmp, dest)
            return
        except (OSError, VoiceError) as exc:
            last = exc
            try:
                tmp.unlink()
            except OSError:
                pass
            if isinstance(exc, VoiceError) and "не совпал" not in str(exc) and "Сервер" not in str(exc):
                break
            time.sleep(1.5 * (attempt + 1))
    raise last if isinstance(last, VoiceError) else VoiceError("Не удалось скачать: " + validate.redact(str(last))[:160])


def install_whisper(directory: str | Path, route: Route, progress) -> None:
    """progress(stage, done, total). Ставит whisper-cli.exe, библиотеки и модель в directory."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    cli, model = local_paths(d)
    if not cli.is_file():
        zpath = d / "whisper-bin-x64.zip"
        download(WHISPER_ZIP["url"], zpath, WHISPER_ZIP["size"], WHISPER_ZIP["sha256"], route,
                 lambda a, b: progress("программа", a, b))
        try:
            with zipfile.ZipFile(zpath) as z:
                names = {Path(n).name: n for n in z.namelist()}
                for want in WHISPER_ZIP["files"]:
                    if want not in names:
                        raise VoiceError(f"В архиве нет {want}")
                    with z.open(names[want]) as src, open(d / want, "wb") as out:
                        out.write(src.read(40 * 1024 * 1024))
        finally:
            try:
                zpath.unlink()
            except OSError:
                pass
    if not model.is_file() or model.stat().st_size != WHISPER_MODEL["size"]:
        download(WHISPER_MODEL["url"], model, WHISPER_MODEL["size"], WHISPER_MODEL["sha256"], route,
                 lambda a, b: progress("модель", a, b))
    log.info("whisper.cpp %s установлен в %s", WHISPER_VERSION, d)


class Installer:
    """Одна установка за раз; прогресс читает интерфейс."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "stage": "", "done": 0, "total": 0, "error": "", "finished": False}

    def start(self, directory, route: Route) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "stage": "подготовка", "done": 0, "total": 0, "error": "", "finished": False}

        def prog(stage, a, b):
            with self.lock:
                self.state.update(stage=stage, done=a, total=b)

        def run():
            err = ""
            try:
                install_whisper(directory, route, prog)
            except VoiceError as exc:
                err = str(exc)
            except Exception as exc:
                err = "Установка не удалась: " + validate.redact(str(exc))[:160]
                log.warning(err)
            with self.lock:
                self.state.update(running=False, error=err, finished=not err)

        threading.Thread(target=run, name="owntube-whisper", daemon=True).start()
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


def temp_dir(data: Path) -> Path:
    d = Path(tempfile.gettempdir()) if not data else Path(data) / "voice-tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d
