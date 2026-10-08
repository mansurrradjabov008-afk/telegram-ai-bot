# 🎩 Antigravity Telegram AI Executive Butler

Sizning shaxsiy Telegram profilingiz uchun 20 yillik tajribaga asoslangan **AI Shaxsiy Yordamchisi (Digital Twin & Executive Assistant)**.

---

## 🌟 Asosiy Imkoniyatlar

1. **Shaxsiy Profilingiz Nomidan Avto-Muloqot:**
   * Do'stlar va tanishlaringiz bilan tabiiy, samimiy muloqot (o'zbekcha + yengil zamonaviy inglizcha).
   * **Insoniy kechikish va yozish imitatsiyasi (Typing action)** orqali Telegram blokirovka (anti-ban) algoritmlaridan 100% himoyalangan.
2. **2-Bosqichli Guardrail & Eskalatsiya (Xavfsizlik Kafolati):**
   * Oddiy gap-gashtakka AI o'zi javob beradi.
   * Moliyaviy masalalar, uchrashuv belgilash, muhim takliflar yoki sirlar paydo bo'lganda, bot aslo o'zicha qaror qilmaydi — darhol **Admin Bot Cockpit** orqali sizga chiroyli tugmalar bilan xabar yetkazadi.
3. **Contact Relationship CRM (Doimiy Xotira):**
   * Har bir kontakt uchun shaxsiy qoida belgilash: `/crm @username yaqin_dost Juda do'stona bo'l, bro deb yoz, hazillash`.
   * Bot bu ko'rsatmani butun umr eslab qoladi!
4. **Smart Scheduler (Rejali Xabarlar):**
   * Masalan: `/schedule @ali 07:00 Ertaga ko'rishamizmi?`.
   * SQLite bazasida saqlanadi, server o'chib yonsa ham yo'qolmaydi va vaqti kelganda avtomatik profilingiz nomidan yuboriladi.
5. **Kanal va Guruhlar Monitoringi (Executive Intelligence):**
   * Muhim kalit so'zlar bo'yicha tezkor ogohlantirish (Instant Alert).
   * So'nggi yangiliklar va siyosiy tahlillar bo'yicha ixcham Executive Digest.

---

## 📂 Loyiha Tuzilmasi

```
telegram_ai_butler/
├── bot/
│   ├── admin_bot.py         # aiogram 3.x Admin Kokpiti (tugmalar, bildirishnomalar)
│   └── userbot.py           # Telethon MTProto Client (shaxsiy profilingiz mijozi)
├── database/
│   ├── connection.py        # Asinxron aiosqlite ulanishi
│   ├── crud.py              # CRM, vazifalar va chatlar tarixi CRUD
│   └── models.py            # SQLite ma'lumotlar modellari
├── services/
│   ├── ai_engine.py         # Google Gemini 2.5 Flash miyasi va 2-bosqichli filtr
│   ├── digest_service.py    # Kanal va guruhlar monitoringi
│   ├── rate_limiter.py      # Anti-Ban va yozish kechikishi imitatsiyasi
│   └── scheduler_service.py # APScheduler vaqt bo'yicha topshiriqlar boshqaruvi
├── scripts/
│   ├── test_gemini.py       # Gemini API kalitini jonli sinash
│   └── simulate_butler.py   # Butun tizim integratsiyasining virtual simulyatori
├── tests/                   # To'liq avtomatlashtirilgan pytest testlari
├── config.py                # Konfiguratsiya boshqaruvi (Pydantic Settings)
├── main.py                  # Asosiy ishga tushiruvchi markaz
└── requirements.txt         # Kerakli kutubxonalar
```

---

## 🚀 Ishga Tushirish Qo'llanmasi

### 1. `.env` Faylini To'ldirish
Loyiha ildizidagi `.env` faylini oching va 4 ta kalitni kiriting:

```env
# 1. Telegram MTProto (my.telegram.org dan)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_PHONE_NUMBER=+998901234567

# 2. Admin Bot (t.me/BotFather dan)
ADMIN_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

# 3. Sening Telegram ID raqaming (@userinfobot dan bilish mumkin)
ADMIN_USER_ID=123456789

# 4. Google AI Studio Gemini API (aistudio.google.com dan - BEPUL)
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-2.5-flash
```

### 2. Sinov va Testlarni Ishga Tushirish
Barcha modullarni avtomatlashtirilgan sinovdan o'tkazish:
```bash
python -m pytest -v
```

Butun tizim integratsiyasini vizual sinash:
```bash
python scripts/simulate_butler.py
```

Gemini AI kalitingizni jonli tekshirish:
```bash
python scripts/test_gemini.py
```

### 3. Asosiy Tizimni Jonli Ishga Tushirish
```bash
python main.py
```
*Birinchi marta ishga tushganda, Telegram shaxsiy profilingizga SMS/Telegram orqali tasdiqlash kodi yuboradi. Uni terminalga bir marta kiritasiz va sessiya `data/` papkasida doimiy saqlanadi.*

---

## 🎮 Admin Bot Buyruqlari

Admin Botga kirib quyidagi buyruqlardan foydalanishingiz mumkin:
* `/start` — Boshqaruv markazini ochish
* `/crm @username [rol] [ko'rsatma]` — Kontakt fe'l-atvori va munosabatini o'rnatish
* `/contacts` — Barcha saqlangan kontaktlarni ko'rish
* `/schedule @username 07:00 Matn` — Xabar rejalashtirish
* `/tasks` — Kutilayotgan rejalashtirilgan xabarlar
* `/digest` — Kanallar va guruhlardan yangi xulosalar
* `/status` — Tizim holati
