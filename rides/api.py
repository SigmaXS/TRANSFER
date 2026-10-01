"""Открытое для приложения Taxi Radar API ленты: те же заявки, фильтры и «близкие места», что в боте.

GET /api/places?key=…                  → {"popular": [...], "all": [...]}
GET /api/rides?key=…&kind=passenger    → {"total": N, "items": [...]}
    kind  — passenger (ищут машину, для водителей) | driver (свободные машины)
    from  — город отправления (необязательно), to — куда: город, @pmr/@md/@ua/@eu или пусто
    both  — 1: в обе стороны;  offset, limit — постранично (limit ≤ 50)
Ключ: переменная APP_API_KEY (по умолчанию "taxiradar-app"). Только чтение.
"""
import hmac
import os
import re
import time

from aiohttp import web

from .db import RidesDB
from .filters import ROUTE, Filter, matches
from .parsing import DRIVER, PASSENGER
from .places import PLACES, POPULAR, resolve_place
from .posts import post_to_parsed, viber_group_link
from .timeparse import TZ, trip_label

_PHONE_RE = re.compile(r"(?:\+?373[\s-]?|\b0)\d{2}[\s-]?\d{2,3}[\s-]?\d{2,3}|\b77\d{5,6}\b|\b[56]\d{6}\b")


def _api_key() -> str:
    return os.environ.get("APP_API_KEY", "taxiradar-app").strip()


def _spec(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"any", "любой", "куда угодно"}:
        return None
    if raw.startswith("@"):
        return raw.lower()
    return resolve_place(raw) or raw


def _phone(post: dict) -> str | None:
    if post.get("phone"):
        return post["phone"]
    m = _PHONE_RE.search(post.get("text") or "")
    return re.sub(r"[\s-]", "", m.group(0)) if m else None


def _item(p: dict) -> dict:
    site = p.get("source") == "site"
    return {
        "id": p.get("id") or p.get("fingerprint"),
        "kind": p["kind"],
        "from": p.get("from_place"),
        "to": p.get("to_place"),
        "places": list(p.get("places") or []),
        "when": "по звонку" if site else trip_label(p["trip_at"], p["has_time"], p.get("when_label")),
        "trip_at": p.get("trip_at"),
        "posted_at": p.get("ts"),
        "people": p.get("people"),
        "seats": p.get("seats"),
        "text": (p.get("text") or "")[:1000],
        "comment": p.get("comment"),
        "phone": _phone(p),
        "telegram": p.get("author_username"),
        "author": p.get("author_name"),
        "source": p.get("chat") or ("Бот" if p.get("source") == "bot" else "сайт"),
        "link": p.get("link"),
        # Куда перейти, если нет телефона: сообщение в Telegram-группе, Viber-группа или сайт
        "group_link": p.get("link") or viber_group_link(p.get("chat")),
        "group_kind": ("site" if site else "telegram") if p.get("link")
        else ("viber" if viber_group_link(p.get("chat")) else None),
        "is_carrier": bool(p.get("is_ad")),
    }


def make_api(app: web.Application, db: RidesDB) -> None:
    def check(request: web.Request) -> None:
        given = request.query.get("key") or request.headers.get("X-Key", "")
        if not hmac.compare_digest(given, _api_key()):
            raise web.HTTPForbidden(text='{"error":"bad key"}', content_type="application/json")

    async def places(request: web.Request):
        check(request)
        return web.json_response({"popular": POPULAR, "all": sorted(PLACES)})

    async def rides(request: web.Request):
        check(request)
        q = request.query
        kind = DRIVER if q.get("kind") == "driver" else PASSENGER
        frm, to = _spec(q.get("from")), _spec(q.get("to"))
        f = Filter(ROUTE, frm, to, both_ways=q.get("both", "1") == "1") if (frm or to) else None
        posts = [p for p in await db.live_posts(kind) if f is None or matches(f, post_to_parsed(p))]
        try:
            offset = max(0, int(q.get("offset", 0)))
            limit = min(50, max(1, int(q.get("limit", 20))))
        except ValueError:
            offset, limit = 0, 20
        return web.json_response({
            "total": len(posts), "offset": offset, "from": frm, "to": to,
            "route": f.title().removeprefix("🛣 ") if f else "все направления",
            "now": time.time(), "tz": str(TZ),
            "items": [_item(p) for p in posts[offset:offset + limit]],
        })

    app.router.add_get("/api/places", places)
    app.router.add_get("/api/rides", rides)
