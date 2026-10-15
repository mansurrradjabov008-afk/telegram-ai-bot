"""
One-time interactive session generator for Termux / Android Phone.
Prompts for Telegram verification code and saves a fresh data/butler_user_session.session.
"""
import os
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import asyncio
from telethon import TelegramClient
from config import settings


async def main():
    session_name = settings.TELEGRAM_SESSION_NAME
    session_file = os.path.join("data", session_name)
    if not session_file.endswith(".session"):
        session_file_with_ext = session_file + ".session"
    else:
        session_file_with_ext = session_file

    print("=" * 60)
    print(f"📱 TELEGRAM USERBOT SAKSIYASI YANGILANMOQDA")
    print(f"Raqam: {settings.TELEGRAM_PHONE_NUMBER}")
    print("=" * 60)

    # Clean old revoked session files
    for p in [session_file, session_file_with_ext, session_file_with_ext + "-journal"]:
        if os.path.exists(p):
            try:
                os.remove(p)
                print(f"🧹 Eski yaroqsiz sessiya fayli o'chirildi: {p}")
            except Exception as e:
                print(f"⚠️ {p} faylini o'chirishda ogohlantirish: {e}")

    client = TelegramClient(session_file, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    print("📲 Telegramga tasdiqlash kodi yuborilmoqda...")
    await client.start(phone=settings.TELEGRAM_PHONE_NUMBER)

    me = await client.get_me()
    print("=" * 60)
    print(f"✅ MUVAFFAQIYATLI ULINDI: {me.first_name} (@{me.username}) [ID: {me.id}]")
    print("🎉 Yangi sessiya muvaffaqiyatli saqlandi!")
    print("=" * 60)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
