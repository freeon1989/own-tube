# -*- coding: utf-8 -*-
"""Провайдеры моделей: список моделей и потоковый чат.

Три вида API:
* OpenAI-совместимый (OpenAI, xAI, DeepSeek, OpenRouter, Gemini через /openai, Ollama, LM Studio, свой сервер);
* Anthropic Messages (api.anthropic.com);
* Google Gemini (generativelanguage.googleapis.com без /openai).

Запросы идут через маршрут own-tube; частные адреса (LAN, VPN, localhost) — напрямую.
Ключ только в заголовке; в журнал и в текст ошибки не попадает.
"""
from __future__ import annotations

import json
from typing import Iterator
from urllib.parse import quote, urlsplit

from .. import httpc, validate
from ..network import Route
from .config import clean_base_url, clean_model_name

MAX_BODY = 2 * 1024 * 1024
LIST_TIMEOUT = 20
CHAT_TIMEOUT = 180  # локальная модель может долго думать до первого слова


class ProviderError(ValueError):
    """Ошибка API с текстом для пользователя (без секретов)."""


def kind_of(base_url: str) -> str:
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    if host == "generativelanguage.googleapis.com" and "/openai" not in parts.path:
        return "gemini"
    return "openai"


def _headers(kind: str, api_key: str) -> dict[str, str]:
    h = {"Accept": "application/json"}
    if not api_key:
        return h
    if kind == "anthropic":
        h.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    elif kind == "gemini":
        h["x-goog-api-key"] = api_key
    else:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _hide(text: str, api_key: str) -> str:
    text = validate.redact(text or "")
    if api_key and len(api_key) >= 6:
        text = text.replace(api_key, "***")
    return text


def _status_error(code: int, body: bytes = b"", api_key: str = "") -> str:
    detail = ""
    try:
        data = json.loads(body.decode("utf-8", "replace")) if body else None
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        elif isinstance(err, str):
            detail = err
        elif isinstance(data, dict) and isinstance(data.get("message"), str):
            detail = data["message"]
    except (ValueError, AttributeError):
        pass
    detail = _hide(detail, api_key)[:200]
    if code in (401, 403):
        base = "Ключ отклонён сервером"
    elif code == 404:
        base = "Сервер не нашёл адрес (проверьте URL, обычно он оканчивается на /v1, и название модели)"
    elif code == 429:
        base = "Сервер просит подождать: слишком много запросов или кончился лимит"
    elif code >= 500:
        base = "Сервер моделей временно недоступен"
    else:
        base = f"Сервер вернул код {code}"
    return base + (f": {detail}" if detail else "")


def _net_error(exc: Exception, api_key: str) -> ProviderError:
    from ..bridge import SocksError

    if isinstance(exc, SocksError):
        return ProviderError("Прокси: " + _hide(str(exc), api_key))
    if isinstance(exc, TimeoutError):
        return ProviderError("Сервер моделей не ответил вовремя")
    if isinstance(exc, OSError):
        return ProviderError("Не удалось связаться с сервером моделей: " + _hide(exc.strerror or str(exc), api_key))
    return ProviderError(_hide(str(exc), api_key)[:200])


def _parse_models(payload: object) -> list[str]:
    names: list[str] = []
    items = []
    if isinstance(payload, dict):
        items = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("models") or []
    elif isinstance(payload, list):
        items = payload
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            n = item.get("id") or item.get("name") or item.get("model")
            if isinstance(n, str):
                names.append(n[7:] if n.startswith("models/") else n)
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


def list_models(base_url: str, api_key: str, route: Route) -> list[str]:
    base = clean_base_url(base_url)
    kind = kind_of(base)
    try:
        code, _, body = httpc.get(base.rstrip("/") + "/models", route, _headers(kind, api_key),
                                  limit=MAX_BODY, timeout=LIST_TIMEOUT)
    except ValueError as exc:
        raise ProviderError(_hide(str(exc), api_key)) from exc
    except Exception as exc:
        raise _net_error(exc, api_key) from exc
    if code != 200:
        raise ProviderError(_status_error(code, body, api_key))
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except ValueError as exc:
        raise ProviderError("Сервер вернул не JSON") from exc
    models = _parse_models(payload)
    if not models:
        raise ProviderError("Сервер ответил, но список моделей пуст")
    return models


def check_connection(base_url: str, api_key: str, route: Route) -> dict:
    models = list_models(base_url, api_key, route)
    via = "напрямую (частный адрес)" if httpc.is_private_host(urlsplit(clean_base_url(base_url)).hostname or "") \
        else ("через прокси" if not route.direct else "напрямую")
    return {"ok": True, "models": models, "detail": f"Ключ принят, моделей: {len(models)} · {via}"}


def chat_completions_url(base_url: str) -> str:
    return clean_base_url(base_url).rstrip("/") + "/chat/completions"


# ---------- чат ----------
def _request_for(kind: str, base: str, model: str, system: str, messages: list[dict], max_tokens: int):
    if kind == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "system": system, "stream": True,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
        return base.rstrip("/") + "/messages", body
    if kind == "gemini":
        m = model[7:] if model.startswith("models/") else model
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "model" if x["role"] == "assistant" else "user", "parts": [{"text": x["content"]}]}
                             for x in messages],
                "generationConfig": {"maxOutputTokens": max_tokens}}
        return f"{base.rstrip('/')}/models/{quote(m, safe='-._')}:streamGenerateContent?alt=sse", body
    body = {"model": model, "stream": True,
            "messages": [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in messages]}
    return chat_completions_url(base), body


def _delta(kind: str, event: dict) -> str:
    if kind == "anthropic":
        if event.get("type") == "content_block_delta":
            d = event.get("delta") or {}
            return d.get("text") or ""
        if event.get("type") == "error":
            raise ProviderError("Ошибка модели: " + str((event.get("error") or {}).get("message") or "")[:200])
        return ""
    if kind == "gemini":
        out = []
        for c in event.get("candidates") or []:
            for p in ((c or {}).get("content") or {}).get("parts") or []:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    out.append(p["text"])
        return "".join(out)
    if isinstance(event.get("error"), (dict, str)):
        err = event["error"]
        raise ProviderError("Ошибка модели: " + str(err.get("message") if isinstance(err, dict) else err)[:200])
    text = []
    for ch in event.get("choices") or []:
        d = (ch or {}).get("delta") or (ch or {}).get("message") or {}
        if isinstance(d.get("content"), str):
            text.append(d["content"])
    return "".join(text)


def chat_stream(cfg: dict, api_key: str, system: str, messages: list[dict], route: Route,
                max_tokens: int = 1024) -> Iterator[str]:
    """Отдаёт текст ответа кусками по мере прихода. Ошибки — ProviderError без секретов."""
    base = clean_base_url(cfg.get("base_url"))
    model = clean_model_name(cfg.get("model"))
    if not model:
        raise ProviderError("В активной конфигурации не указана модель")
    kind = kind_of(base)
    url, body = _request_for(kind, base, model, system, messages, max_tokens)
    headers = _headers(kind, api_key)
    headers.update({"Content-Type": "application/json", "Accept": "text/event-stream, application/json"})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        resp = httpc.open_url("POST", url, route, headers, data, timeout=CHAT_TIMEOUT)
    except ValueError as exc:
        raise ProviderError(_hide(str(exc), api_key)) from exc
    except Exception as exc:
        raise _net_error(exc, api_key) from exc
    try:
        if resp.status != 200:
            raise ProviderError(_status_error(resp.status, resp.read(8192), api_key))
        ctype = (resp.getheader("Content-Type") or "").lower()
        if "event-stream" not in ctype:
            # сервер не умеет потоком — пришёл обычный JSON целиком
            raw = resp.read(MAX_BODY)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise ProviderError("Сервер вернул не JSON") from exc
            if kind == "anthropic":
                text = "".join(b.get("text", "") for b in payload.get("content") or [] if isinstance(b, dict))
            else:
                text = _delta(kind, payload)
            if text:
                yield text
            return
        total = 0
        while True:
            try:
                line = resp.readline(65536)
            except Exception as exc:
                raise _net_error(exc, api_key) from exc
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                event = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            piece = _delta(kind, event)
            if piece:
                total += len(piece)
                if total > MAX_BODY:
                    break
                yield piece
    finally:
        resp.close()
