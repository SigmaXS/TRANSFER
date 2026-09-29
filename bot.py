import asyncio
import html
import logging
import os
import json
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

from rides.db import RidesDB
from rides.handlers import router as rides_router
from rides.listener import make_client, start_listener
from rides.notifier import Notifier

# Токен и настройки берутся из переменных окружения (Railway → Variables).
# Никогда не храните токен в коде: репозиторий публичный.
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("Не задана переменная BOT_TOKEN (Railway → Variables)")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "1657186014").split(",")[0])
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://transfer-production-f20b.up.railway.app")

DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool: asyncpg.Pool | None = None

# Функция инициализации базы данных
async def init_db():
    global pool
    if not DATABASE_URL:
        print("DATABASE_URL не найдена!")
        return
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await pool.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                service TEXT,
                route TEXT,
                trip_date TEXT,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("База данных успешно инициализирована!")
    except Exception as e:
        print(f"Ошибка инициализации БД: {e}")

# Обработчик команды /start
async def cmd_start(message: types.Message):
    web_app_url = WEB_APP_URL

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🚗 Заказать трансфер", 
                    web_app=WebAppInfo(url=web_app_url)
                )
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚗 Открыть приложение", 
                    web_app=WebAppInfo(url=web_app_url)
                )
            ],
            [InlineKeyboardButton(text="🚕 Я водитель — заявки попутчиков", callback_data="r:open")],
        ]
    )
    
    welcome_text = (
        "<b>Что умеет этот бот?</b>\n\n"
        "🟡 <b>Добро пожаловать в Transfer</b> — ваш надёжный личный трансфер!\n\n"
        "🚗 Заберём вас из любой точки и доставим туда, куда нужно: по <b>Молдове</b> 🇲🇩, в <b>Украину</b> 🇺🇦 или <b>Румынию</b> 🇷🇴.\n\n"
        "Выберите маршрут, посмотрите доступные автомобили и цену — всё прямо в приложении."
    )
    
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=inline_kb)
    await message.answer("Воспользуйтесь кнопкой меню внизу для быстрого доступа:", reply_markup=keyboard)

# Обработчик данных из Web App
async def handle_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        
        service = data.get('service')
        route = data.get('route')
        trip_date = data.get('date')
        comment = data.get('comment')

        user_id = message.from_user.id
        username = message.from_user.username
        
        if not username:
            client_display = f"ID: {user_id}"
        else:
            client_display = f"@{username}"

        # Сохраняем заказ в PostgreSQL
        if pool:
            await pool.execute(
                '''
                INSERT INTO orders (user_id, username, service, route, trip_date, comment)
                VALUES ($1, $2, $3, $4, $5, $6)
                ''',
                int(user_id),
                client_display, service, route, trip_date, comment
            )

        inline_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Написать клиенту", 
                        url=f"tg://user?id={user_id}"
                    )
                ]
            ]
        )

        if ADMIN_CHAT_ID:
            e = lambda v: html.escape(str(v)) if v is not None else "—"
            admin_text = (
                "🚨 <b>Новый заказ трансфера!</b>\n\n"
                f"👤 Клиент: {e(client_display)}\n"
                f"🛠 Авто/Услуга: {e(service)}\n"
                f"🛣 Маршрут: {e(route)}\n"
                f"📅 Дата: {e(trip_date)}\n"
                f"💬 Комментарий: {e(comment)}"
            )
            await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML", reply_markup=inline_keyboard)

    except Exception as e:
        print(f"Ошибка при обработке заказа: {e}")

dp.message.register(cmd_start, Command("start"))
dp.message.register(handle_web_app_data, F.web_app_data)

async def handle_index(request):
    return web.FileResponse('index.html')

async def web_server():
    app = web.Application()
    app.router.add_get('/', handle_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"Веб-сервер запущен на порту {PORT}")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    tasks = [web_server(), dp.start_polling(bot)]

    # Раздел для водителей: заявки попутчиков из групп Telegram
    if pool:
        rides_db = RidesDB(pool)
        await rides_db.init()
        dp["rides_db"] = rides_db
        dp.include_router(rides_router)
        client = make_client()
        if client and await start_listener(client, rides_db, Notifier(bot, rides_db)):
            tasks.append(client.run_until_disconnected())
    else:
        print("Без DATABASE_URL раздел для водителей выключен")

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
