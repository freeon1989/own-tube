# -*- coding: utf-8 -*-
"""Вход в Google и VK: cookies из окна входа или из cookies.txt.

Пароли программа не видит: вход идёт на страницах Google/VK в отдельном окне со своим профилем WebView2.
После входа берутся только cookies нужных доменов. Они хранятся зашифрованными (DPAPI на Windows),
в settings.json и в журнал не попадают. yt-dlp получает их во временном файле, который удаляется сразу
после запроса.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import validate
from .secretstore import SecretStore

log = logging.getLogger("owntube.accounts")

KINDS = ("google", "vk")
# Какие домены cookies берём (и какие хосты пускает окно входа через прокси)
COOKIE_DOMAINS = {
    "google": ("youtube.com", "google.com"),
    "vk": ("vk.com", "vkvideo.ru", "vk.ru"),
}
LOGIN_HOSTS = {
    "google": (".google.com", ".google.ru", ".gstatic.com", ".googleapis.com", ".googleusercontent.com",
               ".youtube.com", ".ytimg.com", ".ggpht.com", ".googlevideo.com", ".recaptcha.net"),
    "vk": (".vk.com", ".vk.ru", ".vkvideo.ru", ".userapi.com", ".vk-cdn.net", ".vkuser.net", ".mycdn.me",
           ".okcdn.ru", ".vk-portal.net", ".vkid.ru"),
}
LOGIN_URL = {
    "google": "https://accounts.google.com/ServiceLogin?service=youtube&passive=true&hl=ru"
              "&continue=https%3A%2F%2Fwww.youtube.com%2F",
    "vk": "https://vkvideo.ru/",
}
SIGNED_IN = {
    "google": ("SAPISID", "__Secure-3PAPISID", "LOGIN_INFO"),
    "vk": ("remixsid", "remixnsid"),
}
MAX_COOKIES_TEXT = 256 * 1024


def secret_name(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError("Неизвестный аккаунт")
    return f"cookies-{kind}"


def _domain_ok(domain: str, kind: str) -> bool:
    d = (domain or "").lower().lstrip(".")
    return any(d == base or d.endswith("." + base) for base in COOKIE_DOMAINS[kind])


def _clean_cookie(c: dict, kind: str) -> dict | None:
    name, value, domain = c.get("name"), c.get("value"), c.get("domain")
    if not all(isinstance(x, str) for x in (name, value, domain)):
        return None
    if not name or len(name) > 256 or len(value) > 8192 or not _domain_ok(domain, kind):
        return None
    if any(ch in name + value + domain for ch in "\t\r\n"):
        return None
    path = c.get("path") if isinstance(c.get("path"), str) and c.get("path", "").startswith("/") else "/"
    try:
        expires = int(float(c.get("expires") or 0))
    except (TypeError, ValueError):
        expires = 0
    return {"name": name, "value": value, "domain": domain if domain.startswith(".") else "." + domain.lstrip("."),
            "path": path, "secure": bool(c.get("secure", True)), "expires": max(0, expires),
            "httponly": bool(c.get("httponly"))}


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# own-tube: временный файл, удаляется после запроса"]
    for c in cookies:
        dom = ("#HttpOnly_" if c.get("httponly") else "") + c["domain"]
        lines.append("\t".join([dom, "TRUE", c["path"], "TRUE" if c["secure"] else "FALSE",
                                str(c["expires"]), c["name"], c["value"]]))
    return "\n".join(lines) + "\n"


def parse_netscape(text: str, kind: str) -> list[dict]:
    """cookies.txt (формат Netscape) -> только cookies нужных доменов."""
    if not isinstance(text, str) or len(text) > MAX_COOKIES_TEXT * 4:
        raise ValueError("Файл cookies слишком большой или повреждён")
    out = []
    for raw in text.splitlines():
        line = raw.strip("\r")
        httponly = False
        if line.startswith("#HttpOnly_"):
            line, httponly = line[len("#HttpOnly_"):], True
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _, path, secure, expires, name, value = parts
        c = _clean_cookie({"domain": domain, "path": path, "secure": secure.upper() == "TRUE",
                           "expires": expires, "name": name, "value": value, "httponly": httponly}, kind)
        if c:
            out.append(c)
    return out


def signed_in(cookies: list[dict], kind: str) -> bool:
    names = {c["name"] for c in cookies}
    return any(n in names for n in SIGNED_IN[kind])


class Accounts:
    def __init__(self, secrets: SecretStore, data_dir: Path):
        self._secrets = secrets
        self._dir = Path(data_dir)
        self._procs: dict[str, subprocess.Popen] = {}
        self._info: dict[str, dict] = {}
        self._lock = threading.Lock()

    # --- cookies ---
    def cookies(self, kind: str) -> list[dict]:
        raw = self._secrets.get(secret_name(kind))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        cookies = data.get("cookies") if isinstance(data, dict) else None
        now = time.time()
        return [c for c in (cookies or []) if isinstance(c, dict) and (not c.get("expires") or c["expires"] > now)]

    def cookie_text(self) -> str:
        """Все cookies вошедших аккаунтов в формате Netscape (для yt-dlp). Не логировать."""
        allc = []
        for k in KINDS:
            allc += self.cookies(k)
        return to_netscape(allc) if allc else ""

    def cookie_header(self, kind: str, host: str) -> str:
        host = host.lower()
        parts = []
        for c in self.cookies(kind):
            d = c["domain"].lstrip(".")
            if host == d or host.endswith("." + d):
                parts.append(f"{c['name']}={c['value']}")
        return "; ".join(parts)

    def save_cookies(self, kind: str, cookies: list[dict], info: dict | None = None, origin: str = "window") -> None:
        clean = [x for x in (_clean_cookie(c, kind) for c in cookies) if x]
        if not signed_in(clean, kind):
            raise ValueError("В cookies нет признаков входа в аккаунт" + (" Google/YouTube" if kind == "google" else " VK"))
        payload = json.dumps({"cookies": clean, "info": info or {}, "origin": origin, "saved": int(time.time())},
                             ensure_ascii=False)
        if len(payload) > MAX_COOKIES_TEXT:
            raise ValueError("Слишком много cookies")
        self._secrets.set(payload, name=secret_name(kind))
        with self._lock:
            self._info.pop(kind, None)

    def import_text(self, kind: str, text: str) -> int:
        cookies = parse_netscape(text, kind)
        self.save_cookies(kind, cookies, origin="file")
        return len(cookies)

    def logout(self, kind: str) -> None:
        self._secrets.delete(secret_name(kind))
        with self._lock:
            p = self._procs.pop(kind, None)
            self._info.pop(kind, None)
        if p and p.poll() is None:
            p.kill()
        prof = self.profile_dir(kind)
        if prof.exists():
            shutil.rmtree(prof, ignore_errors=True)

    def profile_dir(self, kind: str) -> Path:
        secret_name(kind)
        return self._dir / "profiles" / kind

    def _meta(self, kind: str) -> dict:
        raw = self._secrets.get(secret_name(kind))
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def status(self) -> dict:
        out = {}
        for k in KINDS:
            with self._lock:
                p = self._procs.get(k)
            running = bool(p and p.poll() is None)
            if p and not running:
                with self._lock:
                    self._procs.pop(k, None)
                self._secrets.reload(secret_name(k))  # окно входа писало из другого процесса
            meta = self._meta(k)
            cookies = self.cookies(k)
            info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
            out[k] = {
                "signed_in": signed_in(cookies, k),
                "login_window": running,
                "origin": meta.get("origin") or "",
                "name": validate.clean_text(info.get("name"), 80),
                "avatar": info.get("avatar") if validate.is_thumb_url(info.get("avatar")) else "",
            }
        return out

    # --- окно входа: отдельный процесс со своим профилем WebView2 ---
    def start_login(self, kind: str) -> None:
        secret_name(kind)
        with self._lock:
            p = self._procs.get(kind)
            if p and p.poll() is None:
                return
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--login", kind]
            else:
                cmd = [sys.executable, "-m", "owntube", "--login", kind]
            flags = 0x08000000 if sys.platform.startswith("win") else 0
            self._procs[kind] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, shell=False, creationflags=flags,
                                                 env={**os.environ, "OWNTUBE_HOME": str(self._dir)})


def sapisid_hash(sapisid: str, origin: str = "https://www.youtube.com", now: int | None = None) -> str:
    ts = int(now if now is not None else time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def google_profile(acc: Accounts, route) -> dict:
    """Имя и аватар аккаунта (по возможности). Ошибки не страшны: вход от этого не зависит."""
    from . import httpc

    sap = next((c["value"] for c in acc.cookies("google") if c["name"] in ("SAPISID", "__Secure-3PAPISID")), "")
    if not sap:
        return {}
    body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": "2.20251120.00.00", "hl": "ru"}}})
    headers = {"Cookie": acc.cookie_header("google", "www.youtube.com"), "Authorization": sapisid_hash(sap),
               "X-Origin": "https://www.youtube.com", "Origin": "https://www.youtube.com",
               "Content-Type": "application/json"}
    resp = httpc.open_url("POST", "https://www.youtube.com/youtubei/v1/account/account_menu?prettyPrint=false",
                          route, headers, body.encode(), timeout=15)
    try:
        if resp.status != 200:
            return {}
        data = json.loads(resp.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    finally:
        resp.close()
    found: dict = {}

    def walk(x):
        if isinstance(x, dict):
            hdr = x.get("activeAccountHeaderRenderer")
            if isinstance(hdr, dict):
                name = (hdr.get("accountName") or {}).get("simpleText")
                thumbs = (hdr.get("accountPhoto") or {}).get("thumbnails") or []
                found["name"] = name or ""
                if thumbs and isinstance(thumbs[-1], dict):
                    found["avatar"] = thumbs[-1].get("url") or ""
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return found
