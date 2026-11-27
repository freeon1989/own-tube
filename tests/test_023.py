"""0.2.3: быстрые списки (innertube), страницы и кэш, VK-страницы, голос, потоки и сеансы медиасервера.

Сеть не нужна: ответы YouTube/VK — образцы, внешние серверы подставные."""
import base64
import hashlib
import json
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from owntube import innertube, lists, player, validate, vk, voice
from owntube.ai import assistant
from owntube.api import Api
from owntube.media import MediaServer
from owntube.network import Route
from owntube.secretstore import MemoryBackend, SecretStore
from owntube.settings import SettingsStore
from test_022 import tls  # noqa: F401  (фикстура с подставным TLS-сервером)


def vid(n):
    return f"vid{n:08d}"[:11].ljust(11, "x")


def video_renderer(n, live=False):
    r = {"videoId": vid(n), "title": {"runs": [{"text": f"Видео {n}"}]},
         "ownerText": {"runs": [{"text": "Канал", "navigationEndpoint": {"browseEndpoint": {"browseId": "UC" + "a" * 22}}}]},
         "lengthText": {"simpleText": "3:21"}, "viewCountText": {"simpleText": "1 234 просмотра"},
         "publishedTimeText": {"simpleText": "2 дня назад"}}
    if live:
        r["badges"] = [{"metadataBadgeRenderer": {"style": "BADGE_STYLE_TYPE_LIVE_NOW"}}]
    return {"videoRenderer": r}


def shorts_lockup(n):
    return {"shortsLockupViewModel": {"entityId": "shorts-shelf-item-" + vid(n),
            "overlayMetadata": {"primaryText": {"content": f"Шорт {n}"}, "secondaryText": {"content": "5 тыс. просмотров"}},
            "onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": vid(n)}}}}}


def page(contents, token="TOKEN-1"):
    data = {"contents": {"twoColumn": {"section": [{"itemSection": {"contents": contents}}]}}}
    if token:
        data["contents"]["twoColumn"]["section"].append(
            {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}})
    return data


def test_parse_search_page_skips_shorts_and_reads_fields():
    data = page([video_renderer(1), shorts_lockup(2), video_renderer(3, live=True), video_renderer(1)])
    items, token = innertube.parse(data)
    assert [i["id"] for i in items] == [vid(1), vid(3)] and token == "TOKEN-1"
    a = items[0]
    assert a["duration"] == "3:21" and a["channel"] == "Канал" and a["channel_id"].startswith("UC") and a["age"] == "2 дня назад"
    assert items[1]["live"] is True and items[1]["duration"] == ""
    assert validate.is_thumb_url(a["thumb"])


def test_parse_shorts_mode_keeps_only_shorts():
    items, token = innertube.parse(page([video_renderer(1), shorts_lockup(2), shorts_lockup(3)], token=""), shorts=True)
    assert [i["id"] for i in items] == [vid(2), vid(3)] and all(i["short"] for i in items) and token == ""


def test_parse_channel_lockups():
    lock = {"lockupViewModel": {"contentId": vid(5), "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
            "contentImage": {"thumbnailViewModel": {"overlays": [{"thumbnailBottomOverlayViewModel": {"badges": [
                {"thumbnailBadgeViewModel": {"text": "4:16"}}]}}]}},
            "metadata": {"lockupMetadataViewModel": {"title": {"content": "Урок"}, "metadata": {"contentMetadataViewModel": {
                "metadataRows": [{"metadataParts": [{"text": {"content": "32 тыс. просмотров"}}, {"text": {"content": "1 день назад"}}]}]}}}}}}
    data = page([lock])
    data["metadata"] = {"channelMetadataRenderer": {"title": "Google", "externalId": "UC" + "b" * 22}}
    items, _ = innertube.parse(data)
    assert items[0]["duration"] == "4:16" and items[0]["views"].startswith("32") and items[0]["channel"] == "Google"
    assert items[0]["channel_id"] == "UC" + "b" * 22 and items[0]["age"] == "1 день назад"


@pytest.fixture
def fake_it(monkeypatch):
    calls = []
    pages = {"": ([video_renderer(i) for i in range(1, 21)], "T1"), "T1": ([video_renderer(i) for i in range(21, 41)], "T2"),
             "T2": ([video_renderer(i) for i in range(41, 45)], "")}

    def search(query, route, sp="", token="", shorts=False):
        calls.append((query, sp, token, shorts))
        contents, nxt = pages[token]
        items, _ = innertube.parse(page(contents, token=""), shorts=shorts)
        return items, nxt

    monkeypatch.setattr(innertube, "search", search)
    return calls


def test_pages_follow_continuation_to_the_end(fake_it):
    items, cur, _ = lists.first("search", "аниме", Route())
    assert len(items) == 20 and cur["k"] == "it" and cur["t"] == "T1"
    more1, cur = lists.more(cur, Route())
    more2, cur = lists.more(cur, Route())
    assert [len(more1), len(more2), cur] == [20, 4, None]
    ids = [i["id"] for i in items + more1 + more2]
    assert len(ids) == len(set(ids)) == 44


def test_falls_back_to_ytdlp_when_innertube_fails(monkeypatch):
    def boom(*a, **k):
        raise innertube.InnertubeError("YouTube ответил HTTP 500")

    monkeypatch.setattr(innertube, "search", boom)
    seen = []
    monkeypatch.setattr(lists.yt, "search", lambda q, route, limit, sp="": seen.append((q, limit)) or
                        [{"source": "yt", "id": vid(i), "title": "t", "live": False} for i in range(limit)])
    items, cur, _ = lists.first("search", "кошки", Route())
    assert len(items) == 24 and cur["k"] == "y" and cur["n"] == 24
    more, cur2 = lists.more(cur, Route())
    assert len(more) == 24 and seen[-1][1] == 48 and cur2["n"] == 48
    assert more[0]["id"] != items[0]["id"]


def test_subscription_feed_is_cut_into_pages(monkeypatch):
    allv = [{"source": "yt", "id": vid(i), "title": "t"} for i in range(60)]
    monkeypatch.setattr(lists.yt, "subscriptions_feed", lambda subs, route, per_channel, limit: allv)
    items, cur, origin = lists.first("home", None, Route(), {"UC" + "a" * 22: "A"})
    assert origin == "subs" and len(items) == 24 and cur["k"] == "mem"
    p2, cur = lists.more(cur, Route())
    p3, cur = lists.more(cur, Route())
    assert (len(p2), len(p3), cur) == (24, 12, None)


def test_cursor_validation_rejects_garbage():
    for bad in ({"k": "evil"}, {"k": "it", "t": "x" * 5000}, {"k": "y", "n": -1}, {"k": "it", "sp": "javascript"},
                {"k": "it", "cid": "nope"}, "text", {"k": "it", "n": True}):
        with pytest.raises((ValueError, TypeError)):
            lists.clean_cursor(bad)
    assert lists.clean_cursor(None) is None
    assert lists.clean_cursor({"k": "it", "t": "abc", "extra": "dropped"}) == {"k": "it", "t": "abc"}


def _api(tmp_path, **kw):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    return Api(store=st, start_downloader=False, **kw)


def test_api_returns_saved_list_first_then_fresh(tmp_path, fake_it):
    api = _api(tmp_path)
    assert api.search("аниме", "yt", None, "cache") == {"ok": True, "items": None}
    fresh = api.search("аниме", "yt")
    assert fresh["ok"] and len(fresh["items"]) == 20 and fresh["more"]["t"] == "T1"
    cached = api.search("аниме", "yt", None, "cache")
    assert cached["stale"] is True and len(cached["items"]) == 20
    nxt = api.search("аниме", "yt", fresh["more"])
    assert nxt["ok"] and len(nxt["items"]) == 20 and nxt["more"]["t"] == "T2"
    # после перезапуска сохранённое остаётся
    api2 = _api(tmp_path)
    assert api2.search("аниме", "yt", None, "cache")["items"][0]["id"] == vid(1)
    bad = api.search("аниме", "yt", {"k": "bogus"})
    assert bad["ok"] is False


def test_feed_pages_and_more_method(tmp_path, fake_it):
    api = _api(tmp_path)
    r = api.feed("category", "music")
    assert r["ok"] and r["more"]["k"] == "it"
    m = api.more(r["more"])
    assert m["ok"] and len(m["items"]) == 20
    assert api.feed("nope")["ok"] is False
    s = api.feed("shorts")
    assert s["ok"] and fake_it[-1][3] is True


# ---------- VK ----------
def vk_video(owner, n):
    return {"owner_id": owner, "id": n, "title": f"VK {n}", "duration": 65, "views": 1200, "image": []}


def test_vk_parse_unions_all_blocks_and_builds_cursor():
    resp = {"videos": [vk_video(-1, 1), vk_video(-1, 2)],
            "catalog_videos": [{"video": vk_video(-1, 2)}, {"video": vk_video(-2, 3)}, {"video": vk_video(-2, 4)}],
            "catalog": {"sections": [{"id": "SEC", "next_from": "NEXT"}]}}
    items = vk.parse_search(resp)
    assert [i["id"] for i in items] == ["-1_1", "-1_2", "-2_3", "-2_4"]
    assert vk._cursor(resp) == {"k": "vk", "sec": "SEC", "t": "NEXT"}
    assert vk._cursor({"section": {"id": "S2", "next_from": ""}}) is None


def test_vk_paging_uses_get_section(monkeypatch):
    calls = []

    def post(url, form, route, cookie=""):
        calls.append((url.split("?")[0].rsplit("/", 1)[1], dict(form)))
        if "getSection" in url:
            return {"response": {"section": {"id": "SEC", "next_from": ""}, "catalog_videos": [{"video": vk_video(-5, 9)}]}}
        return {"response": {"videos": [vk_video(-1, 1)], "catalog": {"sections": [{"id": "SEC", "next_from": "N1"}]}}}

    monkeypatch.setattr(vk, "_post", post)
    monkeypatch.setattr(vk, "anonymous_token", lambda route: "anon")
    first = vk.search("музыка", Route())
    assert first["more"] == {"k": "vk", "sec": "SEC", "t": "N1"}
    second = vk.search("музыка", Route(), cursor=first["more"])
    assert [i["id"] for i in second["items"]] == ["-5_9"] and second["more"] is None
    assert calls[1][0] == "catalog.getSection" and calls[1][1]["start_from"] == "N1" and calls[1][1]["section_id"] == "SEC"


def test_vk_search_through_api_with_cursor(tmp_path, monkeypatch):
    seen = {}

    def fake(q, route, cookie="", adult=False, count=30, cursor=None):
        seen["cursor"] = cursor
        return {"items": [], "more": None, "signed_in": False, "adult": False}

    monkeypatch.setattr(vk, "search", fake)
    api = _api(tmp_path)
    assert api.search("музыка", "vk", {"k": "vk", "sec": "S", "t": "T"})["ok"]
    assert seen["cursor"] == {"k": "vk", "sec": "S", "t": "T"}
    assert api.search("музыка", "vk", {"k": "it", "t": "T"})["ok"] is False


# ---------- потоки и сеансы ----------
def test_stream_cache_shares_one_resolution():
    sc = player.StreamCache()
    n = {"calls": 0}

    def producer():
        n["calls"] += 1
        time.sleep(0.15)
        return {"video": "https://x", "n": n["calls"]}

    out = []
    ths = [threading.Thread(target=lambda: out.append(sc.get(("a",), producer))) for _ in range(5)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert n["calls"] == 1 and len(out) == 5
    assert sc.get(("a",), producer)[1] is True
    assert sc.get(("a",), producer, fresh=True)[1] is False and n["calls"] == 2


def test_media_sessions_exclusive_and_shorts_window():
    m = MediaServer(lambda: Route(), lambda: None)
    try:
        a = m.add_session({"video": "https://a"})
        b = m.add_session({"video": "https://b"})
        assert list(m._sessions) == [b]                      # обычный просмотр сбрасывает остальные
        ids = [m.add_session({"video": f"https://s{i}"}, exclusive=False) for i in range(6)]
        assert len(m._sessions) == 4 and ids[0] not in m._sessions and ids[-1] in m._sessions
        m.drop_session(ids[-1])
        assert ids[-1] not in m._sessions and a not in m._sessions
    finally:
        m.close()


def test_media_refreshes_stream_on_network_failure(tmp_path):
    """Первый адрес «не открывается» (поддельный ffmpeg завершается с ошибкой 502), после обновления — отдаёт данные."""
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nif echo \"$@\" | grep -q BAD; then echo 'Server returned 502 Bad Gateway' >&2; exit 1; fi\n"
                    "head -c 200000 /dev/zero\n")
    fake.chmod(0o755)
    m = MediaServer(lambda: Route(), lambda: str(fake))
    try:
        sid = m.add_session({"video": "https://rr1.googlevideo.com/BAD", "audio": None, "container": "webm"},
                            refresh=lambda: {"video": "https://rr1.googlevideo.com/GOOD", "audio": None, "container": "webm"})
        with urllib.request.urlopen(m.video_url(sid, 0), timeout=30) as r:
            data = r.read(50000)
        assert len(data) == 50000
        assert m._sessions[sid]["video"].endswith("GOOD")
    finally:
        m.close()


def test_media_reports_network_error_without_url(tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'Connection refused for https://rr1.googlevideo.com/videoplayback?ip=1.2.3.4&key=s3cret' >&2\nexit 1\n")
    fake.chmod(0o755)
    m = MediaServer(lambda: Route(), lambda: str(fake))
    try:
        sid = m.add_session({"video": "https://rr1.googlevideo.com/x", "audio": None, "container": "webm"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(m.video_url(sid, 0), timeout=30)
        assert e.value.code == 502
        assert "1.2.3.4" not in m.last_error and "s3cret" not in m.last_error and "videoplayback" not in m.last_error
    finally:
        m.close()


def test_watch_in_shorts_mode_keeps_other_sessions(tmp_path, monkeypatch):
    api = _api(tmp_path)
    monkeypatch.setattr(player, "resolve_streams", lambda source, vid_, q, route, codec, embedded=False: {
        "video": "https://rr1.googlevideo.com/v", "audio": None, "title": "T", "container": "webm", "height": 720,
        "duration": 10, "live": False, "channel": "C", "channel_id": ""})
    monkeypatch.setattr("owntube.api.find_tool", lambda name, configured="": "/bin/true")
    a = api.watch({"source": "yt", "id": vid(1), "title": "a"}, None, True)
    b = api.watch({"source": "yt", "id": vid(2), "title": "b"}, None, True)
    assert a["ok"] and b["ok"] and a["sid"] != b["sid"]
    media = api._media_server()
    assert a["sid"] in media._sessions and b["sid"] in media._sessions
    assert b["quality"] == "720" or b["quality"] in ("best", "720")
    c = api.watch({"source": "yt", "id": vid(3), "title": "c"})
    assert list(media._sessions) == [c["sid"]]
    assert api.drop_watch(c["sid"])["ok"] and not media._sessions
    api._shutdown()


def test_prefetch_resolves_once(tmp_path, monkeypatch):
    api = _api(tmp_path)
    n = {"c": 0}

    def resolve(*a, **k):
        n["c"] += 1
        time.sleep(0.1)
        return {"video": "https://rr1.googlevideo.com/v", "audio": None, "title": "T", "container": "webm", "live": False}

    monkeypatch.setattr(player, "resolve_streams", resolve)
    monkeypatch.setattr("owntube.api.find_tool", lambda name, configured="": "/bin/true")
    item = {"source": "yt", "id": vid(7), "title": "x"}
    assert api.prefetch(item)["started"] is True
    api.prefetch(item)
    r = api.watch(item)
    assert r["ok"] and r["cached"] is True and n["c"] == 1
    api._shutdown()


# ---------- голос ----------
def test_assistant_more_action_and_settings(tmp_path):
    text, acts = assistant.parse_actions("Вот ещё.\n[[more]]\n[[search: лофи]]")
    assert [a["kind"] for a in acts] == ["more", "search"] and "[[" not in text
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    st.update({"voice_tts": True, "voice_rate": 1.3, "ai_autorun": False, "stt_engine": "local", "whisper_dir": str(tmp_path)})
    assert st.value.voice_tts and st.value.voice_rate == 1.3 and st.value.ai_autorun is False
    for bad in ({"voice_rate": 5}, {"stt_engine": "cloud"}, {"whisper_dir": "relative"}, {"voice_tts": "yes"}):
        with pytest.raises(ValueError):
            st.update(bad)


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="нужен ffmpeg")


@needs_ffmpeg
def test_to_wav_converts_webm(tmp_path):
    src = tmp_path / "s.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", str(src)], check=True)
    wav = voice.to_wav(shutil.which("ffmpeg"), src.read_bytes(), tmp_path / "w")
    assert wav.suffix == ".wav" and wav.stat().st_size > 20000
    with pytest.raises(voice.VoiceError):
        voice.to_wav(shutil.which("ffmpeg"), b"not audio at all" * 100, tmp_path / "w")


def test_transcribe_local_with_fake_cli(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF" + b"0" * 2000)
    script = tmp_path / "fake.py"
    script.write_text("import sys\nprint('[00:00.000 --> 00:02.000]  служебная строка')\nprint(' включи музыку для работы ')\n")
    text = voice.transcribe_local(tmp_path, wav, "ru", command=[__import__("sys").executable, str(script)])
    assert text == "включи музыку для работы"
    with pytest.raises(voice.VoiceError):
        voice.transcribe_local(tmp_path / "nothing", wav, "ru")


def test_api_stt_pipeline_local_and_errors(tmp_path, monkeypatch):
    api = _api(tmp_path)
    audio = base64.b64encode(b"x" * 5000).decode()
    monkeypatch.setattr("owntube.api.find_tool", lambda name, configured="": "/usr/bin/ffmpeg")
    wavs = []

    def fake_wav(ffmpeg, raw, work):
        p = Path(work) / "x.wav"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        wavs.append(p)
        return p

    monkeypatch.setattr(voice, "to_wav", fake_wav)
    monkeypatch.setattr(voice, "local_ready", lambda d: True)
    monkeypatch.setattr(voice, "transcribe_local", lambda d, wav, lang, command=None: "найди аниме")
    r = api.stt_transcribe(audio, "audio/webm")
    assert r["ok"] and r["text"] == "найди аниме" and r["engine"] == "local"
    assert not wavs[0].exists()                                    # временный файл удалён
    assert api.stt_transcribe("!!!not base64")["ok"] is False
    assert api.stt_transcribe("")["ok"] is False
    monkeypatch.setattr(voice, "transcribe_local", lambda *a, **k: "")
    assert "не распознана" in api.stt_transcribe(audio)["error"]
    monkeypatch.setattr(voice, "local_ready", lambda d: False)
    err = api.stt_transcribe(audio)                                # локально нет, API не настроено
    assert err["ok"] is False and "ИИ" in err["error"]
    st = api.stt_status()
    assert st["ok"] and st["local"] is False and st["size_mb"] > 100
    api._shutdown()


def test_download_checks_hash_size_and_hosts(tmp_path, monkeypatch):
    payload = b"fake whisper model" * 100
    sha = hashlib.sha256(payload).hexdigest()

    class Resp:
        def __init__(self, status, body=b"", loc=""):
            self.status, self._b, self._loc = status, body, loc

        def getheader(self, n):
            return self._loc

        def read(self, n=-1):
            out, self._b = self._b[:n], self._b[n:]
            return out

        def close(self):
            pass

    chain = {"https://huggingface.co/m.bin": Resp(302, loc="https://cdn-lfs.hf.co/x"), "https://cdn-lfs.hf.co/x": Resp(200, payload)}
    monkeypatch.setattr(voice.httpc, "open_url", lambda m, u, r, h=None, b=None, timeout=20: chain[u])
    seen = []
    voice.download("https://huggingface.co/m.bin", tmp_path / "m.bin", len(payload), sha, Route(), lambda a, b: seen.append(a))
    assert (tmp_path / "m.bin").read_bytes() == payload and seen[-1] == len(payload) and not list(tmp_path.glob("*.part"))
    chain["https://cdn-lfs.hf.co/x"] = Resp(200, payload)
    with pytest.raises(voice.VoiceError, match="не совпал"):
        voice.download("https://huggingface.co/m.bin", tmp_path / "bad.bin", len(payload), "0" * 64, Route(), lambda a, b: None, attempts=1)
    assert not (tmp_path / "bad.bin").exists()
    chain["https://evil.example/x"] = Resp(200, payload)
    with pytest.raises(voice.VoiceError, match="разрешённых"):
        voice.download("https://evil.example/x", tmp_path / "e.bin", len(payload), sha, Route(), lambda a, b: None, attempts=1)
    chain["https://github.com/a"] = Resp(302, loc="https://evil.example/x")
    with pytest.raises(voice.VoiceError, match="разрешённых"):
        voice.download("https://github.com/a", tmp_path / "e2.bin", len(payload), sha, Route(), lambda a, b: None, attempts=1)


def test_install_whisper_extracts_only_needed_files(tmp_path, monkeypatch):
    zbuf = tmp_path / "src.zip"
    with zipfile.ZipFile(zbuf, "w") as z:
        for n in ("whisper-cli.exe", "whisper.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu.dll", "main.exe", "SDL2.dll"):
            z.writestr("Release/" + n, b"bin-" + n.encode())
    model = b"m" * 3000

    def dl(url, dest, size, sha, route, progress, attempts=3):
        if url.endswith(".zip"):
            dest.write_bytes(zbuf.read_bytes())
        else:
            dest.write_bytes(model)
        progress(1, 1)

    monkeypatch.setattr(voice, "download", dl)
    monkeypatch.setitem(voice.WHISPER_MODEL, "size", len(model))
    target = tmp_path / "w"
    stages = []
    voice.install_whisper(target, Route(), lambda *a: stages.append(a[0]))
    names = sorted(p.name for p in target.iterdir())
    assert names == ["ggml-base.bin", "ggml-base.dll", "ggml-cpu.dll", "ggml.dll", "whisper-cli.exe", "whisper.dll"]
    assert voice.WHISPER_ZIP["sha256"].startswith("b1514ebc") and voice.WHISPER_MODEL["sha256"].startswith("60ed5bc3")


# ---------- mpv и ffmpeg ходят через локальный мост; прокси только по доменным именам ----------
def test_bridge_serves_mpv_with_domain_only_socks(tls, monkeypatch):
    import socket
    import ssl as _ssl
    from owntube import httpc
    from socks5_server import Socks5Server
    from test_022 import HOSTS, PROXY_SECRET, PROXY_USER

    srv = Socks5Server(PROXY_USER, PROXY_SECRET, domain_only=True, hosts={h: ("127.0.0.1", tls["port"]) for h in HOSTS})
    route = Route(proxy=srv.url, label="SOCKS", user=PROXY_USER, password=PROXY_SECRET)
    args = route.mpv_args()                                    # то, что получает mpv: --http-proxy=http://127.0.0.1:<порт моста>
    proxy_arg = next(a for a in args if a.startswith("--http-proxy="))
    port = int(proxy_arg.rsplit(":", 1)[1])
    ctx = _ssl.create_default_context(cafile=tls["cafile"])
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"CONNECT rr1.googlevideo.com:443 HTTP/1.1\r\nHost: rr1.googlevideo.com:443\r\n\r\n")
    assert s.recv(200).startswith(b"HTTP/1.1 200")
    t = ctx.wrap_socket(s, server_hostname="rr1.googlevideo.com")
    t.sendall(b"GET /videoplayback?x=1 HTTP/1.1\r\nHost: rr1.googlevideo.com\r\nRange: bytes=0-100\r\nConnection: close\r\n\r\n")
    data = b""
    while len(data) < 60000:
        chunk = t.recv(65536)
        if not chunk:
            break
        data += chunk
    assert data.startswith(b"HTTP/1.0 206") or data.startswith(b"HTTP/1.1 206")
    assert srv.atyps and set(srv.atyps) == {3}                  # прокси получил имя хоста, не IP
    t.close()
    # чужой хост мост не пропускает
    s2 = socket.create_connection(("127.0.0.1", port), timeout=10)
    s2.sendall(b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n")
    assert not s2.recv(100).startswith(b"HTTP/1.1 200")
    srv.close()


def test_family_hint_from_stream_url():
    import socket
    from owntube import httpc
    assert httpc.family_of("https://rr1---sn-x.googlevideo.com/videoplayback?ip=2001%3Adb8%3A%3A1&x=1") == socket.AF_INET6
    assert httpc.family_of("https://rr1---sn-x.googlevideo.com/videoplayback?ip=203.0.113.5") == socket.AF_INET
    assert httpc.family_of("https://www.youtube.com/watch?ip=203.0.113.5") == 0
    assert httpc.family_of("https://rr1.googlevideo.com/videoplayback") == 0


def test_video_check_falls_back_to_h264_like_the_player(monkeypatch):
    from owntube import diagnostics
    calls = []

    def probe(st, route):
        calls.append(st["codec"])
        if st["codec"] == "auto":
            raise ValueError("rr1.googlevideo.com: HTTP 403, 0 байт")
        return "rr1.googlevideo.com: HTTP 206, получено 64 КБ"

    monkeypatch.setattr(diagnostics, "probe_stream", probe)
    out = diagnostics.run(Route(), search_fn=lambda route: 1,
                          video_fn=lambda route, codec="auto": {"video": "https://rr1.googlevideo.com/v", "codec": codec})
    step = next(s for s in out["steps"] if s["name"] == "Проверка видео: первые байты потока")
    assert step["ok"] and "H.264" in step["detail"] and calls == ["auto", "h264"]
