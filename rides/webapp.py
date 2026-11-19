"""Mini App «Попутчики» в Telegram: страница /app и её API /app/api/*.

Вход — по подписи Telegram (initData в заголовке X-Init-Data), без паролей.
Перевозчики (см. carrier.py) в Mini App не показываются.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from .api import _item, _phone, _spec
from .carrier import is_carrier
from .db import DRIVER_ROLE, PASSENGER_ROLE, RidesDB
from .filters import ROUTE, Filter, matches, rank
from .parsing import DRIVER, PASSENGER
from .places import PLACES, POPULAR, RO_NAMES, resolve_place
from .posts import format_post, post_from_bot, post_to_parsed, viber_links
from .timeparse import TZ, ago

log_ = logging.getLogger("rides.webapp")

PAGE = Path(__file__).with_name("webapp") / "index.html"
GO_PAGE = Path(__file__).with_name("webapp") / "go.html"     # звонок / Viber во внешнем браузере
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BOT_USERNAME = os.environ.get("BOT_USERNAME", "MI_transferBot").lstrip("@")
CHANNEL = os.environ.get("RIDES_CHANNEL", "").strip()          # свой канал/группа: бот выкладывает туда заявки
TRIAL_DAYS = int(os.environ.get("RIDES_TRIAL_DAYS", "7"))
PAYMENT_TEXT = os.environ.get("RIDES_PAYMENT_TEXT", "Для продления доступа напишите администратору.")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_CHAT_ID", "1657186014").replace(" ", "").split(",") if x}
MAX_ACTIVE_POSTS = 5
MIN_RESULTS = 5

# Группы Telegram для кнопки «Вступить» (название → ссылка)
TG_GROUPS = [
    ("Попутчики Приднестровья", "https://t.me/poputchik_pmr"),
    ("Попутчики Приднестровье", "https://t.me/poputchiki_pmr"),
    ("Попутчики Молдова/ПМР/Украина", "https://t.me/poputchiki_md_pmr_ua"),
]
VIBER_NAMES = {"попутчики_md": "Попутчики_MD", "попутчики молдова/пмр/украина": "Попутчики Молдова/ПМР/Украина",
               "попутчики приднестровье": "Попутчики Приднестровье", "по пути на юг молдовы": "По пути на юг Молдовы"}


def check_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram WebApp. Возвращает user или None."""
    if not init_data or not TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    given = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not given or not hmac.compare_digest(calc, given):
        return None
    if time.time() - int(pairs.get("auth_date") or 0) > 7 * 86400:
        return None
    try:
        return json.loads(pairs.get("user") or "{}") or None
    except ValueError:
        return None


def _day_ok(p: dict, day: str) -> bool:
    if day in ("", "all"):
        return True
    trip = datetime.fromtimestamp(p["trip_at"], TZ).date()
    today = datetime.now(TZ).date()
    if day == "today":
        return trip <= today
    if day == "tomorrow":
        return trip == today + timedelta(days=1)
    try:
        return trip.isoformat() == day
    except ValueError:
        return True


def _tg_links(p: dict) -> dict:
    """Ссылка на само сообщение в Telegram и на группу (вступить).
    Открытая группа: t.me/имя/123 — сообщение открывается у всех.
    Закрытая: t.me/c/…/123 — только у участников, поэтому даём и «Вступить» (если знаем приглашение)."""
    link = p.get("link") or ""
    if p.get("source") != "group" or not link.startswith("https://t.me/"):
        return {"msg_link": None, "private": False, "join": None}
    if link.startswith("https://t.me/c/"):
        chat = (p.get("chat") or "").lower().replace("ё", "е")
        join = next((u for n, u in TG_GROUPS if n.lower().replace("ё", "е") in chat or chat in n.lower()), None)
        return {"msg_link": link, "private": True, "join": join}
    return {"msg_link": link, "private": False, "join": link.rsplit("/", 1)[0]}


def _card(p: dict, stale: bool = False) -> dict:
    it = _item(p)
    it.update(_tg_links(p))
    it.update({
        "id": p.get("id"),
        "ago": ago(p["ts"]) if p.get("ts") else "",
        "stale": stale,
        "has_time": bool(p.get("has_time")),
        "time": datetime.fromtimestamp(p["trip_at"], TZ).strftime("%H:%M") if p.get("has_time") else None,
        "source_label": p.get("chat") or ("Попутчики · бот" if p.get("source") == "bot" else "сайт"),
        "own": False,
    })
    return it


def _groups() -> dict:
    seen, viber = set(), []
    for name, url in viber_links():
        if name in seen:
            continue
        seen.add(name)
        viber.append({"name": VIBER_NAMES.get(name, name[:1].upper() + name[1:]), "url": url})
    return {"viber": viber, "telegram": [{"name": n, "url": u} for n, u in TG_GROUPS]}


ONLINE: dict[int, float] = {}          # кто сейчас в Mini App: user_id → когда последний раз был запрос
CLIENT_EVENTS = {"call", "viber", "write", "message", "group", "share_viber", "share_tg", "copy", "detail", "join",
                 "whatsapp", "tgphone"}


def make_webapp(app: web.Application, db: RidesDB, notifier) -> None:
    async def log(user_id, type_, data=None):
        try:
            await db.log_event(user_id, type_, data)
        except Exception as e:  # noqa: BLE001 — учёт не должен ломать приложение
            log_.warning("event %s: %s", type_, e)

    async def page(_request):
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def auth(request) -> dict:
        user = check_init_data(request.headers.get("X-Init-Data", ""))
        if not user or not user.get("id"):
            raise web.HTTPUnauthorized(text='{"error":"auth"}', content_type="application/json")
        await db.ensure_user(user["id"], user.get("username"), TRIAL_DAYS)
        ONLINE[user["id"]] = time.time()
        return user

    async def body(request) -> dict:
        try:
            return await request.json()
        except ValueError:
            return {}

    async def me(request):
        user = await auth(request)
        await log(user["id"], "open")
        row = await db.user_row(user["id"])
        u = await db.get_user(user["id"])
        filters = [{"id": r["id"], "kind": r["kind"], "from": r["from_place"], "to": r["to_place"],
                    "both": r["both_ways"], "want": r["want"], "enabled": r.get("enabled") is not False,
                    "title": Filter(r["kind"], r["from_place"], r["to_place"], r["both_ways"]).title()}
                   for r in await db.filter_rows(user["id"])]
        return web.json_response({
            "id": user["id"], "name": user.get("first_name") or "", "role": u.role,
            "has_access": u.has_access(), "access_until": u.access_until(), "active": u.active,
            "quiet": bool(row.get("quiet")), "quiet_from": row.get("quiet_from") or "23:00",
            "quiet_to": row.get("quiet_to") or "07:00",
            "profile": (json.loads(row["profile"]) if isinstance(row.get("profile"), str) else row.get("profile")) or {},
            "payment_text": PAYMENT_TEXT, "filters": filters,
            "groups": _groups(), "bot": BOT_USERNAME, "channel": CHANNEL or None,
            "is_admin": user["id"] in ADMIN_IDS,
        })

    async def places(_request):
        return web.json_response({"popular": POPULAR, "all": sorted(PLACES),
                                  "ro": {k: v for k, v in RO_NAMES.items() if k in PLACES}})

    async def set_role(request):
        user = await auth(request)
        role = (await body(request)).get("role")
        if role in (DRIVER_ROLE, PASSENGER_ROLE):
            await db.set_role(user["id"], role)
        return web.json_response({"ok": True})

    async def rides(request):
        user = await auth(request)
        q = request.query
        kind = PASSENGER if q.get("kind") == "passenger" else DRIVER
        frm, to = _spec(q.get("from")), _spec(q.get("to"))
        f = Filter(ROUTE, frm, to, both_ways=True) if (frm or to) else None
        day = q.get("day", "all")
        base = [p for p in await db.live_posts(kind, limit=500)
                if not is_carrier(p) and (f is None or matches(f, post_to_parsed(p)))]
        # Сколько поездок в каждый день — для чипов «Сегодня 12 · Завтра 31»
        today = datetime.now(TZ).date()
        counts = {"all": len(base)}
        for p in base:
            d = datetime.fromtimestamp(p["trip_at"], TZ).date()
            key = "today" if d <= today else "tomorrow" if d == today + timedelta(days=1) else d.isoformat()
            counts[key] = counts.get(key, 0) + 1
        posts = [p for p in base if _day_ok(p, day)]
        if f is not None:
            ranked = [(rank(f, post_to_parsed(p)), p) for p in posts]
            if f.from_place and f.to_place:
                ranked = [(r, p) for r, p in ranked if r not in (2, 3)]
            posts = [p for _, p in sorted(ranked, key=lambda x: x[0])]
        items = [_card(p) for p in posts]
        for it, p in zip(items, posts):
            it["own"] = p.get("author_id") == user["id"]
        # Мало актуальных по маршруту — добавляем тех, кто ездит здесь часто (прошедшие объявления)
        if f is not None and f.from_place and f.to_place and len(items) < MIN_RESULTS and day in ("all", "today"):
            seen = {it.get("phone") for it in items if it.get("phone")}
            for p in await db.recent_posts(kind):
                if is_carrier(p) or rank(f, post_to_parsed(p)) > 1:
                    continue
                ph = _phone(p)
                if not ph or ph in seen:
                    continue
                seen.add(ph)
                items.append(_card(p, stale=True))
                if len(items) >= MIN_RESULTS + 3:
                    break
        await log(user["id"], "search", {"mode": kind, "from": frm or "", "to": to or "", "day": day,
                                          "found": sum(1 for it in items if not it["stale"])})
        return web.json_response({"total": len(items), "items": items[:80], "from": frm, "to": to, "counts": counts})

    async def one(request):
        await auth(request)
        try:
            p = await db.get_post(int(request.query.get("id", "0")))
        except ValueError:
            p = None
        if not p:
            raise web.HTTPNotFound(text='{"error":"not found"}', content_type="application/json")
        return web.json_response({"item": _card(p, stale=p["expires_at"] < time.time())})

    async def add_filter(request):
        user = await auth(request)
        b = await body(request)
        frm, to = _spec(b.get("from")), _spec(b.get("to"))
        want = PASSENGER if b.get("want") == PASSENGER else DRIVER
        if not (frm or to):
            return web.json_response({"ok": False, "error": "Укажите откуда или куда"}, status=400)
        for r in await db.filter_rows(user["id"]):
            if r["want"] == want and r["from_place"] == frm and r["to_place"] == to:
                await db.set_filter_enabled(user["id"], r["id"], True)
                return web.json_response({"ok": True, "id": r["id"]})
        await db.add_filter(user["id"], Filter(ROUTE, frm, to, both_ways=bool(b.get("both", True))), want)
        await log(user["id"], "route_add", {"from": frm or "", "to": to or "", "want": want})
        return web.json_response({"ok": True})

    async def edit_filter(request):
        user = await auth(request)
        fid = int(request.match_info["id"])
        if request.method == "DELETE":
            await db.delete_filter(user["id"], fid)
        else:
            await db.set_filter_enabled(user["id"], fid, bool((await body(request)).get("enabled")))
        return web.json_response({"ok": True})

    async def settings(request):
        user = await auth(request)
        b = await body(request)
        if "active" in b:
            await db.set_active(user["id"], bool(b["active"]))
        if "quiet" in b:
            await db.set_quiet(user["id"], bool(b["quiet"]))
        hm = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")
        if hm.fullmatch(str(b.get("quiet_from", ""))) and hm.fullmatch(str(b.get("quiet_to", ""))):
            await db.set_quiet_hours(user["id"], b["quiet_from"], b["quiet_to"])
        return web.json_response({"ok": True})

    async def publish(request):
        user = await auth(request)
        b = await body(request)
        kind = DRIVER if b.get("kind") == DRIVER else PASSENGER
        frm = resolve_place(b.get("from") or "") or (b.get("from") or "").strip()
        to = resolve_place(b.get("to") or "") or (b.get("to") or "").strip()
        if not frm or not to:
            return web.json_response({"ok": False, "error": "Укажите откуда и куда"}, status=400)
        if len(await db.my_posts(user["id"])) >= MAX_ACTIVE_POSTS:
            return web.json_response({"ok": False, "error": f"Не больше {MAX_ACTIVE_POSTS} активных объявлений"},
                                     status=400)
        day = b.get("day") or "today"
        when, date_str = {"today": ("сегодня", None), "tomorrow": ("завтра", None)}.get(day, (None, None))
        if when is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            date_str = f"{int(day[8:10])}.{int(day[5:7])}"
        t = (b.get("time") or "").strip()
        time_str = t if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t) else None
        try:
            count = max(1, min(8, int(b.get("count") or 1)))
        except ValueError:
            count = 1
        phone = re.sub(r"[^\d+ ]", "", b.get("phone") or "").strip()[:20] or None
        comment = (b.get("comment") or "").strip()[:300] or None
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
        post = await db.save_post(post_from_bot(
            kind=kind, from_place=frm, to_place=to, when=when, date=date_str, time_str=time_str, count=count,
            comment=comment, user_id=user["id"], username=user.get("username"), name=name, phone=phone))
        pretty = (b.get("text") or "").strip()[:1500]
        if pretty:
            await db.pool.execute("UPDATE ride_posts SET text=$2 WHERE id=$1", post["id"], pretty)
            post["text"] = pretty
        sent = await notifier.dispatch(post)
        await log(user["id"], "publish", {"kind": kind, "from": frm, "to": to, "sent": sent})
        if CHANNEL:
            try:
                await notifier.bot.send_message(CHANNEL, format_post(post), parse_mode="HTML",
                                                disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001
                log_.warning("Канал %s: %s", CHANNEL, e)
        # Автор сам получит встречные объявления по этому маршруту
        want = DRIVER if kind == PASSENGER else PASSENGER
        if not any(r["want"] == want and r["from_place"] == frm and r["to_place"] == to
                   for r in await db.filter_rows(user["id"])):
            await db.add_filter(user["id"], Filter(ROUTE, frm, to, both_ways=False), want)
        return web.json_response({"ok": True, "sent": sent, "item": _card(post)})

    async def event(request):
        """События из страницы: позвонил, написал, открыл сообщение, отправил в группы…"""
        user = await auth(request)
        b = await body(request)
        if b.get("type") in CLIENT_EVENTS:
            data = {k: str(v)[:80] for k, v in (b.get("data") or {}).items()}
            await log(user["id"], b["type"], data)
        return web.json_response({"ok": True})

    async def admin(request):
        user = await auth(request)
        if user["id"] not in ADMIN_IDS:
            raise web.HTTPForbidden(text='{"error":"admin only"}', content_type="application/json")
        now = time.time()
        day_start = datetime.combine(datetime.now(TZ).date(), datetime.min.time(), TZ).timestamp()
        stats = await db.admin_stats(day_start)
        stats["online"] = sum(1 for t in ONLINE.values() if now - t < 180)
        stats["registered"] = [{"id": r["user_id"], "username": r["username"], "role": r["role"],
                                "at": r["profile_at"] or r["created_at"], "posts": r["posts"], **(r["profile"] or {})}
                               for r in await db.registered()]
        return web.json_response(stats)

    async def reset(request):
        """«Сбросить всё»: удалить все маршруты уведомлений, включить уведомления, выключить тихие часы."""
        user = await auth(request)
        for r in await db.filter_rows(user["id"]):
            await db.delete_filter(user["id"], r["id"])
        await db.set_active(user["id"], True)
        await db.set_quiet(user["id"], False)
        return web.json_response({"ok": True})

    async def profile(request):
        """Данные для готовых заявок: подставляются в «Разместить»."""
        user = await auth(request)
        b = await body(request)
        txt = lambda k, n=60: str(b.get(k) or "").strip()[:n]  # noqa: E731
        prof = {"name": txt("name", 40), "phone": re.sub(r"[^\d+ ]", "", txt("phone", 20)).strip(),
                "viber": bool(b.get("viber")), "whatsapp": bool(b.get("whatsapp")), "telegram": bool(b.get("telegram")),
                "car": txt("car"), "pickup": txt("pickup"), "parcels": bool(b.get("parcels"))}
        await db.set_profile(user["id"], prof)
        await log(user["id"], "profile", {"phone": bool(prof["phone"])})
        return web.json_response({"ok": True, "profile": prof})

    async def my(request):
        user = await auth(request)
        return web.json_response({"items": [_card(p) for p in await db.my_posts(user["id"])]})

    async def close(request):
        user = await auth(request)
        await db.close_post(user["id"], int(request.match_info["id"]))
        return web.json_response({"ok": True})

    async def report(request):
        user = await auth(request)
        b = await body(request)
        p = await db.get_post(int(b.get("id") or 0))
        if p:
            for admin in ADMIN_IDS:
                try:
                    await notifier.bot.send_message(
                        admin, f"⚠️ Жалоба из Mini App (от {user['id']}): {html.escape(b.get('reason') or '')}\n\n"
                               + format_post(p), parse_mode="HTML", disable_web_page_preview=True)
                except Exception:  # noqa: BLE001
                    pass
        return web.json_response({"ok": True})

    async def go(_request):
        return web.FileResponse(GO_PAGE, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/app/go", go)
    app.router.add_get("/app", page)
    app.router.add_get("/app/", page)
    app.router.add_get("/app/api/me", me)
    app.router.add_get("/app/api/places", places)
    app.router.add_post("/app/api/role", set_role)
    app.router.add_get("/app/api/rides", rides)
    app.router.add_get("/app/api/post", one)
    app.router.add_post("/app/api/filters", add_filter)
    app.router.add_route("POST", "/app/api/filters/{id:\\d+}", edit_filter)
    app.router.add_route("DELETE", "/app/api/filters/{id:\\d+}", edit_filter)
    app.router.add_post("/app/api/settings", settings)
    app.router.add_post("/app/api/publish", publish)
    app.router.add_get("/app/api/my", my)
    app.router.add_post("/app/api/profile", profile)
    app.router.add_post("/app/api/reset", reset)
    app.router.add_post("/app/api/ev", event)
    app.router.add_get("/app/api/admin", admin)
    app.router.add_route("DELETE", "/app/api/my/{id:\\d+}", close)
    app.router.add_post("/app/api/report", report)
