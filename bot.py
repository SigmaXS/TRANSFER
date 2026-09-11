import asyncio
import logging
import os
import json
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# Токен твоего бота и твой Telegram ID (или ID администратора, куда слать уведомления)
TOKEN = "8951598738:AAFal8Yqbmh49Adc2nFTzHVBFMESo2rda6I"
ADMIN_CHAT_ID = "СЮДА_ВПИШИ_СВОЙ_TELEGRAM_ID" # Можно узнать у @userinfobot

# Подключение к базе данных PostgreSQL на Railway (Railway автоматически создает эту переменную)
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Функция инициализации базы данных (создает таблицу заказов, если её нет)
async def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL не найдена!")
        return
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

# Обработчик команды /start
async def cmd_start(message: types.Message):
    web_app_url = "https://transfer-production-f20b.up.railway.app"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚗 Открыть приложение", 
                    web_app=WebAppInfo(url=web_app_url)
                )
            ]
        ]
    )
    
    await message.answer(
        "👋 Добро пожаловать в TRANSFER MOLDOVA!\n\n"
        "Откройте приложение, чтобы построить маршрут и оформить заявку.",
        reply_markup=keyboard
    )

# Обработчик данных из Web App (когда клиент жмет «Оформить заявку»)
async def handle_web_app_data(message: types.Message):
    try:
        # Распаковываем JSON, который пришел из сайта
        data = json.loads(message.web_app_data.data)
        
        user_id = data.get('user_id')
        username = data.get('username')
        service = data.get('service')
        route = data.get('route')
        trip_date = data.get('date')
        comment = data.get('comment')

        # Сохраняем в PostgreSQL
        if DATABASE_URL:
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.execute(
                '''
                INSERT INTO orders (user_id, username, service, route, trip_date, comment)
                VALUES ($1, $2, $3, $4, $5, $6)
                ''',
                int(user_id) if str(user_id).isdigit() else 0,
                username, service, route, trip_date, comment
            )
            await conn.close()

        # Красивый ответ клиенту
        await message.answer(
            "✅ **Ваша заявка успешно принята!**\n\n"
            f"🛣 Маршрут: {route}\n"
            f"📅 Дата: {trip_date}\n"
            f"💬 Детали: {comment}\n\n"
            "Оператор свяжется с вами в ближайшее время.",
            parse_mode="Markdown"
        )

        # Отправляем уведомление тебе (админу) в личный чат
        if ADMIN_CHAT_ID and ADMIN_CHAT_ID != "СЮДА_ВПИШИ_СВОЙ_TELEGRAM_ID":
            admin_text = (
                "🚨 **Новый заказ трансфера!**\n\n"
                f"👤 Клиент: @{username} (ID: `{user_id}`)\n"
                f"🛠 Услуга: {service}\n"
                f"🛣 Маршрут: {route}\n"
                f"📅 Дата: {trip_date}\n"
                f"💬 Комментарий: {comment}"
            )
            await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="Markdown")

    except Exception as e:
        print(f"Ошибка при обработке заказа: {e}")
        await message.answer("⚠️ Произошла ошибка при сохранении заявки. Попробуйте еще раз.")

dp.message.register(cmd_start, Command("start"))
dp.message.register(handle_web_app_data)

# Веб-сервер для отдачи index.html
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
    await init_db() # Инициализируем БД при старте
    await asyncio.gather(
        web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
