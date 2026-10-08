"""
Interactive script to test Google Gemini API key with real prompt scenarios.
Run: python scripts/test_gemini.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Ensure root in sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

# Configure UTF-8 for Windows terminal
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import settings
from services.ai_engine import AIEngine
from database.models import Contact


async def run_live_test():
    print("=" * 60)
    print("🧪 ANTIGRAVITY GEMINI 2.5 FLASH AI ENGINE TEST")
    print("=" * 60)

    api_key = settings.GEMINI_API_KEY
    if not api_key or api_key.startswith("AIzaSyYour"):
        print("ℹ️ Eslatma: .env faylida GEMINI_API_KEY hali o'rnatilmagan (Heuristic offline filtr sinovdan o'tmoqda).")

    engine = AIEngine(api_key=api_key)

    # Test contact profile
    friend = Contact(
        user_id=999,
        username="bestie",
        full_name="Akmal",
        relationship_type="yaqin_dost",
        custom_instructions="Juda samimiy, 'do'stim' yoki 'bro' deb yoz, hazil aralash gapir.",
    )

    scenarios = [
        ("Salom qalaysan bro, nima gaplar?", "Ali (Oddiy salom-alik)"),
        ("Ertaga soat 14:00 da 1000$ qarzni bera olasanmi?", "Vali (Moliyaviy so'rov)"),
        ("Bizning yangi loyiha bo'yicha uchrashib gaplashishimiz kerak, qachon bo'shsan?", "Hamkor (Uchrashuv taklifi)"),
    ]

    for incoming_text, sender_label in scenarios:
        print(f"\n📩 Xabar ({sender_label}): '{incoming_text}'")
        res = await engine.classify_incoming_message(incoming_text, sender_name=friend.full_name, contact_profile=friend)
        print(f"  👉 Toifa: [{res.category}] (Ishonch: {res.confidence:.2f})")
        print(f"  👉 Sabab: {res.reason}")
        if res.category == "SAFE_CHITCHAT":
            reply = await engine.generate_reply(incoming_text, sender_name=friend.full_name, contact_profile=friend)
            print(f"  🤖 AI Butler sening nomingdan tayyorlagan javob:\n     \"{reply}\"")
        else:
            print(f"  🚨 ESKALATSIYA: Xo'jayinga yuboriladigan tavsiya javob: \"{res.suggested_reply}\"")

    print("\n" + "=" * 60)
    print("✅ Sinov muvaffaqiyatli yakunlandi!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_live_test())
