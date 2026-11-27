import json
import socket
import threading
import zipfile

import pytest

from owntube import bridge, player, search, sponsorblock, takeout, validate
from owntube.api import Api
from owntube.db import Store, clean_item
from owntube.settings import SettingsStore

YT = {"source": "yt", "id": "dQw4w9WgXcQ", "title": "Never", "channel": "Rick", "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw"}
VK = {"source": "vk", "id": "-77521_162222515", "title": "VK", "thumb": "https://sun9-55.userapi.com/x.jpg"}


@pytest.fixture
def db(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# ---------- проверки ----------
def test_vk_and_youtube_links():
    assert validate.parse_vk_link("https://vk.com/videos-77521?z=video-77521_162222515%2Fclub77521") == "-77521_162222515"
    assert validate.parse_vk_link("vkvideo.ru/video-1_2") == "-1_2"
    assert validate.parse_vk_link("https://evil.com/video-1_2") is None
    assert validate.parse_vk_link("http://vk.com/video-1_2") is None
    assert validate.parse_youtube_link("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert validate.parse_youtube_link("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert validate.parse_youtube_link("https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ") is None
    assert validate.is_stream_url("https://vkvd431.okcdn.ru/?x=1", "vk")
    assert not validate.is_stream_url("https://vkvd431.okcdn.ru/?x=1", "yt")
    assert not validate.is_thumb_url("https://evil.com/a.jpg") and validate.is_thumb_url(VK["thumb"])


def test_clean_item_rebuilds_youtube_thumb_and_drops_bad_vk_thumb():
    v = clean_item({**YT, "thumb": "https://evil.com/x.jpg"})
    assert v["thumb"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    assert clean_item({**VK, "thumb": "javascript:alert(1)"})["thumb"] == ""
    with pytest.raises(ValueError):
        clean_item({"source": "vk", "id": "dQw4w9WgXcQ"})


# ---------- база ----------
def test_db_lists(db):
    db.add_history(YT)
    db.add_history(VK)
    assert [h["source"] for h in db.history()] == ["vk", "yt"]
    db.set_mark("later", YT, True)
    db.set_mark("favorite", VK, True)
    assert db.marks_of("yt", YT["id"]) == ["later"]
    db.set_mark("later", YT, False)
    assert db.marks("later") == []
    pid = db.create_playlist("  Аниме  ")
    assert db.create_playlist("Аниме") == pid
    db.add_to_playlist(pid, YT)
    db.add_to_playlist(pid, VK)
    assert db.playlists()[0]["count"] == 2
    db.remove_from_playlist(pid, "yt", YT["id"])
    assert len(db.playlist_items(pid)) == 1
    db.delete_playlist(pid)
    assert db.playlists() == []
    db.subscribe(YT["channel_id"], "Rick")
    assert db.is_subscribed(YT["channel_id"])
    with pytest.raises(ValueError):
        db.subscribe("not-a-channel", "x")
    dl = db.add_download(YT, "720")
    db.update_download(dl, status="running", progress=10, evil="x")
    assert db.downloads()[0]["status"] == "running"
    db.fail_unfinished_downloads()
    assert db.get_download(dl)["status"] == "error"
    assert db.counts()["favorite"] == 1


def test_db_sql_injection_is_just_text(db):
    pid = db.create_playlist("x'); DROP TABLE videos; --")
    db.add_to_playlist(pid, {**YT, "title": "'; DELETE FROM history; --"})
    assert db.playlist_items(pid)[0]["title"].startswith("'; DELETE")


def test_placeholder_title_does_not_overwrite_real_title(db):
    db.upsert_video(YT)
    db.upsert_video({"source": "yt", "id": YT["id"], "title": "Видео из Takeout"})
    db.set_mark("later", {"source": "yt", "id": YT["id"]}, True)
    assert db.marks("later")[0]["title"] == "Never"


# ---------- Takeout ----------
def make_takeout(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Takeout/YouTube/subscriptions/subscriptions.csv",
                   "Channel Id,Channel Url,Channel Title\nUCuAXFkgsw1L7xaCfnd5JJOw,http://x,Rick\nbad,row,here\n")
        z.writestr("Takeout/YouTube/history/watch-history.json", json.dumps([
            {"title": "Watched Never", "titleUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
             "subtitles": [{"name": "Rick", "url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"}],
             "time": "2026-01-01T00:00:00Z"},
            {"title": "Посетили сайт", "titleUrl": "https://example.com"}]))
        z.writestr("Takeout/YouTube/playlists/Музыка-videos.csv", "Video ID,Time\ndQw4w9WgXcQ,2026\nnot_an_id,2026\n")
        z.writestr("Takeout/other/secret.txt", "не читается")


def test_takeout_zip(tmp_path, db):
    p = tmp_path / "t.zip"
    make_takeout(p)
    s = takeout.import_path(p, db)
    assert s == {"subscriptions": 1, "history": 1, "playlists": 1, "playlist_videos": 1, "files": 3}
    assert db.history()[0]["title"] == "Never" and db.history()[0]["channel"] == "Rick"
    assert db.playlists()[0]["name"] == "Музыка"


def test_takeout_rejects_unrelated(tmp_path, db):
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.txt", "x")
    with pytest.raises(ValueError):
        takeout.import_path(p, db)


# ---------- лента, SponsorBlock, форматы ----------
RSS = b"""<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
<title>Chan</title><entry><yt:videoId>dQw4w9WgXcQ</yt:videoId><yt:channelId>UCuAXFkgsw1L7xaCfnd5JJOw</yt:channelId><title>T</title>
<author><name>Rick</name></author><published>2026-01-01T00:00:00+00:00</published>
<media:group><media:community><media:statistics views="1234"/></media:community></media:group></entry>
<entry><yt:videoId>bad</yt:videoId></entry></feed>"""


def test_rss():
    items = search.parse_rss(RSS)
    assert len(items) == 1 and items[0]["views"] == "1 тыс. просмотров" and items[0]["channel_id"].startswith("UC")
    with pytest.raises(ValueError):
        search.parse_rss(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>')


def test_sponsorblock_pick():
    data = [{"videoID": "dQw4w9WgXcQ", "segments": [{"segment": [10, 20.5], "actionType": "skip"},
                                                   {"segment": [30, 25]}, {"segment": [40, 50], "actionType": "mute"}]},
            {"videoID": "other_video", "segments": [{"segment": [1, 2]}]}]
    segs = sponsorblock.pick(data, "dQw4w9WgXcQ")
    assert segs == [(10.0, 20.5)]
    assert sponsorblock.script_opt(segs) == "10-20.5"
    cmd = player.build_command("mpv", {"title": "t", "video": "https://a.googlevideo.com/v", "audio": None}, segments="10-20.5,30-40")
    assert any(c == "--script-opts=owntube_segments=10-20.5+30-40" for c in cmd)


def test_format_selector():
    assert player.format_selector("720") == "bv*[height<=720]+ba/b[height<=720]/b"
    assert player.format_selector("best", "h264").startswith("bv*[vcodec^=avc1]+ba[acodec^=mp4a]")
    with pytest.raises(ValueError):
        player.format_selector("720", "av2")


# ---------- настройки ----------
def test_settings_strict(tmp_path):
    st = SettingsStore(tmp_path)
    st.update({"theme": "neon", "assistant": "kate", "codec": "h264", "sb_categories": ["sponsor"], "sponsorblock": False})
    assert SettingsStore(tmp_path).value.theme == "neon"
    for bad in ({"theme": "x"}, {"assistant": "x"}, {"sb_categories": ["evil"]}, {"sponsorblock": "yes"},
                {"download_dir": "relative/path"}, {"mpv_path": "a\x00b"}):
        with pytest.raises(ValueError):
            st.update(bad)


def test_api_library_and_assistant_needs_config(tmp_path):
    api = Api(store=SettingsStore(tmp_path), start_downloader=False)
    assert api.set_mark("favorite", YT, True)["ok"]
    assert api.library("favorite")["items"][0]["id"] == YT["id"]
    assert api.item_state(YT)["marks"] == ["favorite"]
    assert api.create_playlist("P", YT)["ok"]
    assert api.library("nope")["ok"] is False
    assert api.library("playlist", "1; DROP")["ok"] is False
    r = api.assistant_send("привет")
    assert r["ok"] is False and "ИИ" in r["error"]  # без конфигурации ИИ — понятная ошибка в чате
    assert api.import_takeout()["ok"] is False  # без окна диалога нет — и пути из интерфейса не принимаются


# ---------- мост HTTP→SOCKS ----------
def _serve_once(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_bridge_end_to_end(monkeypatch):
    echo, echo_port = _serve_once(lambda c: (c.sendall(b"pong:" + c.recv(100)), c.close()))
    seen = {}

    def socks5(c):
        assert c.recv(3) == b"\x05\x01\x00"
        c.sendall(b"\x05\x00")
        head = c.recv(5)
        name = c.recv(head[4])
        port = int.from_bytes(c.recv(2), "big")
        seen["target"] = (name.decode(), port)
        up = socket.create_connection(("127.0.0.1", echo_port))
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        bridge._pipe(c, up)

    srv, socks_port = _serve_once(socks5)
    monkeypatch.setattr(bridge, "ALLOWED_PORTS", (443,))
    b = bridge.Bridge(f"socks5h://127.0.0.1:{socks_port}")
    try:
        s = socket.create_connection(("127.0.0.1", b.port))
        s.sendall(b"CONNECT rr1---sn-x.googlevideo.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert s.recv(100).startswith(b"HTTP/1.1 200")
        s.sendall(b"ping")
        assert s.recv(100) == b"pong:ping"
        assert seen["target"] == ("rr1---sn-x.googlevideo.com", 443)
        s.close()
        for req, code in ((b"CONNECT evil.com:443 HTTP/1.1\r\n\r\n", b"403"), (b"CONNECT 1.2.3.4:443 HTTP/1.1\r\n\r\n", b"403"),
                          (b"CONNECT a.googlevideo.com:22 HTTP/1.1\r\n\r\n", b"403"), (b"GET http://x/ HTTP/1.1\r\n\r\n", b"405")):
            s = socket.create_connection(("127.0.0.1", b.port))
            s.sendall(req)
            assert code in s.recv(100)
            s.close()
    finally:
        b.close()
        srv.close()
        echo.close()
