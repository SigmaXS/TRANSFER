"""Приём заявок с телефона: пересылка уведомлений Viber (MacroDroid и т.п.) на адрес бота.

POST https://<домен>/ingest?key=<INGEST_KEY>
Тело — JSON или форма с полями: title (заголовок уведомления), text (текст), app (необязательно).
Берём только уведомления, в заголовке или тексте которых есть одно из слов VIBER_GROUPS
(по умолчанию «попутчик») — личные переписки в бот не попадают.
"""
import hmac
import logging
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
        try:
            result = await process(db, notifier, title, text)
        except Exception as e:  # noqa: BLE001
            log.exception("ingest")
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response(result)

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
