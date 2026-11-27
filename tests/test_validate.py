import pytest

from owntube import validate as v


def test_video_id():
    assert v.is_video_id("dQw4w9WgXcQ")
    for bad in ("", "short", "dQw4w9WgXcQ1", "dQw4w9Wg;cQ", "dQw4w9WgXc ", None, 5, "--help-----"[:11] + "x"):
        assert not v.is_video_id(bad)
    assert not v.is_video_id("-" * 11 + "\n")


def test_query():
    assert v.clean_query("  привет \x00мир ") == "привет мир"
    assert len(v.clean_query("a" * 999)) == v.MAX_QUERY_LEN
    for bad in ("", "   ", "\x01\x02", None, 5):
        with pytest.raises(ValueError):
            v.clean_query(bad)


def test_proxy():
    assert v.clean_proxy("") == ""
    assert v.clean_proxy(" SOCKS5://127.0.0.1:1080 ") == "socks5://127.0.0.1:1080"
    assert v.clean_proxy("http://[::1]:8080") == "http://[::1]:8080"
    assert v.clean_proxy("127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert v.clean_proxy("127.0.0.1:8080", default_scheme="http") == "http://127.0.0.1:8080"
    for bad in ("socks5://u:p@127.0.0.1:1080", "ftp://x:1", "http://host", "http://h:99999", "http://h:1/x",
                "http://h:1 --x", "http://@h:1", "host-without-port"):
        with pytest.raises(ValueError):
            v.clean_proxy(bad)
    assert v.is_http_proxy("http://h:1") and not v.is_http_proxy("socks5://h:1")
    assert "p@ss" not in v.redact("socks5://u:p@ss@127.0.0.1:1080")


def test_stream_url():
    assert v.is_stream_url("https://rr1---sn-x.googlevideo.com/videoplayback?a=1")
    assert v.is_stream_url("https://manifest.googlevideo.com/api/manifest/hls_playlist/x")
    for bad in ("http://rr1.googlevideo.com/x", "https://evil.com/x", "https://googlevideo.com.evil.com/x",
                "file:///etc/passwd", "--script=x", "https://a.googlevideo.com/x y", "https://x.com/?.googlevideo.com", None):
        assert not v.is_stream_url(bad)


def test_quality():
    assert v.clean_quality("720") == "720"
    with pytest.raises(ValueError):
        v.clean_quality("4k")
