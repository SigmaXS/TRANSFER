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


def _in_window(post: dict, f) -> bool:
    """Подходит ли время поездки под окно маршрута (например, 17:00–20:00). Без окна — всегда.
    Поездка без точного времени в окно не попадает только если день точно другой — такие тоже присылаем."""
    if not (f.time_from and f.time_to) or not post.get("has_time"):
        return True
    def mins(s):
        h, m = (int(x) for x in s.split(":"))
        return h * 60 + m
    t = datetime.fromtimestamp(post["trip_at"], TZ)
    n, a, b = t.hour * 60 + t.minute, mins(f.time_from), mins(f.time_to)
    return a <= n <= b if a <= b else (n >= a or n <= b)


def _in_quiet(window: tuple[str, str]) -> bool:
    """Сейчас внутри тихих часов? Окно может переходить через полночь: 23:00–07:00."""
    def mins(s):
        try:
            h, m = (int(x) for x in s.split(":"))
            return h * 60 + m
        except (ValueError, AttributeError):
            return None
    a, b = mins(window[0]), mins(window[1])
    if a is None or b is None or a == b:
        return False
    now = datetime.now(TZ)
    n = now.hour * 60 + now.minute
    return a <= n < b if a < b else (n >= a or n < b)


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
        quiet = {uid for uid, w in (await self.db.quiet_users()).items() if _in_quiet(w)}
        sent = 0
        for user, filters in await self.db.recipients(want=post["kind"]):
            if user.user_id == post.get("author_id"):
                continue
            hit = next((f for f in filters if matches(f, parsed) and _in_window(post, f)), None)
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
