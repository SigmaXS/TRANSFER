"""Рассылка нового объявления тем, у кого подходит фильтр."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .db import RidesDB
from .filters import matches
from .posts import format_post, post_keyboard, post_to_parsed

log = logging.getLogger("rides.notifier")


class Notifier:
    def __init__(self, bot: Bot, db: RidesDB):
        self.bot = bot
        self.db = db

    async def dispatch(self, post: dict) -> int:
        """Заявку пассажира получают водители, свободную машину — пассажиры."""
        parsed = post_to_parsed(post)
        text, kb = format_post(post), post_keyboard(post)
        sent = 0
        for user, filters in await self.db.recipients(want=post["kind"]):
            if user.user_id == post.get("author_id"):
                continue
            if not any(matches(f, parsed) for f in filters):
                continue
            for _attempt in range(2):
                try:
                    await self.bot.send_message(user.user_id, text, reply_markup=kb, parse_mode="HTML",
                                                disable_web_page_preview=True)
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
        return sent
