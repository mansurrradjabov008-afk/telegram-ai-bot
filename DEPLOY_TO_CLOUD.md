# ☁️ Botni Butun Umr 24/7/365 Ishlatish Bo'yicha Qo'llanma

Bu qo'llanma orqali siz botingizni shaxsiy kompyuteringiz o'chirilgan holatda ham **butun umr to'xtovsiz (24/7/365)** ishlashini ta'minlay olasiz.

---

## 1-Bosqich: Hozir Kompyuteringizda Nima Qilindi? (Avtomatik Bajarildi)
Sizning shaxsiy kompyuteringiz uchun:
1. **Windows Auto-Start** yoqildi (`shell:startup` papkasiga ulandi).
2. Endi siz Antigravity'ni yopsangiz ham, kompyuterni o'chirib-yoqsangiz ham:
   * Hech qanday qora konsol oynasi chiqmasdan, bot fonda (backgroundda) jimgina avtomatik ishga tushadi.
   * Agar biron sabab bilan to'xtab qolsa, **Watchdog** uni 5 soniyada avtomatik qayta tiriltiradi (Auto-restart).

---

## 2-Bosqich: Kompyuterni Butunlay O'chirganda Ham 24/7 Ishlashini Ta'minlash (Cloud VPS)

Agar noutbuk qopqog'i yopilsa yoki kompyuteringiz o'chsa ham botingiz to'xtamasligi uchun eng professional yo'l — uni **Cloud Server (VPS)** ga joylashdir.

### Eng Oson va Arzon Usul (Ubuntu VPS - oyiga $2-$3):
(Hetzner, Timeweb, DigitalOcean yoki boshqa istalgan VPS):

1. **Fayllarni serverga nusxalash:**
   Loyiha papkasidagi barcha fayllarni (ayniqsa `.env` va `data/` ichidagi `.session` faylini) serveringizning `/opt/telegram_ai_butler` papkasiga yuklang.

2. **Docker orqali 1 ta buyruq bilan ishga tushirish:**
   ```bash
   docker compose up -d --build
   ```
   *Bo'ldi! `restart: always` tufayli server o'chib yonsa ham bot avtomatik ishlab turadi.*

3. **Yoki Linux Systemd xizmati orqali (Dockersiz):**
   ```bash
   cp butler.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable butler
   systemctl start butler
   ```

Tekshirish:
```bash
systemctl status butler
```

Endi kompyuteringiz yoniq bo'ladimi, o'chiq bo'ladimi — botingiz butun dunyo bo'ylab 24/7/365 sadoqat bilan sizga xizmat qiladi!
