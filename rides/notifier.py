"""Рассылка заявки водителям, у которых подходит фильтр."""
import asyncio
import html
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import RidesDB
from .filters import matches
from .parsing import DRIVER, PASSENGER, Parsed

log = logging.getLogger("notifier")


@dataclass
class Item:
    parsed: Parsed
    chat_title: str
    link: str | None
    sender_username: str | None
    sender_name: str | None


def format_item(item: Item) -> str:
    p = item.parsed
    if p.kind == PASSENGER:
        head = "🙋 <b>Ищут машину</b>"
    elif p.kind == DRIVER:
        head = "🚗 <b>Водитель, есть места</b>" + (" · реклама" if p.is_ad else "")
    else:
        head = "❔ <b>Сообщение с маршрутом</b>"
    route = f"{p.from_place or '?'} → {p.to_place or '?'}" if p.has_route else ", ".join(p.places)
    lines = [head, f"📍 {html.escape(route)}"]
    when = " ".join(x for x in (p.when, p.date, p.time) if x)
    if when:
        lines.append(f"🕐 {when}")
    if p.people:
        lines.append(f"👤 {p.people} чел.")
    text = p.text.strip()
    if len(text) > 500:
        text = text[:500] + "…"
    lines.append(f"\n<i>{html.escape(text)}</i>")
    who = f"@{item.sender_username}" if item.sender_username else (item.sender_name or "")
    lines.append(f"\n💬 {html.escape(item.chat_title)}" + (f" · {html.escape(who)}" if who else ""))
    return "\n".join(lines)


def item_keyboard(item: Item) -> InlineKeyboardMarkup | None:
    row = []
    if item.sender_username:
        row.append(InlineKeyboardButton(text="✉️ Написать", url=f"https://t.me/{item.sender_username}"))
    if item.link:
        row.append(InlineKeyboardButton(text="🔗 В группе", url=item.link))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


class Notifier:
    def __init__(self, bot: Bot, db: RidesDB):
        self.bot = bot
        self.db = db

    def wanted(self, mode: str, p: Parsed) -> bool:
        if p.kind == PASSENGER:
            return True
        return mode == "all"  # водители с местами / прочее — только в режиме «всё»

    async def dispatch(self, item: Item) -> int:
        text = format_item(item)
        kb = item_keyboard(item)
        sent = 0
        for user, filters in await self.db.recipients():
            if not self.wanted(user.mode, item.parsed):
                continue
            if not any(matches(f, item.parsed) for f in filters):
                continue
            try:
                await self.bot.send_message(user.user_id, text, reply_markup=kb, parse_mode="HTML",
                                            disable_web_page_preview=True)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await self.db.set_active(user.user_id, False)  # пользователь заблокировал бота
            except Exception as e:  # noqa: BLE001
                log.warning("Не отправилось %s: %s", user.user_id, e)
            await asyncio.sleep(0.04)  # лимиты Telegram ~30 сообщений/сек
        return sent
