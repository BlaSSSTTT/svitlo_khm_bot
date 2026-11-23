"""ПОТРІБНО ЛИШЕ ДЛЯ ПРИВАТНОГО КАНАЛУ.
Запусти один раз на своєму комп'ютері:  pip install telethon  і  python make_session.py
Він попросить номер телефону й код, а потім виведе довгий рядок — це TG_SESSION.
Цей рядок = доступ до твого акаунта. Вставляй його ТІЛЬКИ в GitHub Secrets.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("api_id (з my.telegram.org): "))
api_hash = input("api_hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nTG_SESSION (скопіюй повністю):\n")
    print(c.session.save())
