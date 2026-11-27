"""0.3.3: быстрая загрузка видео (streamsrc), быстрый старт, «Скорость видео», ленты аккаунтов Google и VK.

Сеть не нужна: сервер видео — локальный (Range, range=, 403), ответы аккаунтов — подставные."""
import http.server
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from owntube import diagnostics, lists, player, search as yt, streamsrc, validate, vk
from owntube.api import Api
from owntube.media import MediaServer
from owntube.network import Route
from owntube.secretstore import MemoryBackend, SecretStore
from owntube.settings import SettingsStore, _validated

FFMPEG = shutil.which("ffmpeg")


def vid(n):
    return f"vid{n:08d}"[:11].ljust(11, "x")


def item(n, **kw):
    return {"source": "yt", "id": vid(n), "title": f"Видео {n}", "channel": "К", "channel_id": "", "duration": "1:00",
            "views": "", "live": False, "thumb": "", **kw}


def _api(tmp_path):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    return Api(store=st, start_downloader=False)


# ---------- локальный «googlevideo» ----------
class Origin:
    """Файлы по путям; понимает Range-заголовок и параметр range=a-b; /bad — 403; ведёт журнал запросов."""

    def __init__(self, files: dict, clen=True, delay=0.0):
        self.files, self.clen, self.delay = files, clen, delay
        self.log: list[dict] = []
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()
        origin = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                with origin._lock:
                    origin.live += 1
                    origin.peak = max(origin.peak, origin.live)
                try:
                    data = origin.files.get(u.path)
                    origin.log.append({"path": u.path, "range": self.headers.get("Range"), "param": (q.get("range") or [None])[0]})
                    if data is None:
                        self.send_response(403)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    n = len(data)
                    a, b, part = 0, n - 1, False
                    if "range" in q:
                        s, _, e = q["range"][0].partition("-")
                        a, b = int(s), min(int(e), n - 1)
                    elif self.headers.get("Range"):
                        s, _, e = self.headers["Range"].split("=", 1)[1].partition("-")
                        a, b, part = int(s), (min(int(e), n - 1) if e else n - 1), True
                    time.sleep(origin.delay)
                    self.send_response(206 if part else 200)
                    self.send_header("Content-Length", str(b - a + 1))
                    if part:
                        self.send_header("Content-Range", f"bytes {a}-{b}/{n}")
                    self.end_headers()
                    self.wfile.write(data[a:b + 1])
                except OSError:
                    pass
                finally:
                    with origin._lock:
                        origin.live -= 1

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path, extra=""):
        clen = f"?clen={len(self.files[path])}" if self.clen and path in self.files else "?x=1"
        return f"http://127.0.0.1:{self.port}{path}{clen}{extra}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def blob(n, seed=7):
    import random
    rnd = random.Random(seed)
    return rnd.randbytes(n)


@pytest.fixture
def origin():
    made = []

    def make(files, **kw):
        o = Origin(files, **kw)
        made.append(o)
        return o
    yield make
    for o in made:
        o.close()


def read_n(src, pos, n, rid=0):
    out = b""
    while len(out) < n:
        out += src.read(pos + len(out), n - len(out), timeout=10, rid=rid)
    return out


def read_all(src, step=100_000, rid=0):
    out, pos = bytearray(), 0
    total = src.size(10)
    while pos < total:
        d = src.read(pos, step, timeout=10, rid=rid)
        assert d
        out += d
        pos += len(d)
    return bytes(out)


# ---------- сетка кусков и Range ----------
def test_chunk_grid_is_contiguous_and_invertible():
    edges = [streamsrc._bounds(i) for i in range(40)]
    assert edges[0][0] == 0 and all(a[1] == b[0] for a, b in zip(edges, edges[1:]))
    assert edges[0][1] - edges[0][0] < edges[5][1] - edges[5][0] or edges[5][1] - edges[5][0] == streamsrc.BODY
    for pos in (0, 1, 131071, 131072, 400000, 10_000_000, 99_999_999):
        i = streamsrc.chunk_of(pos)
        a, b = streamsrc._bounds(i)
        assert a <= pos < b
    ia = streamsrc.chunk_of(300000, streamsrc.AUDIO_HEAD, streamsrc.AUDIO_BODY)
    a, b = streamsrc._bounds(ia, streamsrc.AUDIO_HEAD, streamsrc.AUDIO_BODY)
    assert a <= 300000 < b


def test_parse_range():
    assert streamsrc.parse_range(None, 100) is None
    assert streamsrc.parse_range("bytes=10-", 100) == (10, 99)
    assert streamsrc.parse_range("bytes=10-20", 100) == (10, 20)
    assert streamsrc.parse_range("bytes=90-500", 100) == (90, 99)
    assert streamsrc.parse_range("bytes=-10", 100) == (90, 99)
    for bad in ("bytes=100-", "bytes=5-2", "items=1-2", "bytes=1-2,4-5", "bytes=-0"):
        with pytest.raises(ValueError):
            streamsrc.parse_range(bad, 100)


# ---------- Source ----------
def test_source_reads_whole_file_in_parallel_chunks(origin):
    data = blob(5_300_000)
    o = origin({"/v": data}, delay=0.05)
    src = streamsrc.Source(o.url("/v"), lambda: Route(), {"User-Agent": "t"}, lambda: 8 * streamsrc.MB)
    try:
        assert read_all(src) == data
        assert o.peak >= 2                      # несколько запросов одновременно
        assert len(o.log) >= 8 and all(r["range"] for r in o.log)  # короткие запросы Range, не один длинный
        assert src.stats()["bytes"] >= len(data)
    finally:
        src.close()


def test_source_googlevideo_uses_range_parameter_not_header(origin, monkeypatch):
    monkeypatch.setattr(streamsrc, "GOOGLE", "127.0.0.1")   # узел теста считаем «googlevideo»
    data = blob(1_200_000)
    o = origin({"/v": data})
    src = streamsrc.Source(o.url("/v"), lambda: Route(), None, lambda: 4 * streamsrc.MB)
    try:
        assert read_all(src) == data
        assert all(r["param"] and not r["range"] for r in o.log)
        sizes = {tuple(map(int, r["param"].split("-"))) for r in o.log}
        assert all(b - a + 1 <= streamsrc.BODY for a, b in sizes)  # каждый запрос короткий, душить нечего
    finally:
        src.close()


def test_source_finds_size_without_clen(origin):
    data = blob(700_000)
    o = origin({"/v": data}, clen=False)
    src = streamsrc.Source(o.url("/v"), lambda: Route(), None, lambda: 4 * streamsrc.MB)
    try:
        assert src.size(10) == len(data)
        assert read_all(src) == data
    finally:
        src.close()


def test_source_reports_expired_address(origin):
    o = origin({})
    src = streamsrc.Source(o.url("/bad") + "&clen=500000", lambda: Route(), None, lambda: 2 * streamsrc.MB)
    try:
        with pytest.raises(streamsrc.Expired):
            src.read(0, 1000, timeout=10)
    finally:
        src.close()


def test_two_readers_do_not_thrash_the_window(origin):
    """ffmpeg держит два соединения (заголовок и перемотка): окно держится у обоих, файл не качается заново."""
    data = blob(12_000_000)
    o = origin({"/v": data})
    src = streamsrc.Source(o.url("/v"), lambda: Route(), None, lambda: 4 * streamsrc.MB)
    try:
        for step in range(12):
            a = read_n(src, 100_000 + step * 50_000, 50_000, 1)
            b = read_n(src, 8_000_000 + step * 50_000, 50_000, 2)
            assert a == data[100_000 + step * 50_000:150_000 + step * 50_000]
            assert b == data[8_000_000 + step * 50_000:8_050_000 + step * 50_000]
        assert src.stats()["bytes"] < 4 * 6_000_000   # а не 12 МБ × число переключений
    finally:
        src.close()


def test_prewarm_downloads_only_the_head(origin):
    o = origin({"/v": blob(20_000_000)})
    src = streamsrc.Source(o.url("/v"), lambda: Route(), None, lambda: 64 * streamsrc.MB)
    try:
        src.warm()
        time.sleep(1.0)
        assert 0 < src.stats()["bytes"] <= streamsrc.PREWARM + streamsrc.BODY * 2
    finally:
        src.close()


# ---------- медиасервер ----------
@pytest.fixture
def https_ok(monkeypatch):
    monkeypatch.setattr(validate, "is_stream_url", lambda *a, **k: True)


def test_media_serves_ranges_from_the_source(origin, https_ok):
    data = blob(1_500_000)
    o = origin({"/v": data, "/a": blob(300_000, 3)})
    m = MediaServer(lambda: Route(), lambda: "ffmpeg")
    try:
        sid = m.add_session({"video": o.url("/v"), "audio": o.url("/a"), "container": "webm", "source": "yt"})
        base = f"{m.base}/s/{sid}/video"
        with urllib.request.urlopen(base, timeout=15) as r:
            assert r.status == 200 and r.headers["Accept-Ranges"] == "bytes" and int(r.headers["Content-Length"]) == len(data)
            assert r.read() == data
        req = urllib.request.Request(base, headers={"Range": "bytes=1000-5999"})
        with urllib.request.urlopen(req, timeout=15) as r:
            assert r.status == 206 and r.headers["Content-Range"] == f"bytes 1000-5999/{len(data)}" and r.read() == data[1000:6000]
        req = urllib.request.Request(base, headers={"Range": "bytes=-100"})
        with urllib.request.urlopen(req, timeout=15) as r:
            assert r.read() == data[-100:]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base, headers={"Range": "bytes=99999999-"}), timeout=10)
        assert e.value.code == 416
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{m.base}/s/nope/video", timeout=10)
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{m.port}/wrongtoken/s/{sid}/video", timeout=10)
        assert e.value.code == 404
    finally:
        m.close()


def test_ffmpeg_command_local_has_no_proxy_and_checks_host():
    st = {"video": "https://rr1.googlevideo.com/videoplayback?x=1", "audio": "https://rr1.googlevideo.com/a?x=1",
          "container": "webm", "source": "yt", "user_agent": "UA", "referer": "https://www.youtube.com/"}
    local = {"video": "http://127.0.0.1:5/t/s/x/video", "audio": "http://127.0.0.1:5/t/s/x/audio"}
    cmd = player.ffmpeg_command("ffmpeg", st, 12.5, "http://127.0.0.1:9", local)
    assert "-http_proxy" not in cmd and "-user_agent" not in cmd and "googlevideo" not in " ".join(cmd)
    assert cmd.count("-i") == 2 and "-ss" in cmd and "-multiple_requests" in cmd
    with pytest.raises(player.PlayerError):
        player.ffmpeg_command("ffmpeg", st, 0, "", {"video": "http://evil.example/v", "audio": "http://127.0.0.1:5/a"})
    old = player.ffmpeg_command("ffmpeg", st, 0, "http://127.0.0.1:9")
    assert "-http_proxy" in old and "googlevideo" in " ".join(old)


@pytest.fixture(scope="module")
def sample_webm(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("нужен ffmpeg")
    d = tmp_path_factory.mktemp("media")
    v, a = d / "v.webm", d / "a.webm"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "6", "-an",
                    "-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime", "-cpu-used", "8", "-g", "25",
                    "-cues_to_front", "1", str(v)], check=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:a", "libopus",
                    "-b:a", "48k", "-cues_to_front", "1", str(a)], check=True)
    return v.read_bytes(), a.read_bytes()


def test_real_ffmpeg_plays_through_the_local_source(origin, https_ok, sample_webm):
    v, a = sample_webm
    o = origin({"/v": v, "/a": a})
    m = MediaServer(lambda: Route(), lambda: FFMPEG)
    try:
        sid = m.add_session({"video": o.url("/v"), "audio": o.url("/a"), "container": "webm", "source": "yt"})
        with urllib.request.urlopen(m.video_url(sid, 0), timeout=60) as r:
            got = r.read()
        assert got[:4] == b"\x1aE\xdf\xa3" and len(got) > 50_000
        assert all(x["range"] for x in o.log)              # ffmpeg ходил только за кусками, прямого длинного запроса нет
        with urllib.request.urlopen(m.video_url(sid, 3.0), timeout=60) as r:   # перемотка
            part = r.read()
        assert part[:4] == b"\x1aE\xdf\xa3" and 20_000 < len(part) < len(got)
        assert m.metrics and m.metrics[-1]["local"] is True
    finally:
        m.close()


def test_real_ffmpeg_refreshes_expired_address_with_sources(origin, https_ok, sample_webm):
    v, a = sample_webm
    o = origin({"/v": v, "/a": a})
    m = MediaServer(lambda: Route(), lambda: FFMPEG)
    try:
        bad = {"video": o.url("/bad", "&clen=400000"), "audio": o.url("/a"), "container": "webm", "source": "yt"}
        good = {"video": o.url("/v"), "audio": o.url("/a"), "container": "webm", "source": "yt"}
        sid = m.add_session(bad, refresh=lambda: good)
        with urllib.request.urlopen(m.video_url(sid, 0), timeout=90) as r:
            assert r.read(20000)[:4] == b"\x1aE\xdf\xa3"
        assert m._sessions[sid]["video"].split("?")[0].endswith("/v")
    finally:
        m.close()


def test_hover_prepare_warms_and_session_reuses_the_same_source(origin, https_ok):
    o = origin({"/v": blob(3_000_000), "/a": blob(400_000, 5)})
    m = MediaServer(lambda: Route(), lambda: "ffmpeg")
    try:
        st = {"video": o.url("/v"), "audio": o.url("/a"), "container": "webm", "source": "yt"}
        m.prepare(st)
        time.sleep(0.8)
        before = len(o.log)
        assert before >= 2 and len(m._warm) == 2
        sid = m.add_session(st)
        assert not m._warm and len(m._sources) == 2   # подготовленное перешло в сеанс, второй раз не качается
        assert m.source_stats(sid)["video"]["bytes"] > 0
    finally:
        m.close()


# ---------- «Скорость видео» ----------
def test_video_speed_reports_both_modes(origin):
    o = origin({"/v": blob(3_000_000)})
    text = diagnostics.video_speed({"video": o.url("/v")}, Route(label="тест"), secs=3, limit=3_000_000)
    assert "Мбит/с" in text and "один поток" in text and "тест" in text and "127.0.0.1" in text
    assert "/v" not in text and "clen" not in text   # адрес потока в отчёт не попадает


def test_video_speed_fails_when_no_data(origin):
    o = origin({})
    with pytest.raises(Exception):
        diagnostics.video_speed({"video": o.url("/bad", "&clen=100000")}, Route(), secs=2)


def test_need_note_levels():
    assert "4K" in diagnostics._need_note(80) and "1080p" in diagnostics._need_note(10)
    assert "не хватает" in diagnostics._need_note(0.5)


# ---------- настройки и быстрый старт ----------
def test_new_settings_are_validated():
    s = _validated({"fast_start": False, "start_quality": "360", "prefetch_mb": 64, "account_feeds": False}, strict=True)
    assert (s.fast_start, s.start_quality, s.prefetch_mb, s.account_feeds) == (False, "360", 64, False)
    for bad in ({"prefetch_mb": 7}, {"prefetch_mb": "32"}, {"prefetch_mb": True}, {"start_quality": "2160"}, {"fast_start": 1}):
        with pytest.raises(ValueError):
            _validated(bad, strict=True)
    assert _validated({}).prefetch_mb == 32 and _validated({}).fast_start is True


@pytest.fixture
def watch_api(tmp_path, monkeypatch):
    api = _api(tmp_path)
    seen = []

    def resolve(source, item_id, quality, route=None, codec="auto", embedded=False, ipv4=False):
        seen.append(quality)
        h = {"360": 360, "480": 480, "720": 720, "1080": 1080, "best": 2160}[quality]
        return {"video": f"https://rr1.googlevideo.com/v{quality}", "audio": None, "title": "T", "container": "webm",
                "live": False, "height": h, "duration": 100.0}

    monkeypatch.setattr(player, "resolve_streams", resolve)
    monkeypatch.setattr("owntube.api.find_tool", lambda name, configured="": "/bin/true")
    return api, seen


def test_fast_start_begins_low_then_upgrades(watch_api):
    api, seen = watch_api
    it = {"source": "yt", "id": vid(1), "title": "x"}
    api.save_settings({"quality": "1080", "start_quality": "480"})
    r = api.watch(it, None, False, True)
    assert r["ok"] and r["quality"] == "480" and r["target"] == "1080" and r["upgrade"] is True and r["height"] == 480
    deadline = time.time() + 5
    while "1080" not in seen and time.time() < deadline:
        time.sleep(0.05)
    assert "1080" in seen                                    # адреса выбранного качества готовятся, пока играет низкое
    up = api.watch(it, "1080", False, False, r["sid"], 12.5)
    assert up["ok"] and up["quality"] == "1080" and up["upgrade"] is False and up["sid"] != r["sid"] and up["url"].endswith("t=12.50")
    media = api._media_server()
    assert r["sid"] in media._sessions and up["sid"] in media._sessions  # старый играет, пока интерфейс не переключится
    assert api._watch["sid"] == r["sid"]
    assert api.commit_upgrade(up["sid"], r["sid"])["ok"]
    assert api._watch["sid"] == up["sid"] and r["sid"] not in media._sessions
    assert api.seek_url(up["sid"], 30)["ok"]
    bad = api.commit_upgrade("zzz", "yyy")
    assert not bad["ok"]
    api._shutdown()


def test_fast_start_is_skipped_when_not_useful(watch_api):
    api, seen = watch_api
    it = {"source": "yt", "id": vid(2), "title": "x"}
    api.save_settings({"quality": "360"})
    assert api.watch(it)["quality"] == "360"                       # выбрано ниже стартового
    api.save_settings({"quality": "1080", "fast_start": False})
    r = api.watch({**it, "id": vid(3)})
    assert r["quality"] == "1080" and r["upgrade"] is False
    api.save_settings({"fast_start": True})
    r = api.watch({**it, "id": vid(4)}, "720", False, False)       # пользователь явно выбрал качество — без ступенек
    assert r["quality"] == "720" and r["upgrade"] is False
    r = api.watch({**it, "id": vid(5)}, None, True)                # Shorts
    assert r["upgrade"] is False
    api._shutdown()


def test_fast_start_uses_cached_target_streams(watch_api):
    api, seen = watch_api
    it = {"source": "yt", "id": vid(6), "title": "x"}
    api.save_settings({"quality": "1080"})
    api._streams({"source": "yt", "id": vid(6)}, "1080", api._routes.current(), "auto")
    r = api.watch(it)
    assert r["quality"] == "1080" and r["upgrade"] is False and r["cached"] is True
    api._shutdown()


def test_playback_stats_are_local_and_clamped(watch_api, caplog):
    api, _ = watch_api
    import logging
    caplog.set_level(logging.INFO, logger="owntube.api")
    r = api.playback_stats({"ttff_ms": 1234, "rebuffers": 2, "stall_ms": 900, "height": 720, "upgraded_ms": 8000, "played_s": 55})
    assert r["ok"] and "первый кадр через 1234 мс" in caplog.text and "rebuffers" not in caplog.text
    assert api.playback_stats({"ttff_ms": -5, "rebuffers": "x", "height": 10**9})["ok"]
    assert not api.playback_stats("x")["ok"]
    assert api._playback[-1]["ttff_ms"] == 0 and api._playback[-1]["height"] == 4320
    api._shutdown()


# ---------- ленты аккаунта Google ----------
@pytest.fixture(autouse=True)
def _clean_account_cache():
    lists.account_forget()
    yield
    lists.account_forget()


def fake_account(monkeypatch, total=130):
    calls = []

    def account_items(kind, route, limit, arg=None):
        calls.append((kind, limit, arg))
        return [item(i) for i in range(1, min(total, limit) + 1)]

    monkeypatch.setattr(lists.yt, "account_items", account_items)
    return calls


def test_account_pages_follow_cursor_and_ask_for_more_when_needed(monkeypatch):
    calls = fake_account(monkeypatch, total=130)
    first, cur = lists.account_page("history", None, Route(), 0, force=True)
    assert len(first) == lists.PAGE and cur["k"] == "y" and cur["kind"] == "acct" and cur["q"] == "history"
    assert calls == [("history", lists.ACCT_FIRST, None)]
    cur = lists.clean_cursor(cur)                      # курсор проходит проверку, как от интерфейса
    seen = {i["id"] for i in first}
    pages = 1
    while cur:
        part, cur = lists.more(cur, Route())
        assert part and not ({i["id"] for i in part} & seen)
        seen |= {i["id"] for i in part}
        pages += 1
        if cur:
            cur = lists.clean_cursor(cur)
    assert len(seen) == 130 and pages >= 6
    assert max(c[1] for c in calls) > lists.ACCT_FIRST     # глубже первых 48 дозапрашивали
    assert len(calls) <= 5                                 # а не по запросу на страницу


def test_account_page_rejects_unknown_kind():
    with pytest.raises(ValueError):
        lists.account_page("passwords", None, Route())


def test_account_playlist_needs_valid_id(monkeypatch):
    monkeypatch.setattr(lists.yt, "_flat", lambda url, route, limit: [])
    with pytest.raises(ValueError):
        yt.account_items("playlist", Route(), 10, "a b;c")
    with pytest.raises(ValueError):
        yt.account_items("nope", Route(), 10)


def test_expired_google_login_becomes_a_clear_hint(monkeypatch):
    import yt_dlp

    def boom(url, route, limit):
        raise yt_dlp.utils.DownloadError("ERROR: [youtube:tab] Sign in to confirm you’re not a bot. cookies=SID=abc123secret")

    monkeypatch.setattr(yt, "_flat", boom)
    with pytest.raises(yt.AccountError) as e:
        yt.account_items("subs", Route(), 10)
    assert e.value.relogin and "Войдите заново" in str(e.value) and "abc123secret" not in str(e.value)

    def other(url, route, limit):
        raise yt_dlp.utils.DownloadError("ERROR: unable to download webpage: timed out")

    monkeypatch.setattr(yt, "_flat", other)
    with pytest.raises(yt.AccountError) as e:
        yt.account_items("subs", Route(), 10)
    assert not e.value.relogin and "timed out" in str(e.value)


def signed(api, google=True, vk_in=False):
    api._accounts.status = lambda: {"google": {"signed_in": google}, "vk": {"signed_in": vk_in}}


def test_home_and_subs_come_from_the_account_after_sign_in(tmp_path, monkeypatch):
    api = _api(tmp_path)
    calls = fake_account(monkeypatch)
    signed(api)
    monkeypatch.setattr(lists, "first", lambda *a, **k: pytest.fail("обычная главная не должна вызываться"))
    r = api.feed("home")
    assert r["ok"] and r["from"] == "account" and len(r["items"]) == lists.PAGE and r["more"]["kind"] == "acct"
    assert calls[0][0] == "recommended"
    more = api.feed("home", None, r["more"])
    assert more["ok"] and more["items"][0]["id"] != r["items"][0]["id"]
    s = api.feed("subs")
    assert s["ok"] and calls[-1][0] == "subs"
    cached = api.feed("home", None, None, "cache")
    assert cached["items"] and cached["stale"] is True
    for kind, want in (("history", "history"), ("later", "later"), ("liked", "liked"), ("playlist:PLabc123", "playlist")):
        x = api.feed("acct", kind)
        assert x["ok"] and x["items"], kind
        assert calls[-1][0] == want
    assert calls[-1][2] == "PLabc123"
    assert not api.feed("acct", "cookies")["ok"]
    api._shutdown()


def test_account_feed_falls_back_with_a_relogin_hint(tmp_path, monkeypatch):
    api = _api(tmp_path)
    signed(api)

    def deny(*a, **k):
        raise yt.AccountError(yt.RELOGIN, True)

    monkeypatch.setattr(lists.yt, "account_items", deny)
    monkeypatch.setattr(lists, "first", lambda kind, arg, route, subs=None: ([item(1), item(2)], None, "popular"))
    r = api.feed("home")
    assert r["ok"] and r["relogin"] is True and "Войдите заново" in r["account_error"] and len(r["items"]) == 2
    r = api.feed("acct", "history")
    assert not r["ok"] and "Войдите заново" in r["error"]
    assert "SID" not in json.dumps(r)
    api._shutdown()


def test_empty_account_home_means_probably_expired(tmp_path, monkeypatch):
    api = _api(tmp_path)
    signed(api)
    monkeypatch.setattr(lists.yt, "account_items", lambda *a, **k: [])
    monkeypatch.setattr(lists, "first", lambda kind, arg, route, subs=None: ([item(1)], None, "popular"))
    r = api.feed("home")
    assert r["ok"] and r["relogin"] is True and r["items"]
    api._shutdown()


def test_account_feeds_can_be_turned_off_and_need_sign_in(tmp_path, monkeypatch):
    api = _api(tmp_path)
    monkeypatch.setattr(lists.yt, "account_items", lambda *a, **k: pytest.fail("без входа в аккаунт не ходим"))
    monkeypatch.setattr(lists, "first", lambda kind, arg, route, subs=None: ([item(1)], None, "popular"))
    signed(api, google=False)
    assert api.feed("home")["from"] == "popular"
    assert not api.feed("acct", "history")["ok"]
    signed(api, google=True)
    api.save_settings({"account_feeds": False})
    assert api.feed("home")["from"] == "popular"
    api._shutdown()


def test_signing_out_drops_cached_account_lists(tmp_path, monkeypatch):
    api = _api(tmp_path)
    fake_account(monkeypatch)
    signed(api)
    api.feed("home")
    assert lists._acct
    api._accounts.logout = lambda kind: None
    api.account_logout("google")
    assert not lists._acct and api.feed("home", None, None, "cache")["items"] is None
    api._shutdown()


# ---------- ленты VK ----------
VK_VIDEO = lambda n: {"id": n, "owner_id": -77, "title": f"Ролик {n}", "duration": 61, "views": 1500, "image": []}  # noqa: E731


def vk_mock(monkeypatch, sections=None, my_total=45, calls=None):
    calls = calls if calls is not None else []
    sections = sections if sections is not None else [{"id": "sec_rec", "title": "Рекомендации"}, {"id": "sec_sub", "title": "Подписки"}]

    def post(url, form, route, cookie=""):
        method = url.split("/method/")[1].split("?")[0]
        calls.append((method, {k: v for k, v in form.items() if k != "access_token"}))
        if method == "catalog.getVideo":
            return {"response": {"catalog": {"sections": sections}, "videos": [VK_VIDEO(900)]}}
        if method == "catalog.getSection":
            sid = form["section_id"]
            return {"response": {"section": {"id": sid, "next_from": "n2" if "start_from" not in form else ""},
                                 "videos": [VK_VIDEO(1 if sid == "sec_rec" else 2), VK_VIDEO(3)],
                                 "groups": [{"id": 77, "name": "Группа"}]}}
        if method == "users.get":
            return {"response": [{"id": 4242}]}
        if method == "video.get":
            off = int(form["offset"])
            return {"response": {"count": my_total, "items": [{**VK_VIDEO(100 + off + i), "owner_id": 4242} for i in range(40)][:max(0, min(40, my_total - off))]}}
        raise AssertionError(method)

    monkeypatch.setattr(vk, "_post", post)
    monkeypatch.setattr(vk, "_token", lambda route, cookie: ("TOKEN", bool(cookie)))
    return calls


def test_vk_recommendations_and_subscriptions_use_the_matching_section(monkeypatch):
    calls = vk_mock(monkeypatch)
    rec = vk.feed("recommended", Route(), "remixsid=x")
    assert [i["id"] for i in rec["items"]] == ["-77_1", "-77_3"] and rec["items"][0]["channel"] == "Группа"
    assert rec["more"] == {"k": "vk", "sec": "sec_rec", "t": "n2"} and rec["title"] == "Рекомендации"
    sub = vk.feed("subs", Route(), "remixsid=x")
    assert sub["items"][0]["id"] == "-77_2"
    assert [c[1].get("section_id") for c in calls if c[0] == "catalog.getSection"] == ["sec_rec", "sec_sub"]
    assert "TOKEN" not in json.dumps(calls)


def test_vk_feed_without_login_or_section(monkeypatch):
    vk_mock(monkeypatch)
    with pytest.raises(ValueError, match="Сначала войдите"):
        vk.feed("recommended", Route(), "")
    vk_mock(monkeypatch, sections=[{"id": "only", "title": "Новое"}])
    assert vk.feed("recommended", Route(), "c=1")["items"]                    # для рекомендаций берётся первый раздел
    with pytest.raises(ValueError, match="Подписки"):
        vk.feed("subs", Route(), "c=1")
    with pytest.raises(ValueError):
        vk.feed("bogus", Route(), "c=1")


def test_vk_my_videos_page_through_offsets(monkeypatch):
    calls = vk_mock(monkeypatch, my_total=45)
    first = vk.feed("my", Route(), "c=1")
    assert len(first["items"]) == 40 and first["items"][0]["id"] == "4242_100" and first["more"]["sec"] == vk.MY_SECTION
    second = vk.search("", Route(), "c=1", cursor=first["more"])
    assert len(second["items"]) == 5 and second["more"] is None
    assert [c[1]["offset"] for c in calls if c[0] == "video.get"] == ["0", "40"]


def test_vk_expired_session_gives_relogin_hint(monkeypatch):
    monkeypatch.setattr(vk, "_token", lambda route, cookie: ("T", True))
    monkeypatch.setattr(vk, "_post", lambda *a, **k: {"error": {"error_code": 5, "error_msg": "User authorization failed"}})
    with pytest.raises(ValueError, match="Войдите заново в VK"):
        vk.feed("recommended", Route(), "c=1")


def test_api_vk_feed(tmp_path, monkeypatch):
    api = _api(tmp_path)
    vk_mock(monkeypatch)
    api._accounts.cookie_header = lambda kind, host: "remixsid=secret"
    signed(api, google=False, vk_in=False)
    assert not api.vk_feed("recommended")["ok"]
    signed(api, google=False, vk_in=True)
    r = api.vk_feed("recommended")
    assert r["ok"] and r["items"] and r["more"]["k"] == "vk" and r["signed_in"] is True
    more = api.vk_feed("recommended", r["more"])
    assert more["ok"] and more["items"]
    assert api.vk_feed("recommended", None, "cache")["stale"] is True
    assert not api.vk_feed("passwords")["ok"]
    assert "secret" not in json.dumps(r)
    api._shutdown()
