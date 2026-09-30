"""Приём заявок с телефона: пересылка уведомлений Viber (MacroDroid и т.п.) на адрес бота.

POST https://<домен>/ingest?key=<INGEST_KEY>
Тело — JSON или форма с полями: title (заголовок уведомления), text (текст), app (необязательно).
Берём только уведомления, в заголовке или тексте которых есть одно из слов VIBER_GROUPS
(по умолчанию «попутчик») — личные переписки в бот не попадают.
"""
import collections
import hmac
import json
import logging
import time
import unicodedata
import os
import re
from datetime import datetime

from aiohttp import web

from .db import RidesDB
from .notifier import Notifier
from .parsing import DRIVER, PASSENGER, parse_message
from .posts import post_from_group
from .timeparse import TZ

log = logging.getLogger("rides.ingest")

# Последние входящие уведомления — для команды /ingestlog (диагностика)
RECENT: collections.deque = collections.deque(maxlen=30)

# «Андрей: Сегодня могу…» / «Андрей @ Попутчики: …» — отделяем автора от текста
_SENDER_RE = re.compile(r"^\s*([^:\n]{1,40}?):\s+(.+)$", re.S)


# По умолчанию читаем только эти Viber-группы (сравнение по названию, без эмодзи и регистра)
DEFAULT_VIBER_GROUPS = "попутчики_md,попутчики приднестровье"


def _norm(s: str) -> str:
    """«🚘Попутчики_ⓂⒹ🚘» → «попутчики_md»: буквы в кружках → обычные, эмодзи прочь."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"\s+", " ", "".join(ch for ch in s if ch.isalnum() or ch in " _-/")).strip()


def _groups() -> list[str]:
    raw = os.environ.get("VIBER_GROUPS", DEFAULT_VIBER_GROUPS)
    return [_norm(g) for g in raw.split(",") if _norm(g)]


def split_sender(title: str, text: str) -> tuple[str, str | None, str]:
    """(группа, автор, сообщение) из заголовка и текста уведомления."""
    group, sender = title.strip(), None
    m = _SENDER_RE.match(text or "")
    if m and len(m.group(1).split()) <= 4:
        sender, text = m.group(1).strip(), m.group(2)
    for sep in (" @ ", " в ", " in "):  # «Андрей в Попутчики Приднестровье»
        if sep in group and sender is None:
            sender, group = (x.strip() for x in group.split(sep, 1))
            break
    return group, sender, (text or "").strip()


async def process(db: RidesDB, notifier: Notifier, title: str, text: str) -> dict:
    groups = _groups()
    haystack = _norm(title)  # только название группы, не текст сообщения
    if groups and not any(g in haystack for g in groups):
        return {"ok": True, "skipped": "not a rides group"}
    group, sender, body = split_sender(title, text)
    if len(body) < 6:
        return {"ok": True, "skipped": "empty"}
    p = parse_message(body)
    if not p.places or p.kind not in (PASSENGER, DRIVER):
        if p.kind in (PASSENGER, DRIVER):
            log.info("Похоже на заявку, но город не распознан: %s", body[:200].replace("\n", " "))
        return {"ok": True, "skipped": "not a ride"}
    if await db.seen_recently(p.fingerprint):
        return {"ok": True, "skipped": "duplicate"}
    post = post_from_group(p, chat=f"Viber · {group}"[:80], link=None, posted=datetime.now(TZ),
                           author_id=None, username=None, name=sender)
    saved = await db.save_post(post)
    if not saved:
        return {"ok": True, "skipped": "duplicate"}
    await db.log_request(f"Viber · {group}", p, False)
    sent = await notifier.dispatch(saved)
    log.info("Viber %s | %s → %s | отправлено %d", p.kind, p.from_place, p.to_place, sent)
    return {"ok": True, "kind": p.kind, "from": p.from_place, "to": p.to_place, "sent": sent}


_PLACEHOLDER_RE = re.compile(r"^%\w+(\(\))?$")  # переменная Tasker, которую не подставили


def _clean(v) -> str:
    s = str(v if v is not None else "").strip()
    return "" if _PLACEHOLDER_RE.match(s) else s


def conversation_messages(raw: str) -> tuple[str | None, list[str]]:
    """%anconversation из AutoNotification (JSON переписки Viber) → (название чата, ["Автор: текст", …])."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None, []
    title: str | None = None
    out: list[str] = []

    def name_of(x):
        if isinstance(x, dict):
            x = x.get("name") or x.get("title")
        return x.strip() if isinstance(x, str) and x.strip() else None

    def walk(o):
        nonlocal title
        if isinstance(o, dict):
            for k in ("conversationTitle", "conversation_title", "title"):
                if title is None and isinstance(o.get(k), str) and o[k].strip():
                    title = o[k].strip()
            txt = o.get("text") or o.get("message")
            if isinstance(txt, str) and txt.strip():
                sender = name_of(o.get("sender")) or name_of(o.get("person")) or name_of(o.get("senderName"))
                line = f"{sender}: {txt.strip()}" if sender else txt.strip()
                if line not in out:
                    out.append(line)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return title, out


def _is_summary(text: str) -> bool:
    low = text.strip().lower()
    return (low.startswith(("новые сообщения", "new messages", "mesaje noi"))
            or bool(re.fullmatch(r"\d+\s+(новых|новое|новые)?\s*сообщ\w*.*", low)))


def _split_messages(raw: str) -> list[str]:
    """Строки сводки Viber: каждое «Автор: текст» — отдельное сообщение; строки без автора
    приклеиваются к предыдущему (многострочные заявки)."""
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or _is_summary(line):
            continue
        if _SENDER_RE.match(line) or not out:
            out.append(line)
        else:
            out[-1] += "\n" + line
    return out


def make_app(db: RidesDB, notifier: Notifier) -> web.Application:
    key = os.environ.get("INGEST_KEY", "").strip()

    async def ingest(request: web.Request):
        if not key:
            return web.json_response({"ok": False, "error": "INGEST_KEY not set"}, status=503)
        given = request.query.get("key") or request.headers.get("X-Key", "")
        if not hmac.compare_digest(given, key):
            return web.json_response({"ok": False, "error": "bad key"}, status=403)
        data: dict = dict(request.query)
        if request.can_read_body:
            body = await request.text()
            if "form" in (request.content_type or ""):
                data.update(dict(await request.post()))
            elif body.strip():
                try:
                    obj = json.loads(body)
                except ValueError:
                    obj = None
                if isinstance(obj, dict) and any(k in obj for k in ("title", "text", "lines", "big")):
                    data.update(obj)
                else:
                    data.setdefault("conv", body)  # тело = %anconversation
        data = {k: _clean(v) for k, v in data.items()}
        title = data.get("title") or data.get("notification_title") or ""
        text = data.get("text") or data.get("notification") or data.get("message") or ""
        # Viber сворачивает сообщения группы в сводку «Новые сообщения в …». Сами сообщения —
        # в переписке (conv = %anconversation) или в строках текста (lines/big). Берём каждое.
        conv_title, conv_msgs = conversation_messages(data["conv"]) if data.get("conv") else (None, [])
        if conv_title and (not title or _is_summary(title)):
            title = conv_title
        extra = "\n".join(data.get(k) or "" for k in ("lines", "big", "bigtext", "text_lines", "ticker"))
        raw = {k: v[:700 if k == "conv" else 120] for k, v in data.items() if k != "key" and v}
        messages = conv_msgs or [m for m in _split_messages(extra) if m] or [text]
        results = []
        for body in messages:
            if _is_summary(body):
                result = {"ok": True, "skipped": "summary"}
            else:
                try:
                    result = await process(db, notifier, title, body)
                except Exception as e:  # noqa: BLE001
                    log.exception("ingest")
                    result = {"ok": False, "error": str(e)}
            RECENT.append((time.time(), title[:80], body[:160], result, raw))
            log.info("ingest | %s | %s | %s", title[:60], body[:80].replace("\n", " "), result)
            results.append(result)
        ok = all(r.get("ok") for r in results)
        return web.json_response(results[0] if len(results) == 1 else {"ok": ok, "items": results},
                                 status=200 if ok else 500)

    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/ingest", ingest)
    app.router.add_get("/ingest", ingest)   # некоторые приложения умеют только GET
    app.router.add_get("/", health)
    return app


async def run_server(db: RidesDB, notifier: Notifier):
    port = int(os.environ.get("PORT", "8080"))
    runner = web.AppRunner(make_app(db, notifier))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Приём заявок с телефона: порт %d, /ingest %s", port,
             "включён" if os.environ.get("INGEST_KEY") else "ВЫКЛЮЧЕН (нет INGEST_KEY)")
