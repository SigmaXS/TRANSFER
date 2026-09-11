import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# Токен твоего бота
TOKEN = "8951598738:AAFal8Yqbmh49Adc2nFTzHVBFMESo2rda6I"

# Получаем порт от Railway (или ставим 8080 для локального запуска)
PORT = int(os.environ.get("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Обработчик команды /start
async def cmd_start(message: types.Message):
    # Railway выдаст публичный URL для твоего сервиса, 
    # здесь мы берем адрес из переменной окружения или подставляем локальный для теста
    web_app_url = os.environ.get("WEB_APP_URL", f"http://localhost:{PORT}/")
    
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

# Обработчик данных, которые прилетают из Web App при оформлении заказа
async def handle_web_app_data(message: types.Message):
    data = message.web_app_data.data
    await message.answer(f"✅ Заявка получена!\n\nДетали:\n{data}")

dp.message.register(cmd_start, Command("start"))
dp.message.register(handle_web_app_data)

# Функция для раздачи index.html через aiohttp
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
    # Запускаем и веб-сервер, и бота одновременно
    await asyncio.gather(
        web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())