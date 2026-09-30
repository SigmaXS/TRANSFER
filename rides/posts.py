"""Объявление = заявка пассажира или свободная машина: сборка, карточка, кнопки."""
import hashlib
import html
import time
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .parsing import DRIVER, PASSENGER, Parsed
from .timeparse import TZ, ago, trip_label, trip_window


def post_from_group(p: Parsed, *, chat: str, link: str | None, posted: datetime,
                    author_id: int | None, username: str | None, name: str | None) -> dict:
    trip_at, expires_at, has_time = trip_window(p.when, p.date, p.time, posted)
    day = posted.astimezone(TZ).strftime("%Y-%m-%d")
    return {
        "ts": posted.timestamp(), "source": "group", "chat": chat, "link": link,
        "kind": p.kind, "from_place": p.from_place, "to_place": p.to_place, "places": p.places,
        "trip_at": trip_at, "expires_at": expires_at, "has_time": has_time, "when_label": p.when,
        "people": p.people, "seats": p.seats, "is_ad": p.is_ad, "text": p.text[:1500],
        "author_id": author_id, "author_username": username, "author_name": name,
        # одна и та же заявка в тот же день (в т.ч. в разных группах) — один раз
        "fingerprint": f"g:{p.fingerprint}:{day}",
    }


def post_from_bot(*, kind: str, from_place: str, to_place: str, when: str | None,
                  date: str | None, time_str: str | None, count: int | None, comment: str | None,
                  user_id: int, username: str | None, name: str | None, phone: str | None) -> dict:
    now = datetime.now(TZ)
    trip_at, expires_at, has_time = trip_window(when, date, time_str, now)
    return {
        "ts": now.timestamp(), "source": "bot", "chat": None, "link": None,
        "kind": kind, "from_place": from_place, "to_place": to_place,
        "places": [x for x in (from_place, to_place) if x],
        "trip_at": trip_at, "expires_at": expires_at, "has_time": has_time, "when_label": when,
        "people": count if kind == PASSENGER else None,
        "seats": count if kind == DRIVER else None,
        "is_ad": False, "text": None, "comment": comment,
        "author_id": user_id, "author_username": username, "author_name": name, "phone": phone,
        "fingerprint": "b:" + hashlib.sha1(f"{user_id}:{time.time()}".encode()).hexdigest(),
    }


def post_to_parsed(post: dict) -> Parsed:
    """Для проверки фильтров."""
    return Parsed(text=post.get("text") or "", kind=post["kind"], from_place=post["from_place"],
                  to_place=post["to_place"], places=list(post.get("places") or []),
                  is_ad=bool(post.get("is_ad")))


def format_post(post: dict, show_age: bool = False) -> str:
    kind = post["kind"]
    if kind == PASSENGER:
        head = "🙋 <b>Ищут машину</b>"
    else:
        head = "🚗 <b>Свободная машина</b>" + (" · перевозчик" if post.get("is_ad") else "")
    frm, to = post.get("from_place"), post.get("to_place")
    if post.get("is_ad") and frm and not to:
        route = f"{frm} → разные направления"
    else:
        route = f"{frm or '?'} → {to or '?'}" if (frm or to) else ", ".join(post.get("places") or [])
    lines = [head, f"📍 <b>{html.escape(route)}</b>"]
    if post.get("source") == "site":
        lines.append(f"🕐 по звонку · объявление обновлено {ago(post['ts'])}")
    else:
        when = trip_label(post["trip_at"], post["has_time"], post.get("when_label"))
        lines.append(f"🕐 {when}" + (f" · написали {ago(post['ts'])}" if show_age else ""))
    if post.get("people"):
        lines.append(f"👤 {post['people']} чел.")
    if post.get("seats"):
        lines.append(f"💺 свободных мест: {post['seats']}")
    if post.get("text"):
        text = post["text"].strip()
        lines.append(f"\n<i>{html.escape(text[:500] + ('…' if len(text) > 500 else ''))}</i>")
    if post.get("comment"):
        lines.append(f"\n💬 {html.escape(post['comment'])}")

    if post.get("author_username"):
        who = f"@{html.escape(post['author_username'])}"
    elif post.get("author_id") and post.get("source") == "bot":
        who = f'<a href="tg://user?id={post["author_id"]}">{html.escape(post.get("author_name") or "автор")}</a>'
    else:
        who = html.escape(post.get("author_name") or "")
    if post.get("phone"):
        lines.append(f"📞 {html.escape(post['phone'])}")
    source = {"group": f"💬 {html.escape(post.get('chat') or '')}",
              "site": f"🌐 {html.escape(post.get('chat') or 'сайт')}"}.get(post.get("source"), "📱 Объявление в боте")
    lines.append(f"\n{source}" + (f" · {who}" if who else ""))
    return "\n".join(lines)


def post_keyboard(post: dict) -> InlineKeyboardMarkup | None:
    row = []
    if post.get("author_username"):
        row.append(InlineKeyboardButton(text="✉️ Написать", url=f"https://t.me/{post['author_username']}"))
    if post.get("link"):
        label = "🌐 Объявление на сайте" if post.get("source") == "site" else "🔗 В группе"
        row.append(InlineKeyboardButton(text=label, url=post["link"]))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None
