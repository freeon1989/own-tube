# -*- coding: utf-8 -*-
"""«Проверить связь» по шагам: TCP до прокси, рукопожатие и логин, HTTPS до YouTube, картинки, поиск yt-dlp."""
from __future__ import annotations

import socket
import time
from typing import Callable
from urllib.parse import urlsplit

from urllib.parse import parse_qs

from . import bridge, httpc, streamsrc, validate
from .network import Route

TEST_THUMB = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"


def _step(name: str, fn: Callable[[], str]) -> dict:
    t = time.monotonic()
    try:
        detail = fn() or "OK"
        ok = True
    except Exception as exc:
        ok = False
        detail = validate.redact(str(exc) or type(exc).__name__)[:300]
    return {"name": name, "ok": ok, "detail": detail, "ms": int((time.monotonic() - t) * 1000)}


def probe_stream(st: dict, route: Route) -> str:
    """Читает первые 64 КБ настоящего потока (Range) теми же заголовками, что у плеера. Возвращает описание или бросает ValueError."""
    url = st["video"]
    host = urlsplit(url).hostname or "?"
    ipq = (parse_qs(urlsplit(url).query).get("ip") or [""])[0]
    fam = "IPv6" if ":" in ipq else ("IPv4" if ipq else "?")
    hdr = {"Range": "bytes=0-65535", "Accept": "*/*"}
    if st.get("user_agent"):
        hdr["User-Agent"] = st["user_agent"]
    if st.get("referer"):
        hdr["Referer"] = st["referer"]
    t = time.monotonic()
    tries = 0
    while True:
        tries += 1
        status, _, data = httpc.fetch("GET", url, route, hdr, limit=256 * 1024, timeout=25)
        if status != 403 or tries >= 4:
            break
        time.sleep(1.0)  # плеер тоже повторяет 403: соседний выход прокси/шлюза может совпасть с IP выдачи
    ms = int((time.monotonic() - t) * 1000)
    how = bridge.LAST.get(host) or {}
    atyp = {"domain": "имя хоста (ATYP 3)", "ipv4": "IPv4-адрес (ATYP 1)"}.get(how.get("atyp"), "напрямую")
    if status not in (200, 206) or not data:
        hint = (" Адрес потока привязан к IP, с которого его выдали: если выдача шла по IPv6, а прокси ходит к "
                "googlevideo по IPv4 (или наоборот), сервер отвечает 403.") if status == 403 else ""
        raise ValueError(f"{host}: HTTP {status}, {len(data)} байт; соединение: {atyp}; адрес выдан для {fam}.{hint}")
    again = f", с попытки {tries}" if tries > 1 else ""
    return (f"{host}: HTTP {status}, получено {len(data) // 1024} КБ за {ms} мс{again}; "
            f"соединение с прокси: {atyp}; адрес потока выдан для {fam}")


LEVELS = ((25, "4K"), (10, "1440p"), (6, "1080p"), (3.5, "720p"), (1.6, "480p"), (0.9, "360p"))


def _need_note(mbit: float) -> str:
    for need, name in LEVELS:
        if mbit >= need * 1.5:
            return f"хватит для {name}" + (" и ниже" if name != "360p" else "")
    return "не хватает даже для 360p без остановок"


def _one_stream(st: dict, route: Route, secs: float, limit: int) -> tuple[float, int]:
    """Как читал ffmpeg раньше: один длинный запрос на одном соединении. (Мбит/с, байт)."""
    hdr = {"Range": "bytes=0-", "Accept": "*/*"}
    hdr.update(streamsrc.source_headers(st))
    t0 = time.monotonic()
    resp = httpc.open_url("GET", st["video"], route, hdr, timeout=20)
    got = 0
    try:
        if resp.status not in (200, 206):
            raise ValueError(f"HTTP {resp.status}")
        while time.monotonic() - t0 < secs and got < limit:
            b = resp.read(65536)
            if not b:
                break
            got += len(b)
    finally:
        resp.close()
        conn = getattr(resp, "_owntube_conn", None)
        if conn is not None:
            conn.close()
    return got * 8 / max(time.monotonic() - t0, 1e-6) / 1e6, got


def _parallel(st: dict, route: Route, secs: float, limit: int, parallel: int = 6) -> tuple[float, int]:
    """Как читает плеер теперь: короткие запросы по 0,5 МБ, несколько одновременно. (Мбит/с, байт)."""
    src = streamsrc.Source(st["video"], lambda: route, streamsrc.source_headers(st), lambda: 16 * streamsrc.MB, "test",
                           parallel=parallel)
    t0 = time.monotonic()
    pos = got = 0
    try:
        total = src.size(20)
        while time.monotonic() - t0 < secs and got < limit and pos < total:
            data = src.read(pos, 256 * 1024, timeout=20)
            if not data:
                break
            pos += len(data)
            got += len(data)
    finally:
        src.close()
    return got * 8 / max(time.monotonic() - t0, 1e-6) / 1e6, got


def video_speed(st: dict, route: Route, secs: float = 6.0, limit: int = 24 * 1024 * 1024) -> str:
    """Скорость загрузки настоящего видео через текущий маршрут: один поток и несколько параллельных."""
    host = urlsplit(st["video"]).hostname or "?"
    one, n1 = _one_stream(st, route, secs, limit)
    many, n2 = _parallel(st, route, secs, limit)
    if n1 < 64 * 1024 and n2 < 64 * 1024:
        raise ValueError(f"{host}: данные не пришли")
    best = max(one, many)
    gain = f", параллельная загрузка быстрее в {many / one:.1f} раза" if one > 0.1 and many > one * 1.25 else ""
    return (f"{host}: один поток {one:.1f} Мбит/с, несколько запросов сразу {many:.1f} Мбит/с{gain}. "
            f"Через «{route.label}» {_need_note(best)}.")


def run(route: Route, search_fn=None, video_fn=None, pipe_fn=None) -> dict:
    steps: list[dict] = []
    if not route.direct:
        p = urlsplit(route.proxy)

        def tcp():
            s = socket.create_connection((p.hostname, p.port), timeout=8)
            s.close()
            return "Сервер прокси отвечает"

        steps.append(_step("Соединение с прокси (TCP)", tcp))
        if steps[-1]["ok"]:
            def handshake():
                s = httpc.open_tunnel("www.youtube.com", 443, route, 10)
                s.close()
                kind = "SOCKS5" if validate.is_socks_proxy(route.proxy) else "HTTP CONNECT"
                return f"{kind}: " + ("логин и пароль приняты, " if route.user or route.password else "") + \
                       "туннель до www.youtube.com открыт"
            steps.append(_step("Рукопожатие и вход на прокси", handshake))
    if all(s["ok"] for s in steps):
        def yt():
            status, _, _ = httpc.get("https://www.youtube.com/robots.txt", route, limit=64 * 1024, timeout=15)
            if status >= 400:
                raise ValueError(f"youtube.com ответил кодом {status}")
            return f"youtube.com отвечает (HTTP {status}) " + ("через прокси" if not route.direct else "напрямую")
        steps.append(_step("Доступ к youtube.com (HTTPS)", yt))

        def thumb():
            status, _, data = httpc.get(TEST_THUMB, route, limit=512 * 1024, timeout=15)
            if status != 200 or not data:
                raise ValueError(f"i.ytimg.com ответил кодом {status}")
            return f"Обложки грузятся ({len(data) // 1024} КБ)"
        steps.append(_step("Обложки (i.ytimg.com)", thumb))

        if search_fn is not None:
            def search():
                got = search_fn("lofi", route, 3)
                items, engine = got if isinstance(got, tuple) else (got, "yt-dlp")
                if not items:
                    raise ValueError("Поиск вернул пустой список")
                return f"Поиск работает ({engine}): {len(items)} результата"
            steps.append(_step("Поиск", search))

        if video_fn is not None:
            holder: dict = {}

            def resolve():
                holder["streams"] = video_fn(route)
                st = holder["streams"]
                host = urlsplit(st["video"]).hostname or "?"
                return f"Адрес потока получен ({host}, {st.get('height') or '?'}p, {st.get('container')})"
            steps.append(_step("Проверка видео: адрес потока", resolve))

            if steps[-1]["ok"]:
                def fetch():
                    try:
                        return probe_stream(holder["streams"], route)
                    except ValueError as first:
                        # так же делает плеер: если основной формат отдал 403, берёт другой кодек (H.264/MP4)
                        try:
                            alt = probe_stream(video_fn(route, "h264"), route)
                        except Exception:
                            raise first
                        return alt + " — основной формат (VP9) ответил 403, запасной H.264 открылся: плеер переключится сам"
                steps.append(_step("Проверка видео: первые байты потока", fetch))

                fetch_ok = steps[-1]["ok"]
                if fetch_ok:
                    steps.append(_step("Скорость видео", lambda: video_speed(holder["streams"], route)))

                if pipe_fn is not None and fetch_ok:
                    def pipe():
                        t = time.monotonic()
                        n = pipe_fn(holder["streams"])
                        if n <= 0:
                            raise ValueError("Встроенный плеер не получил данных")
                        return f"Встроенный плеер (ffmpeg) отдал {n // 1024} КБ за {int((time.monotonic() - t) * 1000)} мс"
                    steps.append(_step("Проверка видео: встроенный плеер", pipe))
    ok = bool(steps) and all(s["ok"] for s in steps)
    first_bad = next((s for s in steps if not s["ok"]), None)
    detail = ("Всё в порядке: " + route.label) if ok else (f"Сбой на шаге «{first_bad['name']}»: {first_bad['detail']}"
                                                           if first_bad else "Нет шагов")
    return {"ok": ok, "detail": detail, "steps": steps}
