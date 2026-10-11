# 👑 Antigravity AI Butler — Butun Umr (24/7/365) Mustaqil Ishlatish Bo'yicha Bosh Qo'llanma

Ushbu hujjat sizning shaxsiy Telegram AI Butleringizni **shaxsiy kompyuteringiz o'chirilgan taqdirda ham, hech qanday yordamchisiz va kompyutersiz butun umr mustaqil, xavfsiz va to'xtovsiz** ishlashini ta'minlash uchun tayyorlandi.

---

## 1. Nega Shaxsiy Kompyuterda "Umrbod" Ishlab Bo'lmaydi?
Hozirgi vaqtda botingiz kompyuteringizda fonda (`run_silently.vbs` va `start_butler.bat` orqali) avtomatik ishlab turibdi. 
Biroq:
* Noutbuk qopqog'i yopilsa yoki kompyuter o'chsa — elektr ta'minoti to'xtaydi va bot ham to'xtaydi.
* Wi-Fi yoki internet uzilsa — Telegram bilan aloqa yo'qoladi.

**Yechim:** Botni internetda **24 soat, 365 kun uzluksiz yonib turadigan Bulutli Server (Cloud VPS)** ga joylashtirish.

---

## 2. Cloud VPS Nima va Uni Qayerdan Olish Mumkin?
VPS (Virtual Private Server) — bu bulutdagi 24/7 ishlaydigan virtual kompyuter.
Uni oyiga atigi **$1 – $3** evaziga olish mumkin:
1. **Timeweb Cloud** (timeweb.cloud) — Uzcard/Humo orqali ham to'lash mumkin, oyiga ~20-30 ming so'm. Ubuntu 22.04 yoki 24.04 tanlanadi.
2. **Hetzner Cloud** (hetzner.com) — Germaniya/Finlyandiya serverlari (~€3-4/oy).
3. **DigitalOcean / Linode / Vultr** — oyiga $4-$5.
4. **Oracle Cloud Free Tier** — mutlaqo bepul (Always Free).

*(Kerakli parametrlar eng minimal bo'lsa yetarli: 1 CPU, 1 GB yoki 2 GB RAM, 20 GB Disk, OS: Ubuntu 22.04 LTS).*

---

## 3. Loyihaning Tayyor Arxiv Paketi (Turnkey Production Bundle)
Siz uchun loyihaning barcha eng so'nggi ma'lumotlari:
* Barcha dasturiy kodlar va xizmatlar
* Eng so'nggi `butler.db` bazasi (barcha kontaktlar, shaxsiy uslublar, xotira faktlari)
* Faol Telegram avtorizatsiya sessiyasi (`butler_user_session.session`) — *serverda qaytadan SMS kod so'ralmaydi!*
* `.env` maxfiy kalitlari va sozlamalari
* 1-bosishda o'rnatuvchi `setup_vps.sh` skripti

bitta tayyor arxivga jamlandi:
📁 **Manzil:** `C:\Users\BRand\.gemini\antigravity\scratch\telegram_ai_butler\telegram_ai_butler_production.zip` (hajmi atigi 650 KB).

---

## 4. Serverda 1 Daqiqada Ishga Tushirish (3 Qadam)

Serveringizga (masalan, PuTTY, Termius yoki oddiy PowerShell SSH orqali) ulangach:

### 1-qadam: Arxivni serverga yuklash
PowerShell orqali (kompyuteringizdan):
```powershell
scp "C:\Users\BRand\.gemini\antigravity\scratch\telegram_ai_butler\telegram_ai_butler_production.zip" root@SERVER_IP:/root/
```
*(Yoki Termius / FileZilla orqali faylni serverga tashlaysiz).*

### 2-qadam: Serverda arxivni ochish
Server konsolida:
```bash
sudo apt-get install -y unzip
unzip telegram_ai_butler_production.zip -d /opt/telegram_ai_butler
cd /opt/telegram_ai_butler
```

### 3-qadam: 1 ta buyruq bilan avtomatik o'rnatish
```bash
chmod +x setup_vps.sh
bash setup_vps.sh
```

**BO'LDI! Skript barcha ishlarni o'zi bajaradi:**
* Kerakli kutubxonalarni o'rnatadi
* Python muhitini tayyorlaydi
* Linux `systemd` xizmatini (`butler.service`) yaratadi
* **`Restart=always`** parametrini yoqadi: agar server o'chib yonsa yoki texnik nosozlik bo'lsa, bot 5 soniyada avtomatik qayta tiriladi!

---

## 5. Boshqaruv Buyruqlari (Serverda)
Bot holatini tekshirish:
```bash
sudo systemctl status butler
```
Jonli xabarlar va loglarni ko'rish:
```bash
sudo journalctl -u butler -f
```
Botni to'xtatish (agar kerak bo'lsa):
```bash
sudo systemctl stop butler
```
Botni qayta yuklash:
```bash
sudo systemctl restart butler
```

---

## 6. Natija
Endi siz:
1. Shaxsiy kompyuteringizni butunlay o'chirib qo'yishingiz mumkin.
2. AI yordamchisiz (mensiz) butun umr mustaqil xizmat qiladi.
3. Telegram botingiz 24/7/365 rejimida sizning nomingizdan aqlli muloqot qiladi, dayjest tayyorlaydi va barcha rejalashtirilgan vazifalarni bajaradi!
