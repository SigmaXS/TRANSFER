"""Получить TG_SESSION для аккаунта-читателя. Запускается ОДИН раз на своём компьютере:

    pip install telethon
    python make_session.py

API_ID и API_HASH: https://my.telegram.org → API development tools (войти номером аккаунта-читателя).
Полученную строку вставьте в Railway → Variables → TG_SESSION. Никому её не показывайте:
это полный доступ к аккаунту.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nГотово! Скопируйте всю строку ниже в переменную TG_SESSION:\n")
    print(client.session.save())
