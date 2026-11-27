import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from owntube.ai.config import clean_base_url, clean_config
from owntube.ai.providers import check_connection
from owntube.api import Api
from owntube.network import Route
from owntube.secretstore import MemoryBackend, SecretStore
from owntube.settings import SettingsStore

FAKE_KEY = "sk-test-not-a-real-key-0000"


def _models_server(models=("local/demo-a", "local/demo-b"), expected_key=FAKE_KEY):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            if self.path.rstrip("/") != "/v1/models":
                self.send_error(404)
                return
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer " + expected_key:
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"data": [{"id": m} for m in models]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}/v1"


def test_base_url_http_only_for_private():
    assert clean_base_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"
    assert clean_base_url("http://10.0.0.1:1234/v1") == "http://10.0.0.1:1234/v1"
    assert clean_base_url("http://192.168.1.10/v1") == "http://192.168.1.10/v1"
    assert clean_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert clean_base_url("https://api.example.com/v1") == "https://api.example.com/v1"
    with pytest.raises(ValueError, match="https"):
        clean_base_url("http://example.com/v1")
    with pytest.raises(ValueError, match="отдельное поле"):
        clean_base_url("https://user:key@api.example.com/v1")


def test_ai_config_saved_without_key_on_disk(tmp_path):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=st, start_downloader=False)
    r = api.save_ai_config({
        "name": "Домашний",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": FAKE_KEY,
        "model": "local/demo",
        "vision": True,
        "threads": 2,
        "context": 130000,
        "active": True,
    })
    assert r["ok"]
    cfg = r["config"]
    assert cfg["api_key_set"] and cfg["vision"] and cfg["active"] and cfg["threads"] == 2
    assert "api_key" not in cfg
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert "api_key" not in json.loads(text).get("ai_configs", [{}])[0]
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert FAKE_KEY.encode() not in f.read_bytes()
    listed = api.get_settings()["settings"]["ai_configs"]
    assert listed[0]["model"] == "local/demo" and listed[0]["api_key_set"]


def test_check_ai_key_lists_models(tmp_path):
    httpd, url = _models_server()
    try:
        st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
        api = Api(store=st, start_downloader=False)
        r = api.check_ai_key({"base_url": url, "api_key": FAKE_KEY})
        assert r["ok"] and r["models"] == ["local/demo-a", "local/demo-b"]
        bad = api.check_ai_key({"base_url": url, "api_key": "sk-wrong"})
        assert bad["ok"] is False
        assert FAKE_KEY not in (bad.get("error") or "")
        saved = api.save_ai_config({"name": "T", "base_url": url, "api_key": FAKE_KEY, "model": "local/demo-a"})
        again = api.check_ai_key({"id": saved["config"]["id"]})
        assert again["ok"] and "local/demo-a" in again["models"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_check_connection_uses_route_proxy(tmp_path):
    # without a listening proxy the check must fail with a proxy error, not skip the proxy
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=st, start_downloader=False)
    api.save_settings({"proxy": "127.0.0.1:9"})
    r = api.check_ai_key({"base_url": "http://127.0.0.1:1/v1", "api_key": FAKE_KEY})
    assert r["ok"] is False
    assert "напрямую" not in (r.get("error") or "").lower()


def test_one_active_config(tmp_path):
    st = SettingsStore(tmp_path, secrets=SecretStore(tmp_path, MemoryBackend()))
    api = Api(store=st, start_downloader=False)
    a = api.save_ai_config({"name": "A", "base_url": "http://127.0.0.1:1/v1", "active": True})
    b = api.save_ai_config({"name": "B", "base_url": "http://127.0.0.1:2/v1", "active": True})
    cfgs = api.get_settings()["settings"]["ai_configs"]
    active = [c["name"] for c in cfgs if c["active"]]
    assert active == ["B"]
    api.delete_ai_config(a["config"]["id"])
    names = [c["name"] for c in api.get_settings()["settings"]["ai_configs"]]
    assert names == ["B"]


def test_clean_config_defaults():
    cfg = clean_config({"name": "X", "base_url": "https://api.example.com/v1"})
    assert cfg["threads"] == 1 and cfg["context"] == 130000 and cfg["vision"] is False
    with pytest.raises(ValueError):
        clean_config({"name": "X", "base_url": "ftp://x"})
