"""Приём заявок с телефона: пересылка уведомлений Viber (MacroDroid и т.п.) на адрес бота.

POST https://<домен>/ingest?key=<INGEST_KEY>
Тело — JSON или форма с полями: title (заголовок уведомления), text (текст), app (необязательно).
Берём только уведомления, в заголовке или тексте которых есть одно из слов VIBER_GROUPS
(по умолчанию «попутчик») — личные переписки в бот не попадают.
"""
import collections
import hmac
import logging
import time
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


def _groups() -> list[str]:
    raw = os.environ.get("VIBER_GROUPS", "попутчик")
    return [g.strip().lower() for g in raw.split(",") if g.strip()]


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
    haystack = f"{title}\n{text}".lower()
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
            try:
                if "json" in (request.content_type or ""):
                    data.update(await request.json())
                else:
                    data.update(dict(await request.post()))
            except Exception:  # noqa: BLE001 — тело не JSON и не форма
                data.setdefault("text", await request.text())
        title = str(data.get("title") or data.get("notification_title") or "")
        text = str(data.get("text") or data.get("notification") or data.get("message") or "")
        # Viber сворачивает сообщения группы в сводку «Новые сообщения в …» — сами сообщения
        # приходят в «строках текста» (lines) или «развёрнутом тексте» (big). Берём каждое.
        extra = "\n".join(str(data.get(k) or "") for k in ("lines", "big", "bigtext", "text_lines", "ticker"))
        raw = {k: str(v)[:120] for k, v in data.items() if k != "key" and str(v).strip()}
        messages = [m for m in _split_messages(extra) if m] or [text]
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
