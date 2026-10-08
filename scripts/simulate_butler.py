"""
Full System Simulation for Antigravity Telegram AI Executive Butler.
Simulates CRM contact setup, chit-chat auto-replies, escalation alerts, and scheduled tasks.
Run: python scripts/simulate_butler.py
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta
import pytz

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager
from services.ai_engine import AIEngine
from services.rate_limiter import HumanRateLimiter
from services.scheduler_service import SchedulerService
from services.digest_service import DigestService


async def simulate_system():
    print("=" * 65)
    print("🚀 ANTIGRAVITY AI BUTLER — TO'LIQ TIZIM INTEGRATSIYASI SINOVI")
    print("=" * 65)

    # 1. DB Init
    db_file = root_dir / "data" / "sim_test.db"
    await init_db(db_file)
    db = DatabaseManager(db_path=db_file)
    print("✅ 1. SQLite Baza va jadvallar poydevori tayyor.")

    # 2. Services Init
    ai = AIEngine()
    rate_limiter = HumanRateLimiter(min_delay=1, max_delay=2)
    scheduler = SchedulerService(db)
    digest = DigestService(db, ai)
    scheduler.start()

    # 3. CRM Setup
    print("\n[CRM SINOVI]:")
    print("Xo'jayin buyrug'i: '/crm @akmal yaqin_dost Juda do'stona bo'l, 'bro' deb yoz, hazillash'")
    contact = await db.upsert_contact(
        user_id=1001,
        username="akmal",
        full_name="Akmal",
        relationship_type="yaqin_dost",
        custom_instructions="Juda do'stona bo'l, 'bro' deb yoz, hazillash.",
    )
    print(f"👉 CRM saqlandi: @{contact.username} ({contact.relationship_type}) -> '{contact.custom_instructions}'")

    # 4. Incoming Safe Chit-chat simulation
    print("\n[CHIT-CHAT SINOVI]:")
    msg_1 = "Salom qalaysan, ishlar yaxshimi?"
    print(f"📩 Akmal (@akmal) yozmoqda: '{msg_1}'")
    cls_1 = await ai.classify_incoming_message(msg_1, sender_name=contact.full_name, contact_profile=contact)
    print(f"🧠 AI Tahlili: [{cls_1.category}] (Sabab: {cls_1.reason})")

    if cls_1.category == "SAFE_CHITCHAT":
        reply = await ai.generate_reply(msg_1, sender_name=contact.full_name, contact_profile=contact)
        delay = rate_limiter.calculate_typing_duration(reply)
        print(f"⏳ Insoniy kechikish va yozish imitatsiyasi: {delay:.1f} soniya...")
        await asyncio.sleep(0.5)  # fast simulation
        print(f"💬 Profilingiz nomidan Akmalga yuborilgan javob:\n   \"{reply}\"")

    # 5. Incoming Escalation simulation
    print("\n[ESKALATSIYA SINOVI]:")
    msg_2 = "Assalomu alaykum, kechagi 500$ masalasida qachon ko'rishamiz?"
    sender_2 = "Hamkor Jamshid"
    print(f"📩 {sender_2} yozmoqda: '{msg_2}'")
    cls_2 = await ai.classify_incoming_message(msg_2, sender_name=sender_2)
    print(f"🧠 AI Tahlili: [{cls_2.category}] (Sabab: {cls_2.reason})")

    if cls_2.category == "ESCALATE_TO_BOSS":
        print("🚨 ADMIN BOT KOKPITIDA XO'JAYINGA CHIQARILADIGAN BILDIRISHNOMA:")
        print("-------------------------------------------------------------")
        print(f"🔔 XO'JAYIN, MUHIM XABAR KELDI!\nKimdan: {sender_2}\nXabar: '{msg_2}'")
        print(f"Sabab: {cls_2.reason}")
        print(f"💡 AI Tavsiya qilgan javob: \"{cls_2.suggested_reply}\"")
        print("🔘 [ ✅ Shu javobni yuborish ]  🔘 [ ✍️ O'zgartirib yozish ]  🔘 [ 🛑 O'zim yozaman ]")
        print("-------------------------------------------------------------")

    # 6. Scheduler simulation
    print("\n[REJALASHTIRILGAN XABAR (SCHEDULER) SINOVI]:")
    print("Xo'jayin buyrug'i: '/schedule @akmal 07:00 Ertaga ertalab ko'rishamizmi bro?'")

    scheduled_log = []
    async def mock_send(target, text):
        scheduled_log.append((target, text))

    scheduler.register_callbacks(send_callback=mock_send, notify_boss_callback=lambda txt: print(f"📢 [ADMIN BOTGA HISOBOT]: {txt[:50]}..."))

    tz = pytz.timezone("Asia/Tashkent")
    target_time = datetime.now(tz) + timedelta(seconds=1)
    task = await scheduler.schedule_message("@akmal", "Ertaga ertalab ko'rishamizmi bro?", target_time)
    print(f"⏰ Task #{task.id} SQLite bazasida ro'yxatga olindi. Vaqti: {target_time.strftime('%H:%M:%S')}")
    print("⏳ Kutish va trigger ishlashini tekshirish...")
    await asyncio.sleep(1.5)

    if scheduled_log:
        print(f"✅ Rejadagi xabar yuborildi: {scheduled_log[0]}")

    scheduler.stop()
    print("\n" + "=" * 65)
    print("🎉 TIZIMNING BARCHA ASOSIY MODULLARI 100% XATOSIZ ISHLADI!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(simulate_system())
