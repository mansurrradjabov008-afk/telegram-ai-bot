"""
Admin Cockpit Bot implementation using aiogram 3.x.
Provides an interactive command center and inline keyboard escalation alerts for the Boss.
Bulletproof HTML escaping and fail-safe error handling.
"""

import html
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any
import pytz

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from config import settings
from database.crud import DatabaseManager
from services.ai_engine import AIEngine
from services.scheduler_service import SchedulerService
from services.digest_service import DigestService

logger = logging.getLogger(__name__)


def markdown_to_telegram_html(text: str) -> str:
    """Safely converts standard AI markdown output into valid Telegram HTML."""
    if not text:
        return ""
    # 1. Escape HTML entities
    safe = html.escape(text)
    # 2. Bold: **text**
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    # 3. Inline code: `text`
    safe = re.sub(r"`(.+?)`", r"<code>\1</code>", safe)
    # 4. Italic: *text* (single asterisk, not adjacent to other asterisks)
    safe = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", safe)
    # 5. Clean up bullet lists: convert "* " or "- " at line start to "• "
    safe = re.sub(r"(?m)^(?:\*|-)\s+", "• ", safe)
    return safe


def format_task_time_human(iso_str: str, tz) -> str:
    """Formats raw ISO time string into clean, human-readable Uzbek label with relative delta."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        now = datetime.now(tz)
        diff = dt - now

        months = [
            "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
            "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"
        ]
        month_name = months[dt.month - 1]

        time_part = dt.strftime("%H:%M")
        date_part = f"{dt.day:02d}-{month_name}"

        if dt.date() == now.date():
            day_label = "Bugun"
        elif dt.date() == (now + timedelta(days=1)).date():
            day_label = "Ertaga"
        elif dt.date() == (now + timedelta(days=2)).date():
            day_label = "Indinga"
        else:
            day_label = date_part

        seconds = int(diff.total_seconds())
        if seconds < -60:
            rel = "vaqti yetgan"
        elif seconds < 3600:
            mins = max(1, seconds // 60)
            rel = f"{mins} daqiqadan so'ng"
        elif seconds < 86400:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            rel = f"{hours} soat {mins} daqiqadan so'ng" if mins > 0 else f"{hours} soatdan so'ng"
        else:
            days = seconds // 86400
            rel = f"{days} kundan so'ng"

        return f"{day_label} {time_part} ({rel})"
    except Exception:
        return iso_str


async def safe_reply(message: types.Message, text: str, reply_markup=None, parse_mode: Optional[str] = "HTML"):
    """Sends a reply safely; if formatting fails, automatically falls back to plain text."""
    try:
        await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        logger.warning(f"Telegram parse error on reply ({e}), sending plain text...")
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", "")
        await message.reply(clean_text, reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        logger.error(f"Unexpected error in safe_reply: {e}")


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None, parse_mode: Optional[str] = "HTML"):
    """Sends a message safely; if formatting fails, automatically falls back to plain text."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        logger.warning(f"Telegram parse error on send ({e}), sending plain text...")
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", "")
        await bot.send_message(chat_id=chat_id, text=clean_text, reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        logger.error(f"Unexpected error in safe_send: {e}")


async def safe_reply_or_send_long(
    message: types.Message,
    text: str,
    reply_markup=None,
):
    """Sends text splitting nicely across 4000 character chunks if needed."""
    if len(text) <= 4000:
        return await safe_reply(message, text, reply_markup=reply_markup, parse_mode="HTML")

    lines = text.split("\n")
    chunks = []
    curr = ""
    for line in lines:
        if len(curr) + len(line) + 1 > 3800:
            chunks.append(curr)
            curr = line + "\n"
        else:
            curr += line + "\n"
    if curr:
        chunks.append(curr)

    for i, c in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        if i == 0:
            await safe_reply(message, c.strip(), reply_markup=markup, parse_mode="HTML")
        else:
            await message.answer(c.strip(), reply_markup=markup, parse_mode="HTML")


class ReplyState(StatesGroup):
    waiting_for_custom_reply = State()


class AdminBotService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        ai_engine: AIEngine,
        scheduler_service: SchedulerService,
        digest_service: Optional[DigestService] = None,
        token: Optional[str] = None,
        admin_id: Optional[int] = None,
    ):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self.scheduler_service = scheduler_service
        self.digest_service = digest_service
        self.token = token or settings.ADMIN_BOT_TOKEN
        self.admin_id = admin_id or settings.ADMIN_USER_ID

        self.bot = Bot(token=self.token) if self.token and not self.token.startswith("1234567890:") else None
        self.dp = Dispatcher(storage=MemoryStorage())
        self._userbot_send_fn: Optional[Callable] = None
        self._pending_escalations: Dict[str, Dict[str, Any]] = {}

        self._setup_handlers()

    def set_userbot_sender(self, fn: Callable):
        self._userbot_send_fn = fn

    def _is_admin(self, user_id: int) -> bool:
        return self.admin_id == 0 or user_id == self.admin_id

    def _setup_handlers(self):
        dp = self.dp

        @dp.message(CommandStart())
        async def cmd_start(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return await safe_reply(message, "⛔ Ruxsat berilmagan.")

            text = (
                "👑 <b>Salom, Xo'jayin! Antigravity AI Butler Kokpiti tayyor.</b>\n\n"
                "Men sizning shaxsiy profilingizni to'liq boshqaruvchi markazman.\n\n"
                "📌 <b>Asosiy buyruqlar:</b>\n"
                "• <code>/crm @username [rol] [ko'rsatma]</code> — Kontakt munosabatini va doimiy uslubini belgilash\n"
                "• <code>/contacts</code> — Barcha eslab qolingan kontaktlarni ko'rish\n"
                "• <code>/schedule @username HH:MM Matn</code> — Belgilangan vaqtga xabar rejalashtirish\n"
                "• <code>/tasks</code> — Kutilayotgan rejalashtirilgan xabarlar\n"
                "• <code>/digest</code> — Kanallar va guruhlardan eng so'nggi xulosalar\n"
                "• <code>/status</code> — Tizim holati va sozlamalar"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="📰 Executive Digest", callback_data="digest:open")
            builder.button(text="📋 Kontaktlar", callback_data="menu:contacts")
            builder.button(text="⏰ Rejadagi vazifalar", callback_data="menu:tasks")
            builder.button(text="⚡ Tizim holati", callback_data="menu:status")
            builder.adjust(2, 2)
            await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("status"))
        async def cmd_status(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            auto_reply_str = "Yoqilgan" if settings.AUTO_REPLY_ENABLED else "O'chirilgan"
            phone_str = html.escape(settings.TELEGRAM_PHONE_NUMBER or "Faol")
            model_str = html.escape(settings.GEMINI_MODEL)
            tz_str = html.escape(settings.TIMEZONE)

            status_text = (
                "⚡ <b>ANTIGRAVITY BUTLER TIZIM HOLATI:</b>\n\n"
                f"• <b>AI Modeli:</b> <code>{model_str}</code>\n"
                f"• <b>Avto-javob holati:</b> <code>{auto_reply_str}</code>\n"
                f"• <b>Vaqt mintaqasi:</b> <code>{tz_str}</code>\n"
                f"• <b>Telegram Profil:</b> <code>{phone_str}</code>\n"
                "• <b>Kontaktlar filtri:</b> 🛡 Faol (Faqat /contacts dagi insonlar)\n"
                "• <b>Holat:</b> Barcha xizmatlar 100% faol."
            )
            await safe_reply(message, status_text, parse_mode="HTML")

        async def _execute_digest(target_msg: types.Message, is_callback: bool = False, force_recent: bool = False):
            if not self.digest_service:
                return await safe_reply(target_msg, "❌ <b>Digest xizmati ulanmagan.</b>", parse_mode="HTML")

            loading_text = "⏳ <i>Kanallar va guruhlardagi so'nggi xabarlar jamlanmoqda va AI tahlil qilmoqda...</i>"
            if is_callback:
                try:
                    await target_msg.edit_text(loading_text, parse_mode="HTML")
                    active_msg = target_msg
                except Exception:
                    active_msg = await target_msg.answer(loading_text, parse_mode="HTML")
            else:
                active_msg = await target_msg.reply(loading_text, parse_mode="HTML")

            try:
                summary, count = await self.digest_service.generate_digest(force_recent=force_recent)
                if not summary or count == 0:
                    empty_text = (
                        "ℹ️ <b>Hozircha yangi tahlil qilinadigan xabarlar yo'q!</b>\n\n"
                        "Telegram akkauntingiz a'zo bo'lgan kanallar va guruhlardan yangi xabarlar kelishi bilan avtomatik jamlanadi va tahlil qilinadi."
                    )
                    builder = InlineKeyboardBuilder()
                    builder.button(text="📊 Statistika", callback_data="digest:stats")
                    builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                    builder.adjust(2)
                    if is_callback:
                        await active_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    else:
                        await safe_reply(active_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    return

                formatted = markdown_to_telegram_html(summary)
                builder = InlineKeyboardBuilder()
                builder.button(text="🔄 Yangilash", callback_data="digest:refresh")
                builder.button(text="📊 Statistika", callback_data="digest:stats")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(2, 1)

                full_report = (
                    f"📰 <b>KUNLIK EXECUTIVE DIGEST</b> (<i>{count} ta post tahlili</i>)\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{formatted}"
                )

                if is_callback and len(full_report) <= 4000:
                    try:
                        await active_msg.edit_text(full_report, reply_markup=builder.as_markup(), parse_mode="HTML")
                        return
                    except Exception:
                        pass

                await safe_reply_or_send_long(active_msg, full_report, reply_markup=builder.as_markup())

            except Exception as e:
                logger.error(f"Digest generation failed: {e}", exc_info=True)
                err_text = f"❌ <b>Digest tayyorlashda xatolik yuz berdi:</b>\n<code>{html.escape(str(e))}</code>"
                if is_callback:
                    await active_msg.edit_text(err_text, parse_mode="HTML")
                else:
                    await safe_reply(active_msg, err_text, parse_mode="HTML")

        @dp.message(Command("digest"))
        async def cmd_digest(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _execute_digest(message, is_callback=False)

        async def _apply_contact_instruction(
            msg_obj: types.Message,
            target_user: str,
            instruction_text: str,
            forced_style: Optional[str] = None,
        ):
            target = target_user.replace("@", "").strip()
            instruction = instruction_text.strip()

            # 1. Determine addressing style ('siz' vs 'sen')
            lower_inst = instruction.lower()
            if forced_style in ["siz", "sen"]:
                addressing = forced_style
            elif any(w in lower_inst for w in ["senla", "sensira", "sensirab", "sen deb", "sen ", "do'st", "dost", "bro", "jigar", "tengdosh"]):
                addressing = "sen"
            elif any(w in lower_inst for w in ["sizla", "sizlab", "siz deb", "siz ", "hurmat", "aka", "opa", "ustoz", "mijoz", "rasmiy"]):
                addressing = "siz"
            else:
                addressing = "siz"

            # 2. Relationship categorization
            if any(w in lower_inst for w in ["hazil", "kulgi", "do'st", "dost", "bro", "jigar"]):
                role = "hazilkash_dost"
                role_label = "Hazilkash va do'stona"
            elif any(w in lower_inst for w in ["xush", "muomala", "hurmat", "muloyim", "ajoyib", "shirin"]):
                role = "xushmuomala_ajoyib"
                role_label = "Xushmuomala va hurmatli"
            elif any(w in lower_inst for w in ["rasmiy", "jiddiy", "ish"]):
                role = "rasmiy_hamkor"
                role_label = "Rasmiy va jiddiy"
            else:
                role = "maxsus_uslub"
                role_label = "Shaxsiy ko'rsatma"

            existing = await self.db_manager.get_contact_by_username(target)
            user_id = existing.user_id if existing else abs(hash(target)) % 1000000000

            await self.db_manager.upsert_contact(
                user_id=user_id,
                username=target,
                full_name=existing.full_name if existing else target,
                relationship_type=role,
                addressing_style=addressing,
                custom_instructions=instruction,
            )

            safe_target = html.escape(target)
            safe_instruction = html.escape(instruction)
            safe_role_label = html.escape(role_label)

            if addressing == "sen":
                style_badge = "🟢 <b>SEN (Sensirash / Yaqin do'st)</b>"
                style_exp = f"Bot <code>@{safe_target}</code> ga faqat <i>'sen', 'qilyapsan', 'jigar'</i> deb erkin va do'stona gaplashadi!"
                switch_to = "siz"
                switch_label = "🔄 'SIZ' ga o'zgartirish"
            else:
                style_badge = "🔵 <b>SIZ (Sizlash / Hurmat bilan)</b>"
                style_exp = f"Bot <code>@{safe_target}</code> ga faqat <i>'siz', 'qilyapsiz'</i> deb yuksak hurmat va odob bilan gaplashadi!"
                switch_to = "sen"
                switch_label = "🔄 'SEN' ga o'zgartirish"

            builder = InlineKeyboardBuilder()
            builder.button(text=switch_label, callback_data=f"crm_style:{target}:{switch_to}")
            builder.button(text="📋 Barcha kontaktlar", callback_data="crm:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(1, 2)

            await safe_reply(
                msg_obj,
                f"✨ <b>CRM PROFILI SOZLANDI, XO'JAYIN!</b>\n\n"
                f"👤 <b>Kontakt:</b> <code>@{safe_target}</code>\n"
                f"🗣 <b>Murojaat shakli:</b> {style_badge}\n"
                f"🎭 <b>Munosabat:</b> <code>{safe_role_label}</code>\n"
                f"📜 <b>Ko'rsatma:</b> <i>\"{safe_instruction}\"</i>\n\n"
                f"🧠 <b>Butler xotirasiga muhrlandi!</b>\n"
                f"{style_exp}",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )

        @dp.message(Command("crm"))
        async def cmd_crm(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            text_after = message.text[len("/crm"):].strip()
            if not text_after:
                return await safe_reply(
                    message,
                    "ℹ️ <b>CRM Boshqaruvi — Muloqot uslubi va SIZ / SEN sozlamasi:</b>\n\n"
                    "📌 <b>Buyruq formati:</b>\n"
                    "• <code>/crm @username siz [ko'rsatma]</code> — SIZLASH (hurmat bilan)\n"
                    "• <code>/crm @username sen [ko'rsatma]</code> — SENSIRASH (yaqin do'st)\n\n"
                    "💡 <b>Misollar:</b>\n"
                    "• <code>/crm @davron_aka siz doim hurmat bilan, muloyim gaplash</code>\n"
                    "• <code>/crm @umid_namozov sen yaqin do'stim hazilkash bo'l</code>\n\n"
                    "🗣 <b>Yoki oddiy yozsangiz ham bo'ladi:</b>\n"
                    "• <code>@umid_namozov ni senla, u mening yaqin do'stim</code>\n"
                    "• <code>@davron_aka ni sizla, hurmatli inson</code>",
                    parse_mode="HTML",
                )

            parts = text_after.split(maxsplit=2)
            target = parts[0]
            if not (target.startswith("@") or target.isdigit()):
                parsed = await self.ai_engine.parse_crm_intent(text_after)
                if parsed and parsed.get("is_crm") and parsed.get("username"):
                    return await _apply_contact_instruction(
                        message,
                        parsed["username"],
                        parsed.get("custom_instructions", text_after),
                        forced_style=parsed.get("addressing_style"),
                    )

            if len(parts) >= 2:
                second = parts[1].lower()
                if second in ["siz", "sen"]:
                    forced_style = second
                    instruction = parts[2] if len(parts) > 2 else f"Doim {second} deb gaplashish"
                else:
                    forced_style = None
                    instruction = " ".join(parts[1:])
                await _apply_contact_instruction(message, target, instruction, forced_style=forced_style)
            else:
                await _apply_contact_instruction(message, target, "Samimiy, do'stona muloqot")

        async def _schedule_via_ai(msg_obj: types.Message, raw_text: str) -> bool:
            """Uses AI to parse natural language scheduling request and registers task."""
            tz = pytz.timezone(settings.TIMEZONE)
            now = datetime.now(tz)

            parsed = await self.ai_engine.parse_schedule_intent(raw_text, now, settings.TIMEZONE)
            if not parsed or not parsed.get("is_schedule"):
                return False

            target_peer = parsed["target_peer"]
            scheduled_iso = parsed["scheduled_iso"]
            msg_text = parsed["message_text"]

            try:
                target_dt = datetime.fromisoformat(scheduled_iso)
                if target_dt.tzinfo is None:
                    target_dt = tz.localize(target_dt)

                task = await self.scheduler_service.schedule_message(
                    target_peer=target_peer,
                    message_text=msg_text,
                    run_at=target_dt,
                )

                safe_target = html.escape(target_peer)
                safe_msg = html.escape(msg_text)
                formatted_time = format_task_time_human(scheduled_iso, tz)

                builder = InlineKeyboardBuilder()
                builder.button(text=f"🚀 Hozir yuborish #{task.id}", callback_data=f"task_send:{task.id}")
                builder.button(text=f"❌ Bekor qilish #{task.id}", callback_data=f"task_cancel:{task.id}")
                builder.button(text="⏳ Barcha vazifalar", callback_data="tasks:refresh")
                builder.adjust(2, 1)

                await safe_reply(
                    msg_obj,
                    f"⏰ <b>AI VAZIFANI REJALASHTIRDI! (ID #{task.id})</b>\n\n"
                    f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                    f"🕒 <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                    f"💬 <b>Xabar matni:</b>\n<i>\"{safe_msg}\"</i>\n\n"
                    f"🧠 <i>Belgilangan vaqtda bot profilingiz nomidan xabarni avtomatik yuboradi.</i>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )
                return True
            except Exception as e:
                logger.error(f"Error scheduling via AI: {e}", exc_info=True)
                await safe_reply(msg_obj, f"❌ Rejalashtirishda xatolik: {html.escape(str(e))}", parse_mode="HTML")
                return True

        @dp.message(F.text.regexp(r"(?i)(?:^|.*?)@([a-zA-Z0-9_]{3,32})\s+(.+)"))
        async def handle_natural_crm_or_schedule(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            if message.text.startswith("/") and not message.text.startswith("/crm"):
                return

            # 1. Check if this is a scheduling request
            lower_text = message.text.lower()
            schedule_triggers = ["ertaga", "bugun", "soat", "da ", "keyin", "rejalashtir", "yubor", "jo'nat", "eslat", "minut", "daqiq"]
            if any(tr in lower_text for tr in schedule_triggers):
                handled = await _schedule_via_ai(message, message.text)
                if handled:
                    return

            # 2. Check if this is a CRM instruction via AI parser
            crm_parsed = await self.ai_engine.parse_crm_intent(message.text)
            if crm_parsed and crm_parsed.get("is_crm") and crm_parsed.get("username"):
                return await _apply_contact_instruction(
                    message,
                    crm_parsed["username"],
                    crm_parsed.get("custom_instructions", message.text),
                    forced_style=crm_parsed.get("addressing_style"),
                )

            # 3. Fallback regex match
            match = re.search(r"(?i)@([a-zA-Z0-9_]{3,32})\s+(.+)", message.text)
            if match:
                target = match.group(1)
                instruction = match.group(2)
                await _apply_contact_instruction(message, target, instruction)

        async def _render_contacts_list(target_msg: types.Message, is_callback: bool = False):
            """Renders rich contacts list with addressing style badges and instant toggle buttons."""
            contacts = await self.db_manager.list_contacts()
            if not contacts:
                empty_text = (
                    "📋 <b>SAQLANGAN KONTAKTLAR VA MUROJAAT USLUBLARI:</b>\n\n"
                    "<i>Hozircha CRM xotirasida hech qanday kontakt saqlanmagan.</i>\n\n"
                    "💡 <b>Yangi kontakt va uslub belgilash:</b>\n"
                    "• <code>/crm @username siz [ko'rsatma]</code> — SIZLASH (hurmat bilan)\n"
                    "• <code>/crm @username sen [ko'rsatma]</code> — SENSIRASH (yaqin do'st)\n\n"
                    "<i>Yoki tabiiy tilda yozing:</i>\n"
                    "• <code>@umid_namozov ni senla, bu mening yaqin do'stim</code>\n"
                    "• <code>@davron_aka ni sizla, hurmatli inson</code>"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                if is_callback:
                    await target_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                else:
                    await safe_reply(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            lines = [
                f"📋 <b>SAQLANGAN KONTAKTLAR ({len(contacts)} ta):</b>\n"
                f"🛡 <i>Xavfsizlik: Bot FAQAT shu ro'yxatdagi insonlargagina avtomatik javob beradi. Boshqa barcha xabarlar sizga to'g'ridan-to'g'ri yetkaziladi.</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
            ]
            builder = InlineKeyboardBuilder()

            for c in contacts:
                uname = f"@{c.username}" if c.username else c.full_name
                safe_uname = html.escape(uname)
                target_user = c.username or str(c.user_id)
                style = getattr(c, "addressing_style", "siz") or "siz"
                safe_rel = html.escape(c.relationship_type or "general")
                safe_instr = html.escape(c.custom_instructions or "N/A")

                if style == "sen":
                    style_badge = "🟢 <b>SEN (Sensirash)</b>"
                    switch_btn = f"🔄 'SIZ' ga o'tkazish"
                    switch_to = "siz"
                else:
                    style_badge = "🔵 <b>SIZ (Sizlash)</b>"
                    switch_btn = f"🔄 'SEN' ga o'tkazish"
                    switch_to = "sen"

                lines.append(
                    f"👤 <b>{safe_uname}</b>\n"
                    f"🗣 Murojaat: {style_badge}\n"
                    f"🎭 Munosabat: <code>{safe_rel}</code>\n"
                    f"📜 Ko'rsatma: <i>\"{safe_instr}\"</i>\n"
                )
                builder.button(text=f"{switch_btn} ({safe_uname})", callback_data=f"crm_style:{target_user}:{switch_to}")
                builder.button(text=f"🗑 O'chirish ({safe_uname})", callback_data=f"crm_del:{target_user}")

            builder.button(text="🔄 Yangilash", callback_data="crm:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_layout = [2] * len(contacts) + [2]
            builder.adjust(*adjust_layout)

            content = "\n".join(lines)
            if is_callback:
                await target_msg.edit_text(content, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, content, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("contacts"))
        async def cmd_contacts(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_contacts_list(message, is_callback=False)

        @dp.message(Command("schedule"))
        async def cmd_schedule(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            text_after_cmd = message.text[len("/schedule"):].strip()
            if not text_after_cmd:
                return await safe_reply(
                    message,
                    "ℹ️ <b>Vazifa rejalashtirish (AI & Classic):</b>\n\n"
                    "💡 <b>1. Tabiiy tilda (AI avtomatik tushunadi):</b>\n"
                    "• <code>/schedule Ertaga soat 10 da @umid_namozov ga 'salom' deb yoz</code>\n"
                    "• <code>/schedule 15 daqiqadan keyin @ali ga 'hujjatlar tayyor' deb jo'nat</code>\n\n"
                    "⚡ <b>2. Standart formatda:</b>\n"
                    "• <code>/schedule @username HH:MM Matn</code>\n"
                    "<i>(Masalan: <code>/schedule @ali 18:30 Uchrashamizmi?</code>)</i>",
                    parse_mode="HTML",
                )

            # Check classic format: @username HH:MM Text
            parts = text_after_cmd.split(maxsplit=2)
            if len(parts) >= 3 and re.match(r"^\d{1,2}:\d{2}$", parts[1]) and (parts[0].startswith("@") or parts[0].isdigit() or parts[0].startswith("+")):
                target = parts[0]
                time_str = parts[1]
                msg_text = parts[2]
                try:
                    hour, minute = map(int, time_str.split(":"))
                    tz = pytz.timezone(settings.TIMEZONE)
                    now = datetime.now(tz)
                    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target_dt <= now:
                        target_dt += timedelta(days=1)

                    task = await self.scheduler_service.schedule_message(
                        target_peer=target,
                        message_text=msg_text,
                        run_at=target_dt,
                    )
                    safe_target = html.escape(target)
                    safe_msg = html.escape(msg_text)
                    formatted_time = format_task_time_human(target_dt.isoformat(), tz)

                    builder = InlineKeyboardBuilder()
                    builder.button(text=f"🚀 Hozir yuborish #{task.id}", callback_data=f"task_send:{task.id}")
                    builder.button(text=f"❌ Bekor qilish #{task.id}", callback_data=f"task_cancel:{task.id}")
                    builder.button(text="⏳ Barcha vazifalar", callback_data="tasks:refresh")
                    builder.adjust(2, 1)

                    return await safe_reply(
                        message,
                        f"⏰ <b>Xabar muvaffaqiyatli rejalashtirildi! (ID #{task.id})</b>\n\n"
                        f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                        f"🕒 <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                        f"💬 <b>Xabar:</b> <i>\"{safe_msg}\"</i>",
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(f"Classic schedule parse failed, trying AI: {e}")

            # AI Parser fallback for natural language
            handled = await _schedule_via_ai(message, text_after_cmd)
            if not handled:
                await safe_reply(
                    message,
                    "⚠️ <i>Xabar rejalashtirish tafsilotlarini tushunib bo'lmadi.</i>\n\n"
                    "Misol: <code>/schedule Ertaga soat 10 da @ali ga salom deb yoz</code>",
                    parse_mode="HTML"
                )

        async def _render_tasks_list(target_msg: types.Message, is_callback: bool = False):
            """Renders pending tasks list with clean human-readable dates and action buttons."""
            tasks = await self.db_manager.get_pending_tasks()
            tz = pytz.timezone(settings.TIMEZONE)

            if not tasks:
                empty_text = (
                    "⏳ <b>KUTILAYOTGAN REJADAGI VAZIFALAR:</b>\n\n"
                    "<i>Hozircha hech qanday rejalashtirilgan vazifalar yo'q.</i>\n\n"
                    "💡 <b>Yangi reja qo'shish (AI yordamida):</b>\n"
                    "Istalgan tabiiy tilda yozishingiz mumkin, masalan:\n"
                    "• <code>Ertaga soat 10 da @ali ga salom deb yoz</code>\n"
                    "• <code>15 daqiqadan keyin @umid_namozov ga loyiha tayyor deb jo'nat</code>\n"
                    "• <code>/schedule @username 14:00 Matn</code>"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="📜 Bajarilganlar tarixi", callback_data="task_hist:open")
                builder.button(text="🔄 Yangilash", callback_data="tasks:refresh")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 2)
                if is_callback:
                    await target_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                else:
                    await safe_reply(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            lines = [f"⏳ <b>KUTILAYOTGAN REJADAGI VAZIFALAR ({len(tasks)} ta):</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
            builder = InlineKeyboardBuilder()

            for t in tasks:
                safe_target = html.escape(t.target_peer)
                formatted_time = format_task_time_human(t.scheduled_time, tz)
                safe_msg = html.escape(t.message_text)

                lines.append(
                    f"📌 <b>Vazifa #{t.id}</b>\n"
                    f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                    f"⏰ <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                    f"💬 <b>Matn:</b> <i>\"{safe_msg}\"</i>\n"
                )
                builder.button(text=f"🚀 Yuborish #{t.id}", callback_data=f"task_send:{t.id}")
                builder.button(text=f"❌ Bekor #{t.id}", callback_data=f"task_cancel:{t.id}")

            builder.button(text="📜 Tarix", callback_data="task_hist:open")
            builder.button(text="🔄 Yangilash", callback_data="tasks:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_pattern = [2] * len(tasks) + [2, 1]
            builder.adjust(*adjust_pattern)

            content = "\n".join(lines)
            if is_callback:
                await target_msg.edit_text(content, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, content, reply_markup=builder.as_markup(), parse_mode="HTML")

        async def _render_task_history(target_msg: types.Message, is_callback: bool = False):
            """Renders executed and cancelled task history."""
            tasks = await self.db_manager.get_task_history(limit=8)
            tz = pytz.timezone(settings.TIMEZONE)

            if not tasks:
                text = "📜 <b>BAJARILGAN VAZIFALAR TARIXI:</b>\n\n<i>Hozircha tarix mavjud emas.</i>"
            else:
                lines = ["📜 <b>BAJARILGAN VAZIFALAR TARIXI (Oxirgi 8 ta):</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
                for t in tasks:
                    safe_target = html.escape(t.target_peer)
                    formatted_time = format_task_time_human(t.scheduled_time, tz)
                    safe_msg = html.escape(t.message_text[:80])
                    if t.status == "executed":
                        status_badge = "✅ Yuborilgan"
                    elif t.status == "cancelled":
                        status_badge = "🛑 Bekor qilingan"
                    else:
                        status_badge = "⚠️ Xato bo'lgan"

                    lines.append(
                        f"• <b>#{t.id} -> {safe_target}</b> [{status_badge}]\n"
                        f"  ⏰ {formatted_time}\n"
                        f"  💬 <i>\"{safe_msg}\"</i>\n"
                    )
                text = "\n".join(lines)

            builder = InlineKeyboardBuilder()
            builder.button(text="⏳ Kutilayotgan vazifalar", callback_data="tasks:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(1, 1)

            if is_callback:
                await target_msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("tasks"))
        async def cmd_tasks(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_tasks_list(message, is_callback=False)

        @dp.callback_query(F.data.startswith("escalate_"))
        async def handle_escalation_callbacks(callback: types.CallbackQuery, state: FSMContext):
            data_parts = callback.data.split(":")
            action = data_parts[0]  # escalate_send, escalate_custom, escalate_ignore
            esc_id = data_parts[1]

            item = self._pending_escalations.get(esc_id)
            if not item:
                return await callback.answer("Bu so'rov muddati tugagan yoki allaqachon ko'rib chiqilgan.")

            if action == "escalate_send":
                suggested = item["suggested_reply"]
                peer = item["peer"]
                if self._userbot_send_fn:
                    await self._userbot_send_fn(peer, suggested)

                safe_peer = html.escape(peer)
                safe_sugg = html.escape(suggested)
                try:
                    await callback.message.edit_text(
                        f"✅ <b>Tavsiya etilgan javob yuborildi!</b>\n\n👤 <b>Kimga:</b> <code>{safe_peer}</code>\n💬 <b>Matn:</b> <i>{safe_sugg}</i>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                self._pending_escalations.pop(esc_id, None)
                await callback.answer("Yuborildi!")

            elif action == "escalate_ignore":
                try:
                    await callback.message.edit_text(
                        "🛑 <b>Bu xabar e'tibordan chetda qoldirildi / O'zingiz javob berasiz.</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                self._pending_escalations.pop(esc_id, None)
                await callback.answer("Bekor qilindi.")

            elif action == "escalate_custom":
                await state.set_state(ReplyState.waiting_for_custom_reply)
                await state.update_data(esc_id=esc_id)
                await callback.message.reply(
                    "✍️ <b>Yuborilishi kerak bo'lgan yangi matnni yozing:</b>",
                    parse_mode="HTML",
                )
                await callback.answer()

        @dp.message(ReplyState.waiting_for_custom_reply)
        async def process_custom_reply(message: types.Message, state: FSMContext):
            data = await state.get_data()
            esc_id = data.get("esc_id")
            item = self._pending_escalations.get(esc_id)

            if not item:
                await state.clear()
                return await safe_reply(message, "So'rov topilmadi.")

            peer = item["peer"]
            custom_text = message.text

            if self._userbot_send_fn:
                await self._userbot_send_fn(peer, custom_text)

            safe_peer = html.escape(peer)
            safe_text = html.escape(custom_text)
            await safe_reply(
                message,
                f"✅ <b>Siz kiritgan javob profilingiz nomidan yuborildi!</b>\n\n"
                f"👤 <b>Kimga:</b> <code>{safe_peer}</code>\n"
                f"💬 <b>Matn:</b> <i>{safe_text}</i>",
                parse_mode="HTML",
            )
            self._pending_escalations.pop(esc_id, None)
            await state.clear()

        @dp.callback_query(F.data.startswith("esc_crm_add:"))
        async def handle_esc_crm_add(callback: types.CallbackQuery):
            esc_id = callback.data.split(":")[1]
            item = self._pending_escalations.get(esc_id)
            if not item:
                return await callback.answer("Bu so'rov muddati tugagan.", show_alert=True)

            peer = item["peer"]
            builder = InlineKeyboardBuilder()
            builder.button(text="🔵 SIZ (Hurmat bilan qo'shish)", callback_data=f"esc_crm_save:{esc_id}:siz")
            builder.button(text="🟢 SEN (Do'st sifatida qo'shish)", callback_data=f"esc_crm_save:{esc_id}:sen")
            builder.button(text="🔙 Orqaga", callback_data=f"esc_back:{esc_id}")
            builder.adjust(1)

            safe_peer = html.escape(peer)
            safe_name = html.escape(item.get("sender_name", peer))
            await callback.message.edit_text(
                f"👤 <b>{safe_name} ({safe_peer}) ni /contacts ro'yxatiga qo'shish:</b>\n\n"
                f"Bot ushbu inson bilan qanday murojaat uslubida suhbatlashsin?",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
            await callback.answer()

        @dp.callback_query(F.data.startswith("esc_crm_save:"))
        async def handle_esc_crm_save(callback: types.CallbackQuery):
            parts = callback.data.split(":")
            esc_id = parts[1]
            style = parts[2]

            item = self._pending_escalations.get(esc_id)
            if not item:
                return await callback.answer("Bu so'rov muddati tugagan.", show_alert=True)

            peer = item["peer"]
            target_clean = peer.replace("@", "").strip()
            user_id = int(target_clean) if target_clean.isdigit() else abs(hash(target_clean)) % 1000000000
            username = target_clean if not target_clean.isdigit() else None
            sender_name = item.get("sender_name", target_clean)
            style_name = "SIZ (Hurmat / Sizlash)" if style == "siz" else "SEN (Yaqin do'st / Sensirash)"
            role = "hurmatli_inson" if style == "siz" else "yaqin_dost"
            instruction = "Hurmat bilan, muloyim muloqot" if style == "siz" else "Samimiy, do'stona muloqot"

            await self.db_manager.upsert_contact(
                user_id=user_id,
                full_name=sender_name,
                username=username,
                relationship_type=role,
                addressing_style=style,
                custom_instructions=instruction,
            )

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Shu zaxotiyoq AI javobini yuborish", callback_data=f"escalate_send:{esc_id}")
            builder.button(text="✍️ O'zim javob yozaman", callback_data=f"escalate_custom:{esc_id}")
            builder.button(text="📋 Barcha kontaktlar", callback_data="crm:refresh")
            builder.adjust(1)

            safe_name = html.escape(sender_name)
            safe_peer = html.escape(peer)
            await callback.message.edit_text(
                f"✨ <b>{safe_name} ({safe_peer}) muvaffaqiyatli /contacts ro'yxatiga qo'shildi!</b>\n\n"
                f"🗣 <b>Murojaat uslubi:</b> <code>{style_name}</code>\n"
                f"🛡 <b>Endi bot bu inson bilan keyingi safar avtomatik suhbatlasha oladi!</b>\n\n"
                f"<i>Kelgan oxirgi xabarga javob yuborasizmi?</i>",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
            await callback.answer("Kontakt saqlandi!")

        @dp.callback_query(F.data.startswith("esc_back:"))
        async def handle_esc_back(callback: types.CallbackQuery):
            esc_id = callback.data.split(":")[1]
            item = self._pending_escalations.get(esc_id)
            if not item:
                return await callback.answer("So'rov eskirgan.")

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ AI javobini yuborish", callback_data=f"escalate_send:{esc_id}")
            builder.button(text="✍️ O'zim javob yozaman", callback_data=f"escalate_custom:{esc_id}")
            builder.button(text="➕ Ro'yxatga qo'shish (/crm)", callback_data=f"esc_crm_add:{esc_id}")
            builder.button(text="🛑 E'tiborsiz qoldirish", callback_data=f"escalate_ignore:{esc_id}")
            builder.adjust(1)

            safe_sender = html.escape(item.get("sender_name", item["peer"]))
            safe_peer = html.escape(item["peer"])
            safe_msg = html.escape(item.get("incoming_message", ""))
            safe_reply_text = html.escape(item.get("suggested_reply", "N/A"))

            text = (
                f"📩 <b>RO'YXATDA YO'Q FOYDALANUVCHIDAN XABAR!</b>\n\n"
                f"👤 <b>Kimdan:</b> {safe_sender} (<code>{safe_peer}</code>)\n"
                f"💬 <b>Xabar:</b> <i>\"{safe_msg}\"</i>\n\n"
                f"🛡 <b>Holat:</b> <i>Bu inson <code>/contacts</code> ro'yxatingizda yo'q. Qat'iy qoidangizga asosan bot sizning nomingizdan javob bermadi va xabarni to'g'ridan-to'g'ri sizga yetkazdi.</i>\n\n"
                f"💡 <b>AI tavsiya qilgan javob varianti:</b>\n<code>{safe_reply_text}</code>\n\n"
                "Nima qilamiz, Xo'jayin?"
            )
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            await callback.answer()

        @dp.message(F.text & ~F.text.startswith("/"))
        async def handle_fallback_natural_text(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return
            current_state = await state.get_state()
            if current_state:
                return

            lower = message.text.lower()
            schedule_triggers = ["ertaga", "bugun", "soat", "rejalashtir", "yubor", "jo'nat", "eslat", "daqiq", "minut"]
            if any(tr in lower for tr in schedule_triggers):
                handled = await _schedule_via_ai(message, message.text)
                if handled:
                    return

            text = (
                "👑 <b>Xo'jayin, yangi buyruq kutmoqdaman.</b>\n\n"
                "Quyidagi imkoniyatlardan foydalanishingiz mumkin:\n"
                "• <b>Vazifa rejalashtirish (AI):</b> <code>Ertaga soat 10 da @ali ga salom deb yoz</code>\n"
                "• <b>Muloqot uslubini belgilash:</b> <code>@ali bu inson bilan samimiy bo'l</code>\n"
                "• <b>Brifing olish:</b> <code>/digest</code>"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="📰 Executive Digest", callback_data="digest:open")
            builder.button(text="⏳ Rejadagi vazifalar", callback_data="tasks:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(2, 1)
            await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("digest:"))
        async def handle_digest_callbacks(callback: types.CallbackQuery):
            action = callback.data.split(":")[1]
            if action == "open":
                await callback.answer("Digest yuklanmoqda...")
                await _execute_digest(callback.message, is_callback=True, force_recent=False)
            elif action == "refresh":
                await callback.answer("Qayta tahlil qilinmoqda...")
                await _execute_digest(callback.message, is_callback=True, force_recent=True)
            elif action == "stats":
                await callback.answer()
                stats = await self.digest_service.get_stats() if self.digest_service else {}
                total = stats.get("total_posts", 0)
                unsum = stats.get("unsummarized", 0)
                sources = stats.get("unique_sources", 0)
                text = (
                    "📊 <b>TELEGRAM KANAL VA GURUHLAR STATISTIKASI:</b>\n\n"
                    f"• <b>Jami yig'ilgan xabarlar:</b> <code>{total}</code> ta\n"
                    f"• <b>Tahlil qilinmagan xabarlar:</b> <code>{unsum}</code> ta\n"
                    f"• <b>Kuzatilayotgan manbalar soni:</b> <code>{sources}</code> ta kanal/guruh\n\n"
                    "<i>Userbot profilingiz a'zo bo'lgan barcha kanal va guruhlardagi postlarni tahlil uchun yig'ib boradi.</i>"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="📰 Yangi Digest", callback_data="digest:open")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1)
                await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("menu:"))
        async def handle_menu_callbacks(callback: types.CallbackQuery):
            action = callback.data.split(":")[1]
            if action == "main":
                await callback.answer()
                text = (
                    "👑 <b>Salom, Xo'jayin! Antigravity AI Butler Kokpiti:</b>\n\n"
                    "Sizning shaxsiy profilingizni to'liq boshqaruvchi markaz.\n\n"
                    "📌 <b>Asosiy buyruqlar:</b>\n"
                    "• <code>/crm @username [rol] [ko'rsatma]</code> — Kontakt uslubini belgilash\n"
                    "• <code>/contacts</code> — Saqlangan kontaktlar\n"
                    "• <code>/schedule @username HH:MM Matn</code> — Xabar rejalashtirish\n"
                    "• <code>/tasks</code> — Kutilayotgan rejalashtirilgan xabarlar\n"
                    "• <code>/digest</code> — Kanallar va guruhlardan xulosalar\n"
                    "• <code>/status</code> — Tizim holati"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="📰 Executive Digest", callback_data="digest:open")
                builder.button(text="📋 Kontaktlar", callback_data="menu:contacts")
                builder.button(text="⏰ Rejadagi vazifalar", callback_data="menu:tasks")
                builder.button(text="⚡ Tizim holati", callback_data="menu:status")
                builder.adjust(2, 2)
                await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            elif action == "contacts":
                await callback.answer()
                await _render_contacts_list(callback.message, is_callback=True)
            elif action == "tasks":
                await callback.answer()
                await _render_tasks_list(callback.message, is_callback=True)
            elif action == "status":
                await callback.answer()
                auto_reply_str = "Yoqilgan" if settings.AUTO_REPLY_ENABLED else "O'chirilgan"
                phone_str = html.escape(settings.TELEGRAM_PHONE_NUMBER or "Faol")
                model_str = html.escape(settings.GEMINI_MODEL)
                tz_str = html.escape(settings.TIMEZONE)
                text = (
                    "⚡ <b>ANTIGRAVITY BUTLER TIZIM HOLATI:</b>\n\n"
                    f"• <b>AI Modeli:</b> <code>{model_str}</code>\n"
                    f"• <b>Avto-javob holati:</b> <code>{auto_reply_str}</code>\n"
                    f"• <b>Vaqt mintaqasi:</b> <code>{tz_str}</code>\n"
                    f"• <b>Telegram Profil:</b> <code>{phone_str}</code>\n"
                    "• <b>Kontaktlar filtri:</b> 🛡 Faol (Faqat /contacts dagi insonlar)\n"
                    "• <b>Holat:</b> Barcha xizmatlar 100% faol."
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("crm_style:"))
        async def handle_crm_style_toggle(callback: types.CallbackQuery):
            parts = callback.data.split(":")
            username = parts[1]
            new_style = parts[2]

            contact = await self.db_manager.get_contact_by_username(username)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            await self.db_manager.update_addressing_style(contact.user_id, new_style)
            style_name = "SIZ (Sizlash / Hurmat)" if new_style == "siz" else "SEN (Sensirash / Do'st)"
            await callback.answer(f"✅ @{username} endi {style_name} deb gaplashiladi!", show_alert=True)
            await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("crm_del:"))
        async def handle_crm_delete(callback: types.CallbackQuery):
            username = callback.data.split(":")[1]
            contact = await self.db_manager.get_contact_by_username(username)
            if contact:
                await self.db_manager.delete_contact(contact.user_id)
                await callback.answer(f"🗑 @{username} CRM xotirasidan o'chirildi!", show_alert=True)
            else:
                await callback.answer("Kontakt topilmadi.", show_alert=True)
            await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data == "crm:refresh")
        async def handle_crm_refresh(callback: types.CallbackQuery):
            await callback.answer("Kontaktlar yangilandi")
            await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("task_send:"))
        async def handle_task_send_now(callback: types.CallbackQuery):
            task_id = int(callback.data.split(":")[1])
            success = await self.scheduler_service.execute_now(task_id)
            if success:
                await callback.answer(f"🚀 #{task_id} xabar zudlik bilan yuborildi!", show_alert=True)
            else:
                await callback.answer(f"❌ #{task_id} vazifa topilmadi yoki allaqachon bajarilgan.", show_alert=True)
            await _render_tasks_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("task_cancel:"))
        async def handle_task_cancel(callback: types.CallbackQuery):
            task_id = int(callback.data.split(":")[1])
            await self.scheduler_service.cancel_task(task_id)
            await callback.answer(f"🛑 #{task_id} vazifa bekor qilindi!", show_alert=True)
            await _render_tasks_list(callback.message, is_callback=True)

        @dp.callback_query(F.data == "task_hist:open")
        async def handle_task_history_open(callback: types.CallbackQuery):
            await callback.answer()
            await _render_task_history(callback.message, is_callback=True)

        @dp.callback_query(F.data == "tasks:refresh")
        async def handle_tasks_refresh(callback: types.CallbackQuery):
            await callback.answer("Vazifalar ro'yxati yangilandi")
            await _render_tasks_list(callback.message, is_callback=True)

    async def notify_boss_escalation(
        self,
        peer: str,
        sender_name: str,
        incoming_message: str,
        suggested_reply: Optional[str],
        reason: str,
        is_unknown_contact: bool = False,
    ):
        """Sends rich interactive escalation alert to Boss with inline buttons."""
        if not self.bot or not self.admin_id:
            logger.info(f"Escalation notification for {sender_name}: {incoming_message}")
            return

        esc_id = f"esc_{int(datetime.now().timestamp() * 1000)}"
        clean_username = peer.replace("@", "").strip() if peer.startswith("@") else ""
        self._pending_escalations[esc_id] = {
            "peer": peer,
            "sender_name": sender_name,
            "incoming_message": incoming_message,
            "suggested_reply": suggested_reply or "Assalomu alaykum! Tez orada aloqaga chiqaman.",
            "is_unknown": is_unknown_contact,
            "username": clean_username,
        }

        safe_sender = html.escape(sender_name)
        safe_peer = html.escape(peer)
        safe_msg = html.escape(incoming_message)
        safe_reply_text = html.escape(suggested_reply or "Assalomu alaykum! Tez orada aloqaga chiqaman.")

        builder = InlineKeyboardBuilder()
        if is_unknown_contact:
            builder.button(text="✅ AI javobini yuborish", callback_data=f"escalate_send:{esc_id}")
            builder.button(text="✍️ O'zim javob yozaman", callback_data=f"escalate_custom:{esc_id}")
            builder.button(text="➕ Ro'yxatga qo'shish (/crm)", callback_data=f"esc_crm_add:{esc_id}")
            builder.button(text="🛑 E'tiborsiz qoldirish", callback_data=f"escalate_ignore:{esc_id}")
            builder.adjust(1)

            text = (
                f"📩 <b>RO'YXATDA YO'Q FOYDALANUVCHIDAN XABAR!</b>\n\n"
                f"👤 <b>Kimdan:</b> {safe_sender} (<code>{safe_peer}</code>)\n"
                f"💬 <b>Xabar:</b> <i>\"{safe_msg}\"</i>\n\n"
                f"🛡 <b>Holat:</b> <i>Bu inson <code>/contacts</code> ro'yxatingizda yo'q. Qat'iy qoidangizga asosan bot profilingiz nomidan javob bermadi va xabarni to'g'ridan-to'g'ri sizga yetkazdi.</i>\n\n"
                f"💡 <b>Tavsiya etilgan AI javobi:</b>\n<code>{safe_reply_text}</code>\n\n"
                "Nima qilamiz, Xo'jayin?"
            )
        else:
            builder.button(text="✅ Shu javobni yuborish", callback_data=f"escalate_send:{esc_id}")
            builder.button(text="✍️ O'zgartirib yozish", callback_data=f"escalate_custom:{esc_id}")
            builder.button(text="🛑 O'zim yozaman", callback_data=f"escalate_ignore:{esc_id}")
            builder.adjust(1)

            safe_reason = html.escape(reason)
            text = (
                f"🔔 <b>XO'JAYIN, MUHIM XABAR KELDI!</b>\n\n"
                f"👤 <b>Kimdan:</b> {safe_sender} (<code>{safe_peer}</code>)\n"
                f"📩 <b>Xabar:</b> <i>\"{safe_msg}\"</i>\n\n"
                f"⚠️ <b>Sabab:</b> <i>{safe_reason}</i>\n"
                f"💡 <b>Tavsiya etilgan AI javobi:</b>\n<code>{safe_reply_text}</code>\n\n"
                "Nima qilamiz?"
            )

        await safe_send(
            bot=self.bot,
            chat_id=self.admin_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )

    async def send_notification(self, text: str):
        """Sends notification to Boss safely."""
        if not self.bot or not self.admin_id:
            logger.info(f"Notification to Boss: {text}")
            return
        await safe_send(bot=self.bot, chat_id=self.admin_id, text=text, parse_mode="HTML")
