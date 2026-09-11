import asyncio
import logging
import os
import json
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# Токен твоего бота и твой числовой Telegram ID для уведомлений
TOKEN = "8951598738:AAFal8Yqbmh49Adc2nFTzHVBFMESo2rda6I"
ADMIN_CHAT_ID = 1657186014

DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Функция инициализации базы данных
async def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL не найдена!")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''
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
        await conn.close()
        print("База данных успешно инициализирована!")
    except Exception as e:
        print(f"Ошибка инициализации БД: {e}")

# Обработчик команды /start
async def cmd_start(message: types.Message):
    web_app_url = "https://transfer-production-f20b.up.railway.app"
    
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
            ]
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
        if DATABASE_URL:
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.execute(
                '''
                INSERT INTO orders (user_id, username, service, route, trip_date, comment)
                VALUES ($1, $2, $3, $4, $5, $6)
                ''',
                int(user_id),
                client_display, service, route, trip_date, comment
            )
            await conn.close()

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
            admin_text = (
                "🚨 **Новый заказ трансфера!**\n\n"
                f"👤 Клиент: {client_display}\n"
                f"🛠 Авто/Услуга: {service}\n"
                f"🛣 Маршрут: {route}\n"
                f"📅 Дата: {trip_date}\n"
                f"💬 Комментарий: {comment}"
            )
            await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="Markdown", reply_markup=inline_keyboard)

    except Exception as e:
        print(f"Ошибка при обработке заказа: {e}")

dp.message.register(cmd_start, Command("start"))
dp.message.register(handle_web_app_data)

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
    # Здесь был пропущен await, из-за чего бот не запускался:
    await asyncio.gather(
        web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
