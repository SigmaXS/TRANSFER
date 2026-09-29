"""Бот «Попутчики Молдова | ПМР | UA | EU»: заявки попутчиков из групп Telegram для водителей."""
import asyncio
import logging
import os

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from rides.db import RidesDB
from rides.handlers import router as rides_router
from rides.listener import make_client, start_listener
from rides.notifier import Notifier

# Всё берётся из переменных окружения (Railway → Variables). Токен в коде не храним.
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BOT_NAME = os.environ.get("BOT_NAME", "Попутчики Молдова | ПМР | UA | EU")
BOT_SHORT_DESCRIPTION = (
    "Заявки попутчиков для водителей: Молдова, ПМР, Украина, Европа. Не езди пустым 🚕")
BOT_DESCRIPTION = (
    "🚕 Бот для водителей: присылает заявки людей, которые ищут машину, из групп попутчиков "
    "в Telegram.\n\n"
    "🛣 Фильтр по маршруту (откуда → куда)\n"
    "🇲🇩 Молдова · 🔴 ПМР · 🇺🇦 Украина · 🇪🇺 Европа\n\n"
    "Нажмите «Старт», чтобы настроить фильтры.")

log = logging.getLogger("bot")


async def setup_profile(bot: Bot):
    """Имя, описание и меню команд бота. Меняем только если отличается (у Telegram жёсткий лимит)."""
    try:
        if (await bot.get_my_name()).name != BOT_NAME:
            await bot.set_my_name(BOT_NAME)
        if (await bot.get_my_short_description()).short_description != BOT_SHORT_DESCRIPTION:
            await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        if (await bot.get_my_description()).description != BOT_DESCRIPTION:
            await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_commands([BotCommand(command="start", description="Главное меню и фильтры")])
    except Exception as e:  # noqa: BLE001 — профиль не критичен для работы
        log.warning("Не удалось обновить профиль бота: %s", e)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN (Railway → Variables)")
    if not DATABASE_URL:
        raise SystemExit("Не задана переменная DATABASE_URL (Railway → Variables)")

    bot = Bot(token=TOKEN)
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    rides_db = RidesDB(pool)
    await rides_db.init()
    print("База данных успешно инициализирована!")

    dp = Dispatcher(storage=MemoryStorage(), rides_db=rides_db)
    dp.include_router(rides_router)
    await setup_profile(bot)

    tasks = [dp.start_polling(bot)]
    client = make_client()
    if client and await start_listener(client, rides_db, Notifier(bot, rides_db)):
        tasks.append(client.run_until_disconnected())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
