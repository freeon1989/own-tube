import json

import pytest

from owntube import network, player, search
from owntube.api import Api
from owntube.settings import SettingsStore

STREAM_V = "https://rr1.googlevideo.com/videoplayback?v=1"
STREAM_A = "https://rr2.googlevideo.com/videoplayback?a=1"


def test_normalize_drops_bad_entries_and_builds_thumb_itself():
    items = search.normalize([
        {"id": "dQw4w9WgXcQ", "title": "T", "channel": "C", "duration": 3725, "view_count": 1500000,
         "thumbnails": [{"url": "https://evil.example/x.jpg"}]},
        {"id": "bad id", "title": "x"},
        {"id": "dQw4w9WgXcQ", "title": "dup"},
        "junk", None,
        {"id": "aaaaaaaaaaa", "live_status": "is_live"},
    ])
    assert [i["id"] for i in items] == ["dQw4w9WgXcQ", "aaaaaaaaaaa"]
    assert items[0]["thumb"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    assert items[0]["duration"] == "1:02:05" and items[0]["views"] == "1,5 млн просмотров"
    assert items[1]["live"] and items[1]["duration"] == "" and items[1]["title"] == "Без названия"


def test_pick_streams_and_command_has_no_shell_and_ends_with_url():
    info = {"title": "x", "requested_formats": [
        {"vcodec": "av01", "acodec": "none", "url": STREAM_V, "http_headers": {"User-Agent": "UA/1"}},
        {"vcodec": "none", "acodec": "opus", "url": STREAM_A}]}
    s = player.pick_streams(info)
    cmd = player.build_command("/usr/bin/mpv", s, network.Route("http://127.0.0.1:8080"))
    assert cmd[-2:] == ["--", STREAM_V]
    assert "--audio-file=" + STREAM_A in cmd and "--http-proxy=http://127.0.0.1:8080" in cmd
    assert "--user-agent=UA/1" in cmd and "--ytdl=no" in cmd


def test_pick_streams_rejects_foreign_hosts():
    with pytest.raises(player.PlayerError):
        player.pick_streams({"vcodec": "avc1", "acodec": "aac", "url": "https://evil.example/v"})


def test_socks_proxy_refused_for_mpv():
    with pytest.raises(player.PlayerError):
        player.build_command("mpv", {"title": "x", "video": STREAM_V, "audio": None}, network.Route("socks5://127.0.0.1:1080"))


def test_settings_roundtrip_and_validation(tmp_path):
    st = SettingsStore(tmp_path)
    st.update({"proxy": "socks5://127.0.0.1:1080", "quality": "720"})
    assert SettingsStore(tmp_path).value.proxy == "socks5://127.0.0.1:1080"
    with pytest.raises(ValueError):
        st.update({"proxy": "http://user:pass@h:1"})
    assert "pass" not in (tmp_path / "settings.json").read_text()
    (tmp_path / "settings.json").write_text("{broken")
    assert SettingsStore(tmp_path).value.quality == "best"


def test_api_never_raises_and_validates(tmp_path):
    api = Api(store=SettingsStore(tmp_path), search_fn=lambda q, route: [{"id": "x"}])
    assert api.search("")["ok"] is False
    assert api.search("кошки")["items"] == [{"id": "x"}]
    assert api.play("../etc/passwd")["ok"] is False
    assert api.play("dQw4w9WgXcQ", "4k")["ok"] is False
    assert api.save_settings("nope")["ok"] is False
    json.dumps(api.get_settings())


def test_api_search_passes_proxy(tmp_path):
    seen = {}
    api = Api(store=SettingsStore(tmp_path), search_fn=lambda q, route: seen.update(q=q, proxy=route.proxy) or [])
    api.save_settings({"proxy": "http://127.0.0.1:8118"})
    api.search("test")
    assert seen == {"q": "test", "proxy": "http://127.0.0.1:8118"}


def test_route_provider_abstraction(tmp_path):
    class FakeVpn:  # так будет выглядеть будущий VPN-провайдер
        def current(self):
            return network.Route("http://127.0.0.1:8118", "VPN")

        def check(self):
            return {"ok": True, "detail": "тест"}

    seen = {}
    api = Api(store=SettingsStore(tmp_path), routes=FakeVpn(),
              search_fn=lambda q, route: seen.update(r=route) or [])
    api.search("x")
    assert seen["r"].label == "VPN" and seen["r"].ytdlp_options() == {"proxy": "http://127.0.0.1:8118"}
    assert api.check_route()["ok"]
    assert network.Route().ytdlp_options() == {} and network.Route().mpv_args() == []


def test_manual_proxy_check_unreachable(tmp_path):
    st = SettingsStore(tmp_path)
    st.update({"proxy": "http://127.0.0.1:9"})
    assert Api(store=st).check_route()["ok"] is False
    assert Api(store=SettingsStore(tmp_path / "x")).check_route()["ok"] is True
