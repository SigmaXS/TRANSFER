"""Рассылка нового объявления тем, у кого подходит фильтр."""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .carrier import is_carrier
from .db import RidesDB
from .filters import matches
from .posts import WEBAPP_URL, format_post, post_to_parsed
from .timeparse import TZ

log = logging.getLogger("rides.notifier")


def notify_keyboard(post: dict, filter_id: int | None) -> InlineKeyboardMarkup:
    """📋 Номер (копирует) · 🔎 Открыть (Mini App на этой поездке) · 🔕 Не присылать этот маршрут."""
    from .api import _phone  # здесь, чтобы не было циклического импорта
    row = []
    phone = _phone(post)
    if phone:
        row.append(InlineKeyboardButton(text="📋 Номер", copy_text=CopyTextButton(text=phone)))
    elif post.get("author_username"):
        row.append(InlineKeyboardButton(text="✉️ Написать", url=f"https://t.me/{post['author_username']}"))
    if post.get("id"):
        row.append(InlineKeyboardButton(text="🔎 Открыть", web_app=WebAppInfo(url=f"{WEBAPP_URL}?p={post['id']}")))
    rows = [row] if row else []
    link = post.get("link") or ""
    if post.get("source") == "group" and link.startswith("https://t.me/"):
        rows.append([InlineKeyboardButton(text="💬 Открыть сообщение в группе", url=link)])
    else:
        from .posts import viber_group_link
        if viber := viber_group_link(post.get("chat")):
            rows.append([InlineKeyboardButton(text="💜 Группа в Viber", url=viber)])
    if filter_id:
        rows.append([InlineKeyboardButton(text="🔕 Не присылать этот маршрут", callback_data=f"r:fmute:{filter_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _quiet_now() -> bool:
    h = datetime.now(TZ).hour
    return h >= 23 or h < 7


class Notifier:
    def __init__(self, bot: Bot, db: RidesDB):
        self.bot = bot
        self.db = db

    async def dispatch(self, post: dict) -> int:
        """Заявку пассажира получают водители, свободную машину — пассажиры. Перевозчиков не рассылаем."""
        if is_carrier(post):
            return 0
        parsed = post_to_parsed(post)
        text = format_post(post)
        quiet = await self.db.quiet_users() if _quiet_now() else set()
        sent = 0
        for user, filters in await self.db.recipients(want=post["kind"]):
            if user.user_id == post.get("author_id"):
                continue
            hit = next((f for f in filters if matches(f, parsed)), None)
            if hit is None:
                continue
            kb = notify_keyboard(post, hit.id)
            for _attempt in range(2):
                try:
                    await self.bot.send_message(user.user_id, text, reply_markup=kb, parse_mode="HTML",
                                                disable_web_page_preview=True,
                                                disable_notification=user.user_id in quiet)
                    sent += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except TelegramForbiddenError:
                    await self.db.set_active(user.user_id, False)  # заблокировал бота
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("Не отправилось %s: %s", user.user_id, e)
                    break
            await asyncio.sleep(0.04)  # лимит Telegram ~30 сообщений/сек
        if sent:
            try:
                await self.db.log_event(None, "notify", {"n": sent, "kind": post["kind"]})
            except Exception:  # noqa: BLE001
                pass
        return sent
