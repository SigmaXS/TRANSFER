"""Бот «Попутчики Молдова | ПМР | UA | EU»: водители находят пассажиров, пассажиры — машины."""
import asyncio
import logging
import os

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonCommands

from rides.db import RidesDB
from rides.handlers import ADMIN_IDS, router as rides_router
from rides.listener import Watcher, make_client
from rides.notifier import Notifier
from rides.web import poll_sites

# Всё берётся из переменных окружения (Railway → Variables). Токен в коде не храним.
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BOT_NAME = os.environ.get("BOT_NAME", "Попутчики Молдова | ПМР | UA | EU")
BOT_SHORT_DESCRIPTION = (
    "Попутки и свободные машины: Молдова, ПМР, Украина, Европа. Водителям — пассажиры, пассажирам — машины 🚕")
BOT_DESCRIPTION = (
    "🚕 Попутчики · Молдова · ПМР · UA · EU\n\n"
    "🚗 Водителям — заявки людей, которые ищут машину, из групп попутчиков и из бота. "
    "Не езди пустым.\n"
    "🙋 Пассажирам — свободные машины по вашему маршруту и своя заявка «хочу поехать». Бесплатно.\n\n"
    "🛣 Маршрут откуда → куда · 🇲🇩 Молдова · 🔴 ПМР · 🇺🇦 Украина · 🇪🇺 Европа\n\n"
    "Нажмите «Старт».")

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
        admin_cmds = [BotCommand(command="start", description="Главное меню"),
                      BotCommand(command="sources", description="Группы и сайты, которые читает бот"),
                      BotCommand(command="findgroups", description="Найти группы попутчиков"),
                      BotCommand(command="addsource", description="Добавить группу: /addsource @group"),
                      BotCommand(command="stats", description="Статистика заявок"),
                      BotCommand(command="grant", description="Продлить доступ: /grant id дней")]
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:  # noqa: BLE001 — админ ещё не писал боту
                pass
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())  # вместо старой кнопки приложения
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

    notifier = Notifier(bot, rides_db)
    watcher = None
    client = make_client()
    if client:
        watcher = Watcher(client, rides_db, notifier)
        if not await watcher.start():
            watcher = None

    dp = Dispatcher(storage=MemoryStorage(), rides_db=rides_db, notifier=notifier, watcher=watcher)
    dp.include_router(rides_router)
    await setup_profile(bot)

    tasks = [dp.start_polling(bot), poll_sites(rides_db)]
    if watcher:
        tasks.append(client.run_until_disconnected())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
