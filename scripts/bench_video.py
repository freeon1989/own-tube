# -*- coding: utf-8 -*-
"""Замер старта и буферизации видео во встроенном плеере: прежний путь (ffmpeg сам ходит на адрес потока) против нового
(локальный источник с параллельной упреждающей загрузкой). Только для разработки.

Сервер имитирует googlevideo: задержка на соединение и на запрос, потолок скорости одного соединения (один TCP-поток
через далёкий прокси/туннель) и «дросселирование» длинных запросов без диапазона — после первых МБ скорость падает
ниже битрейта; короткие запросы с range= / Range: bytes=a-b отдаются в полную скорость. Клиент — настоящий MediaServer
и настоящий ffmpeg; маршрут — SOCKS5 с логином (tests/socks5_server.py) или встроенный VPN (--awg-conf).

  python scripts/bench_video.py                     # через SOCKS5 на этой машине
  python scripts/bench_video.py --awg-conf /tmp/awg-test-client.conf --awg-host 10.77.9.1
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from owntube import bridge, httpc, player, validate  # noqa: E402
from owntube.media import MediaServer  # noqa: E402
from owntube.network import Route  # noqa: E402

WORK = Path(os.environ.get("BENCH_DIR", "/tmp/owntube-bench"))
DURATION = 120  # секунд тестового ролика


def make_media(duration=DURATION):
    WORK.mkdir(parents=True, exist_ok=True)
    v, a = WORK / "video.webm", WORK / "audio.webm"
    if not v.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"testsrc2=size=1280x720:rate=30,noise=alls=40:allf=t", "-t", str(duration), "-an",
                        "-c:v", "libvpx-vp9", "-b:v", "4M", "-minrate", "4M", "-maxrate", "4M", "-deadline", "realtime",
                        "-cpu-used", "8", "-g", "90", "-cues_to_front", "1", str(v)], check=True)
    if not a.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(duration),
                        "-c:a", "libopus", "-b:a", "128k", "-cues_to_front", "1", str(a)], check=True)
    return v, a


def make_cert(host):
    crt, key = WORK / "cert.pem", WORK / "key.pem"
    san = f"IP:{host},IP:127.0.0.1,DNS:localhost"
    if not crt.exists() or (WORK / "san.txt").read_text() != san if (WORK / "san.txt").exists() else True:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                        "-days", "30", "-subj", "/CN=bench", "-addext", f"subjectAltName={san}"], check=True,
                       capture_output=True)
        (WORK / "san.txt").write_text(san)
    return crt, key


class FakeGV:
    """Имитация googlevideo. rtt — сек; conn_bps — потолок одного соединения (байт/с); throttle_bps — для длинных запросов."""

    def __init__(self, files: dict, rtt=0.08, conn_bps=1.25e6, throttle_bps=0.4e6, burst=1 << 20, host="127.0.0.1"):
        self.files, self.rtt, self.conn_bps, self.throttle_bps, self.burst = files, rtt, conn_bps, throttle_bps, burst
        self.ctx = None
        self.conns = 0
        self.peak = 0
        self.live = 0
        self.requests: list[tuple[str, int]] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                if srv.ctx is not None:
                    self.request = srv.ctx.wrap_socket(self.request, server_side=True)
                    self.connection = self.request
                    self.rfile = self.request.makefile("rb", self.rbufsize)
                    self.wfile = self.request.makefile("wb", self.wbufsize)
                with threading.Lock():
                    srv.conns += 1
                    srv.live += 1
                    srv.peak = max(srv.peak, srv.live)
                time.sleep(srv.rtt * 3)  # TCP + TLS через далёкий прокси

            def finish(self):
                srv.live -= 1
                super().finish()

            def log_message(self, *a):
                return

            def do_GET(self):
                u = urlsplit(self.path)
                data = srv.files.get(u.path)
                if data is None:
                    self.send_error(404)
                    return
                n = len(data)
                q = parse_qs(u.query)
                a, b, bounded = 0, n - 1, False
                if "range" in q:
                    s, _, e = q["range"][0].partition("-")
                    a, b, bounded = int(s), min(int(e), n - 1), True
                rng = self.headers.get("Range")
                if rng:
                    s, _, e = rng.split("=", 1)[1].partition("-")
                    a = int(s)
                    if e:
                        b, bounded = min(int(e), n - 1), True
                    else:
                        b = n - 1
                if a >= n:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{n}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                time.sleep(srv.rtt)
                part = rng is not None
                srv.requests.append((u.path, b - a + 1))
                self.send_response(206 if part else 200)
                self.send_header("Content-Length", str(b - a + 1))
                if part:
                    self.send_header("Content-Range", f"bytes {a}-{b}/{n}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                sent, pos, t0 = 0, a, time.monotonic()
                blk = 32768
                try:
                    while pos <= b:
                        piece = data[pos:min(pos + blk, b + 1)]
                        self.wfile.write(piece)
                        pos += len(piece)
                        sent += len(piece)
                        rate = srv.conn_bps if bounded or sent <= srv.burst else min(srv.conn_bps, srv.throttle_bps)
                        if not bounded and sent > srv.burst:
                            base = srv.burst / srv.conn_bps
                            want = base + (sent - srv.burst) / rate
                        else:
                            want = sent / rate
                        d = t0 + want - time.monotonic()
                        if d > 0:
                            time.sleep(d)
                except OSError:
                    pass

        crt, key = make_cert(host)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(crt), str(key))
        self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.host = host
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, name):
        return f"https://{self.host}:{self.port}/{name}"


def play(media: MediaServer, sid: str, start: float, bitrate_bps: float, seconds=30.0, want_bytes=24 << 20):
    """Читает поток как <video>: время до первого байта, до N МБ, средняя скорость, остановки при воспроизведении в реальном времени."""
    t0 = time.monotonic()
    r = urllib.request.urlopen(media.video_url(sid, start), timeout=90)
    got, tl = 0, []
    ttfb = None
    while time.monotonic() - t0 < seconds and got < want_bytes:
        b = r.read1(262144) if hasattr(r, "read1") else r.read(65536)
        if not b:
            break
        if ttfb is None:
            ttfb = time.monotonic() - t0
        got += len(b)
        tl.append((time.monotonic() - t0, got))
    r.close()
    el = time.monotonic() - t0
    # воспроизведение: старт, когда накоплено 2 с; дальше играем 1 с/с, остановка — если плейхед догнал загруженное
    need0 = bitrate_bps / 8 * 2
    ps = next((t for t, g in tl if g >= need0), None)
    stalls, stall_s = 0, 0.0
    if ps is not None:
        clock, played, i, stalled = ps, 0.0, 0, False
        for t, g in tl:
            avail = g / (bitrate_bps / 8)
            if avail >= played + 0.25 or t >= clock:
                pass
        # дискретная модель: шаг 0.1 с
        t, played, stalled = ps, 0.0, False
        end = tl[-1][0]
        j = 0
        while t < end:
            while j + 1 < len(tl) and tl[j + 1][0] <= t:
                j += 1
            avail = tl[j][1] / (bitrate_bps / 8)
            if played >= avail - 0.05:
                if not stalled:
                    stalls += 1
                    stalled = True
                stall_s += 0.1
            else:
                if stalled and avail - played < 1.0:
                    stall_s += 0.1  # ждём, пока накопится секунда
                else:
                    stalled = False
                    played += 0.1
            t += 0.1
    t5 = next((t for t, g in tl if g >= 5 << 20), None)
    t1 = next((t for t, g in tl if g >= 1 << 20), None)  # ~1,5 с видео: первый кадр можно показать
    return {"ttfb_ms": round((ttfb or el) * 1000), "t_1mb_ms": round(t1 * 1000) if t1 else None, "t_5mb_s": round(t5, 2) if t5 else None,
            "mbit_s": round(got * 8 / max(el, 1e-6) / 1e6, 1), "bytes": got, "stalls": stalls, "stall_s": round(stall_s, 1)}


def run_case(label, route, fake, use_sources, bitrate, repeats, prefetch_mb):
    out = []
    for k in range(repeats):
        httpc.close_pool()
        media = MediaServer(lambda: route, lambda: "ffmpeg", lambda: prefetch_mb, use_sources=use_sources)
        streams = {"video": fake.url("video.webm"), "audio": fake.url("audio.webm"), "container": "webm", "source": "yt"}
        sid = media.add_session(streams, exclusive=True)
        try:
            time.sleep(0.0)
            first = play(media, sid, 0, bitrate)
            seek = play(media, sid, 60, bitrate, seconds=25, want_bytes=4 << 20)
        finally:
            media.drop_session(sid)
            media.close()
        out.append((first, seek))
        print(f"  {label} #{k + 1}: первый байт {first['ttfb_ms']} мс, первый МБ (≈ первый кадр) {first['t_1mb_ms']} мс, 5 МБ за {first['t_5mb_s']} с, {first['mbit_s']} Мбит/с, "
              f"остановок {first['stalls']} ({first['stall_s']} с); перемотка → первый МБ {seek['t_1mb_ms']} мс", flush=True)
    med = lambda key, idx: statistics.median(x[idx][key] for x in out if x[idx][key] is not None)  # noqa: E731
    return {"ttfb_ms": med("ttfb_ms", 0), "first_frame_ms": med("t_1mb_ms", 0), "t_5mb_s": med("t_5mb_s", 0), "mbit_s": med("mbit_s", 0),
            "stalls": med("stalls", 0), "stall_s": med("stall_s", 0), "seek_first_frame_ms": med("t_1mb_ms", 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtt-ms", type=float, default=80)
    ap.add_argument("--conn-mbit", type=float, default=10, help="потолок одного соединения, Мбит/с")
    ap.add_argument("--throttle-mbit", type=float, default=3.2, help="скорость длинного запроса без диапазона после всплеска")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--prefetch-mb", type=int, default=32)
    ap.add_argument("--awg-conf")
    ap.add_argument("--awg-host", default="10.77.9.1", help="адрес этой машины внутри туннеля")
    ap.add_argument("--json")
    ap.add_argument("--only", choices=["до", "после"])
    ap.add_argument("--probe", type=int, help="подменить -probesize/-analyzeduration (проба)")
    a = ap.parse_args()
    validate.is_stream_url = lambda *x, **k: True  # тестовый сервер — не googlevideo
    bridge.host_allowed = lambda *x, **k: True
    os.environ["SSL_CERT_FILE"] = str(WORK / "cert.pem")  # доверяем тестовому сертификату (Python); ffmpeg — ниже
    orig = player.ffmpeg_command

    def with_tls(*args, **kw):
        cmd = orig(*args, **kw)
        if a.probe:
            cmd = [str(a.probe) if x == "1500000" else x for x in cmd]
        out = []
        for i, x in enumerate(cmd):
            if x == "-i" and not kw.get("local") and not (len(args) > 4 and args[4]):
                out += ["-tls_verify", "0"]
            out.append(x)
        return out
    import owntube.media as media_mod
    media_mod.ffmpeg_command = with_tls
    httpc.is_private_host = lambda h: False  # иначе маршрут для 127.0.0.1 игнорируется
    make_cert("127.0.0.1")
    v, au = make_media()
    files = {"/video.webm": v.read_bytes(), "/audio.webm": au.read_bytes()}
    bitrate = len(files["/video.webm"]) * 8 / DURATION
    print(f"тестовый ролик: видео {len(files['/video.webm']) / 1e6:.1f} МБ ({bitrate / 1e6:.1f} Мбит/с), звук {len(files['/audio.webm']) / 1e6:.1f} МБ")
    host = a.awg_host if a.awg_conf else "127.0.0.1"
    fake = FakeGV(files, a.rtt_ms / 1000, a.conn_mbit * 1e6 / 8, a.throttle_mbit * 1e6 / 8, host=host)
    cleanup = []
    if a.awg_conf:
        from owntube import awg
        prov = awg.AwgProvider(lambda: awg.parse_conf(Path(a.awg_conf).read_text()), lambda: "/tmp/awgproxy")
        route = prov.current()
        cleanup.append(prov)
        label = "встроенный VPN"
    else:
        from socks5_server import Socks5Server
        TEST_USER, TEST_PASS = "u", "p"  # учётка тестового сервера, не настоящая
        srv = Socks5Server(TEST_USER, TEST_PASS)
        route = Route(proxy=srv.url, label="socks5", user=TEST_USER, password=TEST_PASS)
        label = "SOCKS5"
    print(f"маршрут: {label}; RTT {a.rtt_ms:.0f} мс, соединение ≤ {a.conn_mbit} Мбит/с, длинный запрос без диапазона после 1 МБ ≤ {a.throttle_mbit} Мбит/с")
    res = {}
    for name, flag in (("до", False), ("после", True)):
        if a.only and name != a.only:
            continue
        fake.requests.clear()
        fake.peak = 0
        res[name] = run_case(name, route, fake, flag, bitrate, a.repeats, a.prefetch_mb)
        res[name]["peak_conns"] = fake.peak
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    for c in cleanup:
        try:
            c.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
