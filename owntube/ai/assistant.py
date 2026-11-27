# -*- coding: utf-8 -*-
"""Помощницы: характер, системная подсказка, история чата, разбор действий, фоновые ответы.

Модель предлагает действия строками [[search: …]], [[play: ID]], [[open: раздел]].
Программа их проверяет и показывает кнопками; действия с аккаунтом (лайк, подписка, комментарий)
не выполняются сами — только после подтверждения пользователя.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from typing import Callable, Iterator

from .. import validate

PERSONAS = {
    "rei": ("Рэй", "Ты спокойная, точная и чуть загадочная. Отвечаешь коротко и по делу, без лишних эмоций."),
    "yuki": ("Юки", "Ты мягкая и заботливая. Объясняешь подробно и бережно, любишь спокойные каналы и уютные видео."),
    "kate": ("Кейт", "Ты бойкая, шутишь и по-дружески подначиваешь. Отлично подбираешь, что посмотреть вечером."),
    "lila": ("Лила", "Ты энергичная, обожаешь музыку и танцы, всегда готова собрать новый плейлист."),
    "nina": ("Нина", "Ты любишь новости и тренды, всё проверяешь и честно говоришь, когда не уверена."),
}
SECTIONS = {
    "home": "Главная", "shorts": "Shorts", "subs": "Подписки", "history": "История", "later": "Смотреть позже",
    "favorite": "Избранное", "playlists": "Плейлисты", "downloads": "Загрузки", "vk": "VK Видео",
    "settings": "Настройки",
}
ACCOUNT_ACTIONS = ("like", "subscribe", "comment")
ACTION_RE = re.compile(r"\[\[\s*(search|play|open|like|subscribe|comment)\s*:\s*([^\]\n]{1,200})\]\]", re.I)
MAX_MESSAGE = 4000
CHARS_PER_TOKEN = 3  # грубо для русского текста


def persona(aid: str) -> tuple[str, str]:
    return PERSONAS.get(aid) or PERSONAS["rei"]


def clean_message(text: object) -> str:
    if not isinstance(text, str):
        raise ValueError("Сообщение должно быть текстом")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("Пустое сообщение")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"Сообщение длиннее {MAX_MESSAGE} символов")
    return text


def clean_context(ctx: object) -> dict:
    """Что сейчас на экране: раздел и до 12 видимых роликов. Всё проверяется."""
    out = {"view": "", "title": "", "items": []}
    if not isinstance(ctx, dict):
        return out
    out["view"] = validate.clean_text(ctx.get("view"), 40)
    out["title"] = validate.clean_text(ctx.get("title"), 120)
    for it in (ctx.get("items") or [])[:12]:
        if not isinstance(it, dict):
            continue
        vid = it.get("id")
        if it.get("source", "yt") == "yt" and validate.is_video_id(vid):
            out["items"].append({"id": vid, "title": validate.clean_text(it.get("title"), 120),
                                 "channel": validate.clean_text(it.get("channel"), 80)})
    return out


def system_prompt(aid: str, ctx: dict, can_account: bool = False) -> str:
    name, character = persona(aid)
    lines = [
        f"Ты — {name}, помощница в программе own-tube (клиент YouTube и VK Видео для Windows).",
        character,
        "Отвечай по-русски, дружелюбно и кратко (обычно 1–4 предложения). Ты взрослая аниме-героиня; "
        "никакого откровенного содержания.",
        "Ты можешь предлагать действия — отдельной строкой в конце ответа, ровно в таком виде:",
        "[[search: запрос]] — найти видео на YouTube;",
        "[[play: ID]] — включить ролик (ID из 11 символов, только из списка на экране или из результатов поиска);",
        "[[open: раздел]] — открыть раздел: " + ", ".join(SECTIONS) + ".",
        "Не выдумывай ID роликов. Если не знаешь ID — предложи [[search: …]].",
        "Действия с аккаунтом (лайк, подписка, комментарий) сама не делаешь: можно предложить [[like: ID]] или "
        "[[subscribe: канал]], пользователь подтвердит сам." if can_account else
        "Лайки, подписки в аккаунте и комментарии ты не делаешь.",
        "Никогда не проси пароли, коды и ключи.",
    ]
    if ctx.get("view"):
        lines.append(f"Сейчас открыт раздел: {ctx.get('title') or ctx['view']}.")
    if ctx.get("items"):
        lines.append("На экране ролики:")
        for it in ctx["items"]:
            lines.append(f"- {it['id']}: {it['title']}" + (f" ({it['channel']})" if it["channel"] else ""))
    return "\n".join(lines)


def parse_actions(text: str, allowed_ids: set[str] | None = None) -> tuple[str, list[dict]]:
    """Вырезает [[…]] из ответа. Возвращает (чистый текст, проверенные действия)."""
    actions = []
    for m in ACTION_RE.finditer(text or ""):
        kind, arg = m.group(1).lower(), m.group(2).strip()
        if kind == "search":
            try:
                actions.append({"kind": "search", "query": validate.clean_query(arg)})
            except ValueError:
                pass
        elif kind == "play":
            vid = validate.parse_youtube_link(arg) or (arg if validate.is_video_id(arg) else "")
            if vid:
                actions.append({"kind": "play", "id": vid, "known": bool(allowed_ids and vid in allowed_ids)})
        elif kind == "open":
            key = arg.lower()
            key = next((k for k, v in SECTIONS.items() if key in (k, v.lower())), "")
            if key:
                actions.append({"kind": "open", "section": key, "label": SECTIONS[key]})
        else:
            actions.append({"kind": kind, "arg": validate.clean_text(arg, 120), "confirm": True})
        if len(actions) >= 5:
            break
    clean = ACTION_RE.sub("", text or "").strip()
    return clean, actions


def trim_history(history: list[dict], context_tokens: int, system: str, reserve: int = 1024) -> list[dict]:
    """Последние сообщения, которые помещаются в окно контекста модели."""
    budget = max(1000, int(context_tokens) - reserve) * CHARS_PER_TOKEN - len(system)
    out: list[dict] = []
    for m in reversed(history):
        cost = len(m["content"]) + 16
        if out and budget - cost < 0:
            break
        budget -= cost
        out.append(m)
    out.reverse()
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out[-40:]


class Jobs:
    """Ответы модели идут в фоне; интерфейс забирает текст опросом assistant_poll."""

    def __init__(self):
        self._jobs: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, gen_fn: Callable[[], Iterator[str]], on_done: Callable[[str, str], list] | None = None) -> int:
        jid = next(self._ids)
        job = {"text": "", "done": False, "error": "", "actions": [], "t": time.time(), "cancel": False}
        with self._lock:
            # старые законченные задачи не копим
            for k in [k for k, j in self._jobs.items() if j["done"] and time.time() - j["t"] > 600]:
                self._jobs.pop(k, None)
            self._jobs[jid] = job

        def run():
            try:
                for piece in gen_fn():
                    if job["cancel"]:
                        break
                    with self._lock:
                        job["text"] += piece
            except Exception as exc:  # ProviderError и прочее — в чат, без секретов
                job["error"] = validate.redact(str(exc))[:400] or "Ошибка модели"
            try:
                if on_done:
                    job["actions"] = on_done(job["text"], job["error"]) or []
            except Exception:
                pass
            job["done"] = True
            job["t"] = time.time()

        threading.Thread(target=run, name=f"owntube-ai-{jid}", daemon=True).start()
        return jid

    def poll(self, jid: int) -> dict:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                raise ValueError("Ответ не найден")
            return {"text": job["text"], "done": job["done"], "error": job["error"], "actions": job["actions"]}

    def cancel(self, jid: int) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job:
                job["cancel"] = True
