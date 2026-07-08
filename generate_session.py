# -*- coding: utf-8 -*-
"""
شغّل هذا الملف مرة واحدة فقط على جهازك (أو عبر Termux / Replit / Colab)
لتوليد Session String الخاص بحسابك الشخصي.
سيطلب منك رقم الهاتف، ثم كود التحقق الذي يصلك عبر تيليجرام.

بعد ظهور الـ Session String، انسخه وضعه في GitHub Secrets باسم TG_SESSION.
لا تشارك هذا الـ String مع أي أحد أبداً — فهو يعطي وصولاً كاملاً لحسابك.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("أدخل API_ID الخاص بك (من my.telegram.org): "))
API_HASH = input("أدخل API_HASH الخاص بك: ").strip()

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n=== انسخ هذا الـ Session String واحفظه كـ Secret باسم TG_SESSION ===\n")
    print(client.session.save())
    print("\n=== لا تشارك هذا النص مع أي أحد ===")
