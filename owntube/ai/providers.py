# -*- coding: utf-8 -*-
"""Проверка ключа и список моделей. Чат — заготовка для второго этапа.

Запросы идут через текущий Route (тот же прокси, что у клиента). Ключ только в заголовке,
в журнал и в текст ошибки не попадает.
"""
from __future__ import annotations

import json
import ssl
from urllib.parse import urlsplit

from .. import validate
from ..bridge import SocksError, http_connect, socks_connect
from ..network import Route
from .config import clean_base_url, clean_model_name

MAX_BODY = 2 * 1024 * 1024
TIMEOUT = 20


def _connect(host: str, port: int, scheme: str, route: Route, timeout: float):
    if route.direct:
        sock = __import__("socket").create_connection((host, port), timeout=timeout)
    elif validate.is_socks_proxy(route.proxy):
        sock = socks_connect(route.proxy, host, port, timeout=timeout,
                             user=route.user, password=route.password)
    else:
        sock = http_connect(route.proxy, host, port, timeout=timeout,
                            user=route.user, password=route.password)
    if scheme == "https":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.settimeout(timeout)
    return sock


def request(method: str, url: str, route: Route, headers: dict[str, str] | None = None,
            body: bytes | None = None, timeout: float = TIMEOUT) -> tuple[int, bytes]:
    """HTTP(S) через Route. Редиректы не следуем. URL уже должен быть проверен."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Поддерживаются только http и https")
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    hdrs = {"Host": host if port in (80, 443) else f"{host}:{port}",
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "own-tube"}
    if body is not None:
        hdrs["Content-Length"] = str(len(body))
    if headers:
        hdrs.update(headers)
    lines = [f"{method} {path} HTTP/1.1"]
    for k, v in hdrs.items():
        # не кладём значение заголовка Authorization в исключение
        if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
            raise ValueError("Некорректный заголовок")
        lines.append(f"{k}: {v}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")
    sock = _connect(host, port, parts.scheme, route, timeout)
    try:
        sock.sendall(raw)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_HEADER:
                raise ValueError("Слишком длинный ответ сервера")
        head, _, rest = data.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        bits = first.split(" ", 2)
        code = int(bits[1]) if len(bits) >= 2 and bits[1].isdigit() else 0
        body_buf = rest
        while len(body_buf) <= MAX_BODY:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body_buf += chunk
            if len(body_buf) > MAX_BODY:
                raise ValueError("Слишком большой ответ сервера")
        return code, body_buf
    finally:
        try:
            sock.close()
        except OSError:
            pass


MAX_HEADER = 16384


def _auth_headers(base_url: str, api_key: str) -> dict[str, str]:
    host = (urlsplit(base_url).hostname or "").lower()
    if host.endswith("anthropic.com"):
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {api_key}"}


def _models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def _parse_models(payload: object) -> list[str]:
    names: list[str] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    names.append(item["id"])
                elif isinstance(item, str):
                    names.append(item)
        elif isinstance(payload.get("models"), list):
            for item in payload["models"]:
                if isinstance(item, dict) and isinstance(item.get("id") or item.get("name"), str):
                    names.append(item.get("id") or item.get("name"))
                elif isinstance(item, str):
                    names.append(item)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                names.append(item["id"])
            elif isinstance(item, str):
                names.append(item)
    out, seen = [], set()
    for n in names:
        try:
            n = clean_model_name(n)
        except ValueError:
            continue
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _status_error(code: int) -> str:
    if code in (401, 403):
        return "Ключ отклонён сервером"
    if code == 404:
        return "Сервер не нашёл список моделей (проверьте URL, обычно он оканчивается на /v1)"
    if code == 429:
        return "Сервер просит подождать (слишком много запросов)"
    if code >= 500:
        return "Сервер моделей временно недоступен"
    return f"Сервер вернул код {code}"


def list_models(base_url: str, api_key: str, route: Route) -> list[str]:
    url = _models_url(clean_base_url(base_url))
    try:
        code, body = request("GET", url, route, headers=_auth_headers(url, api_key))
    except SocksError as exc:
        raise ValueError(validate.redact(str(exc))) from exc
    except OSError as exc:
        raise ValueError("Не удалось связаться с сервером моделей: " + (exc.strerror or str(exc))) from exc
    if code != 200:
        raise ValueError(_status_error(code))
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except ValueError as exc:
        raise ValueError("Сервер вернул не JSON") from exc
    models = _parse_models(payload)
    if not models:
        raise ValueError("Ключ принят, но список моделей пуст")
    return models


def check_connection(base_url: str, api_key: str, route: Route) -> dict:
    """Проверка ключа: GET /models. Возвращает {ok, models} или бросает ValueError без секретов."""
    if not api_key:
        raise ValueError("Введите API-ключ")
    models = list_models(base_url, api_key, route)
    return {"ok": True, "models": models, "detail": f"Ключ принят, моделей: {len(models)}"}


def chat_completions_url(base_url: str) -> str:
    """Заготовка для второго этапа: адрес chat/completions."""
    return clean_base_url(base_url).rstrip("/") + "/chat/completions"
