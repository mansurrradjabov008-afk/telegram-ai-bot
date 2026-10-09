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
from database.models import Draft
from services.ai_engine import AIEngine
from services.scheduler_service import SchedulerService
from services.digest_service import DigestService
from services.schedule_parser import (
    parse_schedule_intent_hybrid,
    parse_deterministic_schedule,
    extract_peer_and_text_without_time,
    clean_target_peer,
    clean_message_text,
    ScheduleParseResult,
)

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


USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def normalize_and_validate_username(raw: str) -> Optional[str]:
    """
    Normalizes (strip '@', lowercase) and validates against ^[A-Za-z][A-Za-z0-9_]{4,31}$.
    Returns lowercase username string if valid, None if invalid.
    """
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if USERNAME_REGEX.match(cleaned):
        return cleaned.lower()
    return None


class ReplyState(StatesGroup):
    waiting_for_custom_reply = State()
    waiting_for_draft_edit = State()


class ScheduleWizardState(StatesGroup):
    waiting_for_peer = State()
    waiting_for_time = State()
    waiting_for_text = State()


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
        self.admin_id = admin_id if admin_id is not None else settings.owner_id_effective

        self.bot = Bot(token=self.token) if self.token and not self.token.startswith("1234567890:") else None
        self.dp = Dispatcher(storage=MemoryStorage())
        self._userbot_send_fn: Optional[Callable] = None
        self._manual_reply_check_fn: Optional[Callable] = None
        self._pending_escalations: Dict[str, Dict[str, Any]] = {}

        self._setup_handlers()

    def set_userbot_sender(self, fn: Callable):
        self._userbot_send_fn = fn

    def set_manual_reply_checker(self, fn: Callable):
        self._manual_reply_check_fn = fn


    def _is_admin(self, user_id: int) -> bool:
        """Owner-only check comparing numeric Telegram ID with owner ID. Returns False if unconfigured or mismatched."""
        effective_id = self.admin_id if self.admin_id is not None else settings.owner_id_effective
        if not effective_id:
            return False
        return user_id == effective_id

    def _setup_handlers(self):
        dp = self.dp

        @dp.message(CommandStart())
        async def cmd_start(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            text = (
                "👑 <b>Salom, Xo'jayin! Antigravity AI Butler Kokpiti tayyor.</b>\n\n"
                "Men sizning shaxsiy profilingizni to'liq boshqaruvchi markazman.\n\n"
                "📌 <b>Asosiy buyruqlar:</b>\n"
                "• <code>/mode @username auto|draft|mute</code> — Kontakt avtonomiya rejimini belgilash\n"
                "• <code>/contacts</code> — Saqlangan kontaktlar va ularning rejimlari\n"
                "• <code>/drafts</code> — Kutilayotgan qoralamalar ro'yxati va tasdiqlash\n"
                "• <code>/crm @username [rol] [ko'rsatma]</code> — Kontakt munosabatini belgilash\n"
                "• <code>/schedule @username HH:MM Matn</code> — Belgilangan vaqtga xabar rejalashtirish\n"
                "• <code>/tasks</code> — Kutilayotgan rejalashtirilgan xabarlar\n"
                "• <code>/digest</code> — Kanallar va guruhlardan xulosalar\n"
                "• <code>/status</code> — Tizim holati va avtonomiya hisoboti"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="📰 Executive Digest", callback_data="digest:open")
            builder.button(text="📋 Kontaktlar", callback_data="menu:contacts")
            builder.button(text="📝 Qoralamalar", callback_data="menu:drafts")
            builder.button(text="⏰ Rejadagi vazifalar", callback_data="menu:tasks")
            builder.button(text="⚡ Tizim holati", callback_data="menu:status")
            builder.adjust(2, 3)
            await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("status"))
        async def cmd_status(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            auto_reply_str = "Yoqilgan" if settings.AUTO_REPLY_ENABLED else "O'chirilgan"
            phone_str = html.escape(settings.TELEGRAM_PHONE_NUMBER or "Faol")
            model_str = html.escape(settings.GEMINI_MODEL)
            tz_str = html.escape(settings.TIMEZONE)

            mode_counts = await self.db_manager.get_mode_counts()
            pending_drafts = await self.db_manager.count_pending_drafts()
            total_contacts = mode_counts.get("total", mode_counts.get("auto", 0) + mode_counts.get("draft", 0) + mode_counts.get("mute", 0))

            status_text = (
                "⚡ <b>ANTIGRAVITY BUTLER TIZIM HOLATI:</b>\n\n"
                f"• <b>AI Modeli:</b> <code>{model_str}</code>\n"
                f"• <b>Avto-javob holati:</b> <code>{auto_reply_str}</code>\n"
                f"• <b>Vaqt mintaqasi:</b> <code>{tz_str}</code>\n"
                f"• <b>Telegram Profil:</b> <code>{phone_str}</code>\n"
                "• <b>Kontaktlar filtri:</b> 🛡 Faol (Faqat /contacts dagi insonlar)\n"
                f"• <b>Avtonomiya rejimlari:</b> ⚡ auto: {mode_counts.get('auto', 0)} | 📝 draft: {mode_counts.get('draft', 0)} | 🔇 mute: {mode_counts.get('mute', 0)} (Jami: {total_contacts})\n"
                f"• <b>Kutilayotgan qoralamalar:</b> <code>{pending_drafts}</code> ta\n"
                "• <b>Holat:</b> Barcha xizmatlar 100% faol."
            )
            await safe_reply(message, status_text, parse_mode="HTML")

        @dp.message(Command("restart"))
        async def cmd_restart(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await safe_reply(
                message,
                "🔄 <b>Antigravity Butler qayta yuklanmoqda...</b>\n\n"
                "<i>Watchdog jarayoni 5 soniya ichida botni eng so'nggi yangilangan kod bilan qayta ishga tushiradi.</i>",
                parse_mode="HTML",
            )
            import os
            await asyncio.sleep(1.0)
            os._exit(0)


        async def _render_digest_sources(target_msg: types.Message, is_callback: bool = False):
            if not self.digest_service:
                text = "❌ <b>Digest xizmati ulanmagan.</b>"
                if is_callback:
                    return await target_msg.edit_text(text, parse_mode="HTML")
                return await safe_reply(target_msg, text, parse_mode="HTML")

            sources = await self.digest_service.list_sources_summary()
            if not sources:
                empty_text = (
                    "📋 <b>KUZATILAYOTGAN MANBALAR RO'YXATI (DIGEST SOURCES):</b>\n\n"
                    "<i>Hozircha hech qanday manba qo'shilmagan.</i>\n\n"
                    "💡 <b>Manba qo'shish:</b>\n"
                    "• <code>/digest add @kanal_nomi</code>\n"
                    "• <code>/digest add https://t.me/kanal_nomi</code>\n\n"
                    "🔒 <i>Eslatma: Shaxsiy yozishmalar (1-ga-1 chat) aslo qo'shilmaydi. Faqat kanal va guruhlar.</i>"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔄 Yangilash", callback_data="digest_sources:list")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1)
                if is_callback:
                    return await target_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return await safe_reply(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")

            lines = [
                f"📋 <b>KUZATILAYOTGAN MANBALAR (Jami: {len(sources)} ta):</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
            ]
            builder = InlineKeyboardBuilder()

            for s in sources:
                type_badge = "📢 Kanal" if s["peer_type"] == "channel" else "👥 Guruh"
                uname = f"@{s['username']}" if s["username"] else f"ID: {s['peer_id']}"
                safe_title = html.escape(s["title"])
                safe_uname = html.escape(uname)
                post_count = s["post_count"]

                lines.append(
                    f"• <b>{safe_title}</b> [{type_badge}]\n"
                    f"  🔗 <code>{safe_uname}</code> | 📝 Postlar: <b>{post_count}</b> ta\n"
                )
                del_id = s["username"] or str(s["peer_id"])
                builder.button(text=f"🗑 O'chirish ({safe_uname})", callback_data=f"digest_src_del:{del_id}")

            builder.button(text="🔄 Yangilash", callback_data="digest_sources:list")
            builder.button(text="📰 Yangi Digest", callback_data="digest:open")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(*([1] * len(sources) + [2, 1]))

            content = "\n".join(lines)
            content += (
                "\n💡 <b>Boshqarish:</b>\n"
                "• <code>/digest add @kanal</code> — Yangi kanal/guruh qo'shish\n"
                "• <code>/digest remove @kanal</code> — Manbani ro'yxatdan o'chirish"
            )

            if is_callback:
                await target_msg.edit_text(content, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, content, reply_markup=builder.as_markup(), parse_mode="HTML")

        async def _execute_digest(target_msg: types.Message, is_callback: bool = False, force_recent: bool = False):
            if not self.digest_service:
                return await safe_reply(target_msg, "❌ <b>Digest xizmati ulanmagan.</b>", parse_mode="HTML")

            # Check feature flag: if V2 disabled, run legacy V1
            if not getattr(settings, "DIGEST_V2_ENABLED", True):
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
                return

            # Smart Digest V2 execution (Phase 1: Collection pipeline)
            loading_text = "⏳ <i>Kuzatilayotgan manbalardan yangi postlar yig'ilmoqda (Smart Digest V2)...</i>"
            if is_callback:
                try:
                    await target_msg.edit_text(loading_text, parse_mode="HTML")
                    active_msg = target_msg
                except Exception:
                    active_msg = await target_msg.answer(loading_text, parse_mode="HTML")
            else:
                active_msg = await target_msg.reply(loading_text, parse_mode="HTML")

            try:
                stats = await self.digest_service.collect_new_posts(trigger="manual")
                total = stats.get("total_collected", 0)
                sources_cnt = stats.get("sources_count", 0)
                purged = stats.get("purged_old_posts", 0)
                errors = stats.get("errors", [])

                if sources_cnt == 0:
                    empty_text = (
                        "ℹ️ <b>Kuzatiladigan manbalar mavjud emas!</b>\n\n"
                        "Smart Digest V2 xabarlarni faqat siz ko'rsatgan kanallar va guruhlardan yig'adi.\n\n"
                        "Qo'shish: <code>/digest add @kanal</code>"
                    )
                    builder = InlineKeyboardBuilder()
                    builder.button(text="📋 Manbalar ro'yxati", callback_data="digest_sources:list")
                    builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                    builder.adjust(1, 1)
                    if is_callback:
                        await active_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    else:
                        await safe_reply(active_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    return

                err_lines = ""
                if errors:
                    err_lines = f"\n⚠️ <b>Muammoli manbalar ({len(errors)} ta):</b>\n"
                    for err in errors[:3]:
                        err_lines += f"• {html.escape(err.get('title', 'Noma'))}: <code>{html.escape(err.get('error', ''))}</code>\n"

                dup_count = stats.get("duplicates_filtered", 0)
                spam_count = stats.get("spam_filtered", 0)
                clean_count = stats.get("clean_remaining", total - dup_count - spam_count)
                suspicious_count = stats.get("suspicious_jobs", 0)

                classified_cnt = stats.get("classified_count", 0)
                discarded_cnt = stats.get("discarded_classified_count", 0)
                skipped_cnt = stats.get("skipped_posts_count", 0)
                failed_batches = stats.get("failed_batches_count", 0)
                topic_counts = stats.get("topic_counts", {})

                topics_str = ""
                topic_labels = {
                    "A": "Siyosat",
                    "B": "Tashqi siyosat",
                    "C": "TSUE",
                    "D": "Kasblar narxlari",
                    "E": "AI",
                    "F": "Imkoniyatlar",
                }
                if topic_counts:
                    topics_str = "\n📑 <b>Klassifikatsiya qilingan mavzular:</b>\n"
                    for t_code in ["A", "B", "C", "D", "E", "F"]:
                        cnt = topic_counts.get(t_code, 0)
                        if cnt > 0:
                            topics_str += f"• <b>{t_code} ({topic_labels[t_code]}):</b> {cnt} ta\n"

                quota_warning = ""
                if skipped_cnt > 0:
                    quota_warning += f"\n⚠️ <b>AI kvotasi sababli qoldirildi:</b> <code>{skipped_cnt}</code> ta post\n"
                if failed_batches > 0:
                    quota_warning += f"⚠️ <b>Xatolik tufayli o'tkazib yuborilgan batch'lar:</b> <code>{failed_batches}</code> ta\n"

                report = (
                    "📰 <b>SMART DIGEST V2 — MONITORING VA KLASSIFIKATSIYA</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📊 <b>Kuzatilgan manbalar:</b> <code>{sources_cnt}</code> ta\n"
                    f"📥 <b>Yangi yig'ilgan postlar:</b> <code>{total}</code> ta\n"
                    f"🧹 <b>Dublikatlar tozalandi:</b> <code>{dup_count}</code> ta\n"
                    f"🛡 <b>Reklama va spamlar:</b> <code>{spam_count}</code> ta\n"
                    f"⚠️ <b>Gumonli e'lonlar:</b> <code>{suspicious_count}</code> ta\n"
                    f"✨ <b>Tahlilga olingan toza postlar:</b> <code>{clean_count}</code> ta\n"
                    f"🎯 <b>Saralangan va kiritilgan:</b> <code>{classified_cnt}</code> ta\n"
                    f"🚫 <b>Ahamiyatsiz/boshqa mavzular:</b> <code>{discarded_cnt}</code> ta\n"
                    f"{topics_str}"
                    f"{quota_warning}"
                    f"🗑 <b>Tozalangan eski postlar:</b> <code>{purged}</code> ta\n"
                    f"{err_lines}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Reklama/spam filtrlandi: {spam_count} ta | Saralangan: {classified_cnt} ta</i>"
                )

                builder = InlineKeyboardBuilder()
                builder.button(text="🔄 Qayta yig'ish", callback_data="digest:refresh")
                builder.button(text="📋 Manbalar", callback_data="digest_sources:list")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(2, 1)

                if is_callback and len(report) <= 4000:
                    try:
                        await active_msg.edit_text(report, reply_markup=builder.as_markup(), parse_mode="HTML")
                        return
                    except Exception:
                        pass

                await safe_reply_or_send_long(active_msg, report, reply_markup=builder.as_markup())

            except Exception as e:
                logger.error(f"Smart Digest V2 collection failed: {e}", exc_info=True)
                err_text = f"❌ <b>Xabarlarni yig'ishda xatolik yuz berdi:</b>\n<code>{html.escape(str(e))}</code>"
                if is_callback:
                    await active_msg.edit_text(err_text, parse_mode="HTML")
                else:
                    await safe_reply(active_msg, err_text, parse_mode="HTML")

        @dp.message(Command("digest"))
        async def cmd_digest(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            raw_text = (message.text or "").strip()
            parts = raw_text.split(maxsplit=2)
            subcmd = parts[1].lower() if len(parts) > 1 else ""

            if subcmd == "sources":
                await _render_digest_sources(message, is_callback=False)
                return
            elif subcmd == "add":
                if len(parts) < 3:
                    return await safe_reply(
                        message,
                        "⚠️ <b>Qo'shish uchun manba ko'rsating.</b>\n"
                        "Foydalanish: <code>/digest add @kanal</code> yoki <code>/digest add https://t.me/kanal</code>",
                        parse_mode="HTML",
                    )
                target = parts[2].strip()
                loading_msg = await safe_reply(message, "⏳ <i>Manba tekshirilmoqda va ulanmoqda...</i>", parse_mode="HTML")
                client = self.digest_service.get_client() if self.digest_service else None
                success, resp_text, source = await self.digest_service.resolve_and_add_source(client, target)
                builder = InlineKeyboardBuilder()
                builder.button(text="📋 Manbalar ro'yxati", callback_data="digest_sources:list")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1)
                await loading_msg.edit_text(resp_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return
            elif subcmd == "remove":
                if len(parts) < 3:
                    return await safe_reply(
                        message,
                        "⚠️ <b>O'chirish uchun manba ko'rsating.</b>\n"
                        "Foydalanish: <code>/digest remove @kanal</code>",
                        parse_mode="HTML",
                    )
                target = parts[2].strip()
                success, resp_text = await self.digest_service.remove_source(target)
                builder = InlineKeyboardBuilder()
                builder.button(text="📋 Manbalar ro'yxati", callback_data="digest_sources:list")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1)
                return await safe_reply(message, resp_text, reply_markup=builder.as_markup(), parse_mode="HTML")

            await _execute_digest(message, is_callback=False)

        async def _apply_contact_instruction(
            msg_obj: types.Message,
            target_user: str,
            instruction_text: str,
            forced_style: Optional[str] = None,
        ):
            target = target_user.replace("@", "").strip()
            instruction = instruction_text.strip()

            if not target.isdigit():
                normalized = normalize_and_validate_username(target)
                if not normalized:
                    return await safe_reply(
                        msg_obj,
                        "❌ <b>Noto'g'ri username formati!</b>\n"
                        "Username 5-32 belgidan iborat bo'lishi va harf bilan boshlanishi kerak (masalan: <code>@foydalanuvchi</code>).",
                        parse_mode="HTML",
                    )
                target = normalized

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
            mode = existing.mode if existing else "draft"

            await self.db_manager.upsert_contact(
                user_id=user_id,
                username=target,
                full_name=existing.full_name if existing else target,
                relationship_type=role,
                addressing_style=addressing,
                mode=mode,
                custom_instructions=instruction,
            )

            safe_target = html.escape(target)
            safe_instruction = html.escape(instruction)
            safe_role_label = html.escape(role_label)

            mode_labels = {
                "auto": "⚡ auto (Avtomatik javob)",
                "draft": "📝 draft (Qoralama / Tasdiq bilan)",
                "mute": "🔇 mute (Ovozsiz)",
            }
            mode_badge = mode_labels.get(mode, f"<code>{mode}</code>")

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
                f"⚙️ <b>Avtonomiya rejimi:</b> {mode_badge}\n"
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

        @dp.message(Command("mode"))
        async def cmd_mode(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            text_after = message.text[len("/mode"):].strip()
            if not text_after:
                contacts = await self.db_manager.list_contacts()
                lines = [
                    "⚙️ <b>AVTONOMIYA REJIMLARI BOSHQARUVI:</b>\n",
                    "📌 <b>Foydalanish:</b>",
                    "• <code>/mode @username auto|draft|mute</code>\n",
                    "💡 <b>Rejimlar:</b>",
                    "• ⚡ <b>auto</b> — Bot avtomatik javob beradi (xavfli mavzulardan tashqari)",
                    "• 📝 <b>draft</b> — Bot javob tayyorlaydi, lekin faqat siz tasdiqlagach yuboriladi",
                    "• 🔇 <b>mute</b> — Bot bu kontaktni to'liq e'tiborsiz qoldiradi\n",
                    "📋 <b>Hozirgi kontaktlar rejimlari:</b>",
                ]
                if not contacts:
                    lines.append("<i>Hozircha kontaktlar mavjud emas.</i>")
                else:
                    for c in contacts:
                        uname = f"@{c.username}" if c.username else c.full_name
                        safe_uname = html.escape(uname)
                        mode = getattr(c, "mode", "draft") or "draft"
                        if mode == "auto":
                            badge = "⚡ auto (Avtomatik)"
                        elif mode == "mute":
                            badge = "🔇 mute (Ovozsiz)"
                        else:
                            badge = "📝 draft (Qoralama)"
                        lines.append(f"• <b>{safe_uname}</b>: <code>{badge}</code>")

                builder = InlineKeyboardBuilder()
                builder.button(text="📋 Barcha kontaktlar", callback_data="crm:refresh")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1)

                return await safe_reply(message, "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")

            parts = text_after.split()
            if len(parts) < 2:
                return await safe_reply(
                    message,
                    "⚠️ <b>Noto'g'ri format.</b>\n"
                    "Foydalanish: <code>/mode @username auto|draft|mute</code>\n"
                    "Misol: <code>/mode @ali auto</code>",
                    parse_mode="HTML",
                )

            raw_username, raw_mode = parts[0], parts[1].lower()
            normalized_username = normalize_and_validate_username(raw_username)
            if not normalized_username:
                return await safe_reply(
                    message,
                    "❌ <b>Noto'g'ri username formati!</b>\n"
                    "Username 5-32 belgidan iborat bo'lishi va harf bilan boshlanishi kerak (masalan: <code>@foydalanuvchi</code>).",
                    parse_mode="HTML",
                )

            if raw_mode not in ("auto", "draft", "mute"):
                return await safe_reply(
                    message,
                    "⚠️ <b>Noto'g'ri rejim!</b>\n"
                    "Mavjud rejimlar: <code>auto</code>, <code>draft</code>, <code>mute</code>.",
                    parse_mode="HTML",
                )

            contact = await self.db_manager.get_contact_by_username(normalized_username)
            if not contact:
                return await safe_reply(
                    message,
                    f"❌ <code>@{normalized_username}</code> kontaktlar ro'yxatida topilmadi.\n"
                    f"Uni avval <code>/crm @{normalized_username} [ko'rsatma]</code> orqali qo'shing.",
                    parse_mode="HTML",
                )

            await self.db_manager.update_contact_mode_by_username(normalized_username, raw_mode)

            mode_labels = {
                "auto": "⚡ auto (Avtomatik javob)",
                "draft": "📝 draft (Qoralama / Tasdiq kerak)",
                "mute": "🔇 mute (Ovozsiz / E'tiborsiz)",
            }
            label = mode_labels.get(raw_mode, raw_mode)

            builder = InlineKeyboardBuilder()
            builder.button(text="📋 Kontaktlar ro'yxati", callback_data="crm:refresh")
            builder.adjust(1)

            await safe_reply(
                message,
                f"✅ <code>@{normalized_username}</code> rejimi o'zgartirildi: <b>{label}</b>.",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )


        async def _schedule_and_respond(msg_obj: types.Message, raw_text: str) -> bool:
            """Parses schedule request via hybrid parser and creates scheduled task."""
            tz = pytz.timezone(settings.TIMEZONE)
            parsed = await parse_schedule_intent_hybrid(raw_text, self.ai_engine, settings.TIMEZONE)
            if not parsed or not parsed.is_schedule:
                return False

            target_peer = parsed.target_peer
            run_at = parsed.run_at
            msg_text = parsed.message_text

            try:
                task = await self.scheduler_service.schedule_message(
                    target_peer=target_peer,
                    message_text=msg_text,
                    run_at=run_at,
                )

                safe_target = html.escape(target_peer)
                safe_msg = html.escape(msg_text)
                formatted_time = format_task_time_human(run_at.isoformat(), tz)

                builder = InlineKeyboardBuilder()
                builder.button(text=f"🚀 Hozir yuborish #{task.id}", callback_data=f"task_send:{task.id}")
                builder.button(text=f"❌ Bekor qilish #{task.id}", callback_data=f"task_cancel:{task.id}")
                builder.button(text="⏳ Barcha vazifalar", callback_data="tasks:refresh")
                builder.adjust(2, 1)

                await safe_reply(
                    msg_obj,
                    f"⏰ <b>VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI! (ID #{task.id})</b>\n\n"
                    f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                    f"🕒 <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                    f"💬 <b>Xabar matni:</b>\n<i>\"{safe_msg}\"</i>\n\n"
                    f"🧠 <i>Belgilangan vaqtda bot profilingiz nomidan xabarni avtomatik yuboradi.</i>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )
                return True
            except Exception as e:
                logger.error(f"Error creating scheduled task: {e}", exc_info=True)
                await safe_reply(msg_obj, f"❌ Rejalashtirishda xatolik: {html.escape(str(e))}", parse_mode="HTML")
                return True

        @dp.message(Command("cancel"))
        async def cmd_cancel(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return
            current_state = await state.get_state()
            if current_state:
                await state.clear()
                builder = InlineKeyboardBuilder()
                builder.button(text="⏰ Rejadagi vazifalar", callback_data="tasks:refresh")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1)
                await safe_reply(
                    message,
                    "🚫 <b>Faol jarayon bekor qilindi.</b>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )
            else:
                await safe_reply(message, "ℹ️ Hozirda hech qanday faol jarayon yo'q.", parse_mode="HTML")

        @dp.message(Command("schedule"))
        async def cmd_schedule(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return

            raw = (message.text or "").strip()
            parts = raw.split(maxsplit=1)
            text_after_cmd = parts[1].strip() if len(parts) > 1 else ""

            if not text_after_cmd:
                builder = InlineKeyboardBuilder()
                builder.button(text="✍️ Yangi vazifa rejalashtirish", callback_data="schedule:wizard")
                builder.button(text="⏳ Kutilayotgan vazifalar", callback_data="tasks:refresh")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 2)

                return await safe_reply(
                    message,
                    "⏰ <b>VAZIFALARNI REJALASHTIRISH MARKAZI:</b>\n\n"
                    "Quyidagi usullardan biri orqali xabar yuborishni rejalashtirishingiz mumkin:\n\n"
                    "⚡ <b>1. Tezkor format:</b>\n"
                    "• <code>/schedule @username 22:30 Xabar matni</code>\n"
                    "• <code>/schedule @username 15 daqiqadan keyin Xabar matni</code>\n\n"
                    "🧠 <b>2. Tabiiy tilda (AI tushunadi):</b>\n"
                    "• <code>/schedule Ertaga soat 10 da @ali ga 'salom' deb yoz</code>\n"
                    "• <code>/schedule 30 minutdan keyin @umid ga loyiha tayyor deb jo'nat</code>\n\n"
                    "✍️ <b>3. Bosqichma-bosqich qulay usta:</b>\n"
                    "Quyidagi <b>'✍️ Yangi vazifa rejalashtirish'</b> tugmasini bosing.",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )

            handled = await _schedule_and_respond(message, text_after_cmd)
            if not handled:
                # Check if user provided peer + message, but omitted time!
                partial = extract_peer_and_text_without_time(text_after_cmd)
                if partial:
                    target_peer, msg_text = partial
                    await state.update_data(target_peer=target_peer, message_text=msg_text)
                    await state.set_state(ScheduleWizardState.waiting_for_time)

                    builder = InlineKeyboardBuilder()
                    builder.button(text="❌ Bekor qilish", callback_data="wizard:cancel")
                    builder.adjust(1)

                    return await safe_reply(
                        message,
                        f"👤 <b>Kimga:</b> <code>{html.escape(target_peer)}</code>\n"
                        f"💬 <b>Xabar matni:</b> <i>\"{html.escape(msg_text)}\"</i>\n\n"
                        "🕒 <b>Qaysi vaqtda yuborilsin?</b>\n\n"
                        "Iltimos, xabar yuborilishi kerak bo'lgan vaqtni kiriting:\n"
                        "• <code>22:45</code> (Bugun yoki ertaga)\n"
                        "• <code>10 daqiqadan keyin</code>\n"
                        "• <code>Ertaga soat 09:00 da</code>\n\n"
                        "<i>Bekor qilish uchun /cancel yozing yoki tugmani bosing.</i>",
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML",
                    )

                builder = InlineKeyboardBuilder()
                builder.button(text="✍️ Bosqichma-bosqich kiritish", callback_data="schedule:wizard")
                builder.button(text="⏳ Barcha vazifalar", callback_data="tasks:refresh")
                builder.adjust(1, 1)

                await safe_reply(
                    message,
                    "⚠️ <b>Rejalashtirish parametrlarini aniqlab bo'lmadi.</b>\n\n"
                    "Iltimos, vaqt yoki qabul qiluvchini to'g'ri ko'rsating. Masalan:\n"
                    "• <code>/schedule @username 22:30 Matn</code>\n"
                    "• <code>/schedule @username 15 daqiqadan keyin Matn</code>\n"
                    "• <code>/schedule Ertaga soat 10:00 da @ali ga salom deb yoz</code>\n\n"
                    "Yoki quyidagi tugma orqali bosqichma-bosqich kiriting:",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )

        @dp.callback_query(F.data == "schedule:wizard")
        async def handle_schedule_wizard_start(callback: types.CallbackQuery, state: FSMContext):
            if not self._is_admin(callback.from_user.id):
                return
            await state.set_state(ScheduleWizardState.waiting_for_peer)
            await callback.answer()

            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Bekor qilish", callback_data="wizard:cancel")
            builder.adjust(1)

            text = (
                "✍️ <b>YANGI VAZIFA REJALASHTIRISH (1/3)</b>\n\n"
                "👤 <b>Xabar kimga yuboriladi?</b>\n"
                "Foydalanuvchi nomini (<code>@username</code>), telefon raqami yoki ID sini yozing:\n"
                "<i>(Masalan: <code>@sah_60</code> yoki <code>@ali</code>)</i>\n\n"
                "<i>Bekor qilish uchun /cancel yozing yoki tugmani bosing.</i>"
            )
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data == "wizard:cancel")
        async def handle_wizard_cancel_cb(callback: types.CallbackQuery, state: FSMContext):
            if not self._is_admin(callback.from_user.id):
                return
            await state.clear()
            await callback.answer("Bekor qilindi")
            builder = InlineKeyboardBuilder()
            builder.button(text="⏳ Kutilayotgan vazifalar", callback_data="tasks:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(1, 1)
            await callback.message.edit_text(
                "🚫 <b>Vazifa rejalashtirish bekor qilindi.</b>",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )

        @dp.message(ScheduleWizardState.waiting_for_peer)
        async def process_wizard_peer(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return
            raw_peer = (message.text or "").strip()
            if not raw_peer:
                return await safe_reply(message, "⚠️ Iltimos, foydalanuvchi nomini kiriting (masalan: <code>@ali</code>).", parse_mode="HTML")

            target_peer = clean_target_peer(raw_peer)
            await state.update_data(target_peer=target_peer)
            await state.set_state(ScheduleWizardState.waiting_for_time)

            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Bekor qilish", callback_data="wizard:cancel")
            builder.adjust(1)

            text = (
                f"👤 <b>Qabul qiluvchi:</b> <code>{html.escape(target_peer)}</code>\n\n"
                "🕒 <b>2/3-BOSQICH: Qaysi vaqtda yuborilsin?</b>\n\n"
                "Vaqtni quyidagi ko'rinishlardan birida yozing:\n"
                "• <code>22:30</code> (Bugun yoki ertaga)\n"
                "• <code>15 daqiqadan keyin</code>\n"
                "• <code>1 soatdan keyin</code>\n"
                "• <code>Ertaga soat 10:00 da</code>\n"
                "• <code>2026-10-09 14:00</code>"
            )
            await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(ScheduleWizardState.waiting_for_time)
        async def process_wizard_time(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return
            data = await state.get_data()
            target_peer = data.get("target_peer", "@contact")
            existing_msg_text = data.get("message_text")

            time_input = (message.text or "").strip()
            test_phrase = f"{target_peer} {time_input} test_vazifa"
            parsed = await parse_schedule_intent_hybrid(test_phrase, self.ai_engine, settings.TIMEZONE)
            if not parsed or not parsed.is_schedule:
                parsed = await parse_schedule_intent_hybrid(time_input, self.ai_engine, settings.TIMEZONE)

            if not parsed or not parsed.is_schedule:
                builder = InlineKeyboardBuilder()
                builder.button(text="❌ Bekor qilish", callback_data="wizard:cancel")
                builder.adjust(1)
                return await safe_reply(
                    message,
                    "⚠️ <b>Vaqt formatini tushunib bo'lmadi.</b>\n\n"
                    "Iltimos, qaytadan yozing. Masalan:\n"
                    "• <code>22:30</code>\n"
                    "• <code>15 daqiqadan keyin</code>\n"
                    "• <code>Ertaga soat 09:00 da</code>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )

            tz = pytz.timezone(settings.TIMEZONE)
            formatted_human = format_task_time_human(parsed.run_at.isoformat(), tz)

            if existing_msg_text:
                await state.clear()
                try:
                    task = await self.scheduler_service.schedule_message(
                        target_peer=target_peer,
                        message_text=existing_msg_text,
                        run_at=parsed.run_at,
                    )
                    safe_target = html.escape(target_peer)
                    safe_msg = html.escape(existing_msg_text)
                    builder = InlineKeyboardBuilder()
                    builder.button(text=f"🚀 Hozir yuborish #{task.id}", callback_data=f"task_send:{task.id}")
                    builder.button(text=f"❌ Bekor qilish #{task.id}", callback_data=f"task_cancel:{task.id}")
                    builder.button(text="⏳ Barcha vazifalar", callback_data="tasks:refresh")
                    builder.adjust(2, 1)

                    return await safe_reply(
                        message,
                        f"⏰ <b>VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI! (ID #{task.id})</b>\n\n"
                        f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                        f"🕒 <b>Vaqti:</b> <code>{formatted_human}</code>\n"
                        f"💬 <b>Xabar matni:</b>\n<i>\"{safe_msg}\"</i>\n\n"
                        f"🧠 <i>Belgilangan vaqtda bot profilingiz nomidan xabarni avtomatik yuboradi.</i>",
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Error creating wizard scheduled task: {e}", exc_info=True)
                    return await safe_reply(message, f"❌ Rejalashtirishda xatolik: {html.escape(str(e))}", parse_mode="HTML")

            await state.update_data(run_at_iso=parsed.run_at.isoformat(), human_time=formatted_human)
            await state.set_state(ScheduleWizardState.waiting_for_text)

            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Bekor qilish", callback_data="wizard:cancel")
            builder.adjust(1)

            text = (
                f"👤 <b>Kimga:</b> <code>{html.escape(target_peer)}</code>\n"
                f"⏰ <b>Vaqti:</b> <code>{formatted_human}</code>\n\n"
                "💬 <b>3/3-BOSQICH: Xabar matnini kiriting:</b>\n\n"
                "Yuborilishi kerak bo'lgan matnni yozib jo'nating:"
            )
            await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(ScheduleWizardState.waiting_for_text)
        async def process_wizard_text(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return
            msg_text = (message.text or "").strip()
            if not msg_text:
                return await safe_reply(message, "⚠️ Xabar matni bo'sh bo'lishi mumkin emas. Matnni yozing:")

            data = await state.get_data()
            target_peer = data.get("target_peer")
            run_at_iso = data.get("run_at_iso")
            await state.clear()

            tz = pytz.timezone(settings.TIMEZONE)
            run_at = datetime.fromisoformat(run_at_iso)
            if run_at.tzinfo is None:
                run_at = tz.localize(run_at)

            try:
                task = await self.scheduler_service.schedule_message(
                    target_peer=target_peer,
                    message_text=msg_text,
                    run_at=run_at,
                )

                safe_target = html.escape(target_peer)
                safe_msg = html.escape(msg_text)
                formatted_time = format_task_time_human(run_at.isoformat(), tz)

                builder = InlineKeyboardBuilder()
                builder.button(text=f"🚀 Hozir yuborish #{task.id}", callback_data=f"task_send:{task.id}")
                builder.button(text=f"❌ Bekor qilish #{task.id}", callback_data=f"task_cancel:{task.id}")
                builder.button(text="⏳ Barcha vazifalar", callback_data="tasks:refresh")
                builder.adjust(2, 1)

                await safe_reply(
                    message,
                    f"⏰ <b>VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI! (ID #{task.id})</b>\n\n"
                    f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                    f"🕒 <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                    f"💬 <b>Xabar matni:</b>\n<i>\"{safe_msg}\"</i>\n\n"
                    f"🧠 <i>Belgilangan vaqtda bot profilingiz nomidan xabarni avtomatik yuboradi.</i>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Error creating wizard scheduled task: {e}", exc_info=True)
                await safe_reply(message, f"❌ Rejalashtirishda xatolik: {html.escape(str(e))}", parse_mode="HTML")

        async def _render_contacts_list(target_msg: types.Message, is_callback: bool = False):
            """Renders rich contacts list with addressing style badges, autonomy mode badges and instant toggle buttons."""
            contacts = await self.db_manager.list_contacts()
            if not contacts:
                empty_text = (
                    "📋 <b>SAQLANGAN KONTAKTLAR VA MUROJAAT USLUBLARI:</b>\n\n"
                    "<i>Hozircha CRM xotirasida hech qanday kontakt saqlanmagan.</i>\n\n"
                    "💡 <b>Yangi kontakt va uslub belgilash:</b>\n"
                    "• <code>/crm @username siz [ko'rsatma]</code> — SIZLASH (hurmat bilan)\n"
                    "• <code>/crm @username sen [ko'rsatma]</code> — SENSIRASH (yaqin do'st)\n"
                    "• <code>/mode @username auto|draft|mute</code> — Avtonomiya rejimi\n\n"
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
                f"🛡 <i>Xavfsizlik: Bot FAQAT shu ro'yxatdagi insonlargagina avtomatik yoki tasdiq bilan javob beradi. Boshqa barcha xabarlar sizga to'g'ridan-to'g'ri yetkaziladi.</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
            ]
            builder = InlineKeyboardBuilder()

            for c in contacts:
                uname = f"@{c.username}" if c.username else c.full_name
                safe_uname = html.escape(uname)
                target_user = c.username or str(c.user_id)
                style = getattr(c, "addressing_style", "siz") or "siz"
                mode = getattr(c, "mode", "draft") or "draft"
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

                if mode == "auto":
                    mode_badge = "⚡ <b>auto (Avtomatik javob)</b>"
                elif mode == "mute":
                    mode_badge = "🔇 <b>mute (Ovozsiz)</b>"
                else:
                    mode_badge = "📝 <b>draft (Qoralama / Tasdiq)</b>"

                lines.append(
                    f"👤 <b>{safe_uname}</b>\n"
                    f"⚙️ Rejim: {mode_badge}\n"
                    f"🗣 Murojaat: {style_badge}\n"
                    f"🎭 Munosabat: <code>{safe_rel}</code>\n"
                    f"📜 Ko'rsatma: <i>\"{safe_instr}\"</i>\n"
                )
                # Row 1: SIZ/SEN switch and Delete
                builder.button(text=f"{switch_btn} ({safe_uname})", callback_data=f"crm_style:{target_user}:{switch_to}")
                builder.button(text=f"🗑 O'chirish ({safe_uname})", callback_data=f"crm_del:{target_user}")

                # Row 2: Mode quick toggle buttons
                builder.button(
                    text="✅ ⚡ Auto" if mode == "auto" else "⚡ Auto",
                    callback_data=f"crm_mode:{target_user}:auto",
                )
                builder.button(
                    text="✅ 📝 Draft" if mode == "draft" else "📝 Draft",
                    callback_data=f"crm_mode:{target_user}:draft",
                )
                builder.button(
                    text="✅ 🔇 Mute" if mode == "mute" else "🔇 Mute",
                    callback_data=f"crm_mode:{target_user}:mute",
                )

            builder.button(text="🔄 Yangilash", callback_data="crm:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_layout = []
            for _ in contacts:
                adjust_layout.extend([2, 3])
            adjust_layout.append(2)
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

        @dp.message(F.text & ~F.text.startswith("/") & F.text.regexp(r"(?i)(?:^|.*?)@([a-zA-Z0-9_]{3,32})\s+(.+)"))
        async def handle_natural_crm_or_schedule(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            # 1. Check if this is a scheduling request
            handled = await _schedule_and_respond(message, message.text)
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
                builder.button(text="✍️ Yangi vazifa rejalashtirish", callback_data="schedule:wizard")
                builder.button(text="📜 Bajarilganlar tarixi", callback_data="task_hist:open")
                builder.button(text="🔄 Yangilash", callback_data="tasks:refresh")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1, 2)
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

            builder.button(text="✍️ Yangi vazifa qo'shish", callback_data="schedule:wizard")
            builder.button(text="📜 Tarix", callback_data="task_hist:open")
            builder.button(text="🔄 Yangilash", callback_data="tasks:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_pattern = [2] * len(tasks) + [1, 1, 2]
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
            if not self._is_admin(callback.from_user.id):
                return
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
            if not self._is_admin(message.from_user.id):
                return
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
            if not self._is_admin(callback.from_user.id):
                return
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
            if not self._is_admin(callback.from_user.id):
                return
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
                mode="draft",
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
                f"⚙️ <b>Avtonomiya rejimi:</b> 📝 <b>draft (Qoralama / Tasdiq bilan)</b>\n"
                f"🗣 <b>Murojaat uslubi:</b> <code>{style_name}</code>\n"
                f"🛡 <b>Endi bot bu inson bilan keyingi safar avtomatik suhbatlasha oladi!</b>\n\n"
                f"<i>Kelgan oxirgi xabarga javob yuborasizmi?</i>",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
            await callback.answer("Kontakt saqlandi!")

        @dp.callback_query(F.data.startswith("esc_back:"))
        async def handle_esc_back(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
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

            handled = await _schedule_and_respond(message, message.text)
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
            if not self._is_admin(callback.from_user.id):
                return
            action = callback.data.split(":")[1]
            if action == "open":
                await callback.answer("Digest yuklanmoqda...")
                await _execute_digest(callback.message, is_callback=True, force_recent=False)
            elif action == "refresh":
                await callback.answer("Qayta tahlil qilinmoqda...")
                await _execute_digest(callback.message, is_callback=True, force_recent=True)
            elif action == "sources":
                await callback.answer()
                await _render_digest_sources(callback.message, is_callback=True)
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
                builder.button(text="📋 Manbalar ro'yxati", callback_data="digest_sources:list")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(2, 1)
                await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data == "digest_sources:list")
        async def handle_digest_sources_list(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer()
            await _render_digest_sources(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("digest_src_del:"))
        async def handle_digest_source_delete(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            target_id = callback.data.split(":", 1)[1]
            if self.digest_service:
                success, resp_text = await self.digest_service.remove_source(target_id)
                await callback.answer("Manba o'chirildi!" if success else "Topilmadi", show_alert=True)
            else:
                await callback.answer("Xizmat topilmadi")
            await _render_digest_sources(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("menu:"))
        async def handle_menu_callbacks(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            action = callback.data.split(":")[1]
            if action == "main":
                await callback.answer()
                text = (
                    "👑 <b>Salom, Xo'jayin! Antigravity AI Butler Kokpiti:</b>\n\n"
                    "Sizning shaxsiy profilingizni to'liq boshqaruvchi markaz.\n\n"
                    "📌 <b>Asosiy buyruqlar:</b>\n"
                    "• <code>/mode @username auto|draft|mute</code> — Kontakt avtonomiya rejimini belgilash\n"
                    "• <code>/contacts</code> — Saqlangan kontaktlar va ularning rejimlari\n"
                    "• <code>/drafts</code> — Kutilayotgan qoralamalar ro'yxati va tasdiqlash\n"
                    "• <code>/crm @username [rol] [ko'rsatma]</code> — Kontakt uslubini belgilash\n"
                    "• <code>/schedule @username HH:MM Matn</code> — Xabar rejalashtirish\n"
                    "• <code>/tasks</code> — Kutilayotgan rejalashtirilgan xabarlar\n"
                    "• <code>/digest</code> — Kanallar va guruhlardan xulosalar\n"
                    "• <code>/status</code> — Tizim holati"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="📰 Executive Digest", callback_data="digest:open")
                builder.button(text="📋 Kontaktlar", callback_data="menu:contacts")
                builder.button(text="📝 Qoralamalar", callback_data="menu:drafts")
                builder.button(text="⏰ Rejadagi vazifalar", callback_data="menu:tasks")
                builder.button(text="⚡ Tizim holati", callback_data="menu:status")
                builder.adjust(2, 3)
                await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            elif action == "contacts":
                await callback.answer()
                await _render_contacts_list(callback.message, is_callback=True)
            elif action == "drafts":
                await callback.answer()
                await _render_drafts_list(callback.message, is_callback=True)
            elif action == "tasks":
                await callback.answer()
                await _render_tasks_list(callback.message, is_callback=True)
            elif action == "status":
                await callback.answer()
                auto_reply_str = "Yoqilgan" if settings.AUTO_REPLY_ENABLED else "O'chirilgan"
                phone_str = html.escape(settings.TELEGRAM_PHONE_NUMBER or "Faol")
                model_str = html.escape(settings.GEMINI_MODEL)
                tz_str = html.escape(settings.TIMEZONE)

                mode_counts = await self.db_manager.get_mode_counts()
                pending_drafts = await self.db_manager.count_pending_drafts()
                total_contacts = mode_counts.get("total", mode_counts.get("auto", 0) + mode_counts.get("draft", 0) + mode_counts.get("mute", 0))

                text = (
                    "⚡ <b>ANTIGRAVITY BUTLER TIZIM HOLATI:</b>\n\n"
                    f"• <b>AI Modeli:</b> <code>{model_str}</code>\n"
                    f"• <b>Avto-javob holati:</b> <code>{auto_reply_str}</code>\n"
                    f"• <b>Vaqt mintaqasi:</b> <code>{tz_str}</code>\n"
                    f"• <b>Telegram Profil:</b> <code>{phone_str}</code>\n"
                    "• <b>Kontaktlar filtri:</b> 🛡 Faol (Faqat /contacts dagi insonlar)\n"
                    f"• <b>Avtonomiya rejimlari:</b> ⚡ auto: {mode_counts.get('auto', 0)} | 📝 draft: {mode_counts.get('draft', 0)} | 🔇 mute: {mode_counts.get('mute', 0)} (Jami: {total_contacts})\n"
                    f"• <b>Kutilayotgan qoralamalar:</b> <code>{pending_drafts}</code> ta\n"
                    "• <b>Holat:</b> Barcha xizmatlar 100% faol."
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("crm_style:"))
        async def handle_crm_style_toggle(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
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
            if not self._is_admin(callback.from_user.id):
                return
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
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer("Kontaktlar yangilandi")
            await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("crm_mode:"))
        async def handle_crm_mode_toggle(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            parts = callback.data.split(":")
            target_user = parts[1]
            new_mode = parts[2]

            if new_mode not in ("auto", "draft", "mute"):
                return await callback.answer("Noto'g'ri rejim.", show_alert=True)

            if target_user.isdigit():
                contact = await self.db_manager.get_contact_by_id(int(target_user))
                if contact:
                    if contact.mode == new_mode:
                        return await callback.answer("Allaqachon ushbu rejimda.")
                    await self.db_manager.update_contact_mode(contact.user_id, new_mode)
            else:
                contact = await self.db_manager.get_contact_by_username(target_user)
                if contact:
                    if contact.mode == new_mode:
                        return await callback.answer("Allaqachon ushbu rejimda.")
                    await self.db_manager.update_contact_mode_by_username(target_user, new_mode)

            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            mode_labels = {
                "auto": "⚡ auto (Avtomatik)",
                "draft": "📝 draft (Qoralama)",
                "mute": "🔇 mute (Ovozsiz)",
            }
            await callback.answer(f"✅ @{contact.username or target_user} rejimi {mode_labels.get(new_mode, new_mode)} ga o'zgartirildi!")
            await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("task_send:"))
        async def handle_task_send_now(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            task_id = int(callback.data.split(":")[1])
            success = await self.scheduler_service.execute_now(task_id)
            if success:
                await callback.answer(f"🚀 #{task_id} xabar zudlik bilan yuborildi!", show_alert=True)
            else:
                await callback.answer(f"❌ #{task_id} vazifa topilmadi yoki allaqachon bajarilgan.", show_alert=True)
            await _render_tasks_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("task_cancel:"))
        async def handle_task_cancel(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            task_id = int(callback.data.split(":")[1])
            await self.scheduler_service.cancel_task(task_id)
            await callback.answer(f"🛑 #{task_id} vazifa bekor qilindi!", show_alert=True)
            await _render_tasks_list(callback.message, is_callback=True)

        @dp.callback_query(F.data == "task_hist:open")
        async def handle_task_history_open(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer()
            await _render_task_history(callback.message, is_callback=True)

        @dp.callback_query(F.data == "tasks:refresh")
        async def handle_tasks_refresh(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer("Vazifalar ro'yxati yangilandi")
            await _render_tasks_list(callback.message, is_callback=True)

        # ------------------ DRAFTS FLOW (PHASE 2 & 3) ------------------
        async def _render_drafts_list(target_msg: types.Message, is_callback: bool = False):
            """Renders pending unexpired drafts with quick action buttons."""
            await self.db_manager.expire_old_drafts()
            drafts = await self.db_manager.list_pending_drafts(limit=10)

            if not drafts:
                empty_text = (
                    "📝 <b>KUTILAYOTGAN QORALAMALAR:</b>\n\n"
                    "<i>Hozircha tasdiq kutayotgan qoralamalar yo'q.</i>\n\n"
                    "💡 <b>Qoralama qanday hosil bo'ladi?</b>\n"
                    "• <code>draft</code> rejimdagi kontaktlardan xabar kelganda\n"
                    "• <code>auto</code> rejimdagi kontakt xabarida nozik/xavfli mavzu (to'lov, karta, uchrashuv) aniqlanganda"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔄 Yangilash", callback_data="drafts:refresh")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(2)
                if is_callback:
                    await target_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                else:
                    await safe_reply(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            lines = [f"📝 <b>KUTILAYOTGAN QORALAMALAR ({len(drafts)} ta):</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
            builder = InlineKeyboardBuilder()

            for d in drafts:
                contact_str = f"@{d.contact_username}" if d.contact_username else str(d.contact_user_id or "Noma'lum")
                safe_contact = html.escape(contact_str)
                safe_incoming = html.escape(d.incoming_text[:150] + ("..." if len(d.incoming_text) > 150 else ""))
                safe_reply_text = html.escape(d.proposed_reply[:200] + ("..." if len(d.proposed_reply) > 200 else ""))

                sens_badge = ""
                if d.is_sensitive:
                    sens_reason = html.escape(d.sensitive_reason or "Nozik mavzu")
                    sens_badge = f"\n⚠️ <b>Muhim - tasdiq kerak!</b> (Sabab: <i>{sens_reason}</i>)"

                lines.append(
                    f"📌 <b>Qoralama #{d.id}</b>{sens_badge}\n"
                    f"👤 <b>Kimdan:</b> <code>{safe_contact}</code>\n"
                    f"📩 <b>Kelgan xabar:</b> <i>\"{safe_incoming}\"</i>\n"
                    f"💡 <b>Tavsiya etilgan javob:</b>\n<code>{safe_reply_text}</code>\n"
                )

                builder.button(text=f"✅ Yuborish #{d.id}", callback_data=f"draft_send:{d.id}")
                builder.button(text=f"✍️ Tahrirlash #{d.id}", callback_data=f"draft_edit:{d.id}")
                builder.button(text=f"❌ Rad #{d.id}", callback_data=f"draft_reject:{d.id}")

            builder.button(text="🔄 Yangilash", callback_data="drafts:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_layout = [3] * len(drafts) + [2]
            builder.adjust(*adjust_layout)

            content = "\n".join(lines)
            if is_callback:
                await target_msg.edit_text(content, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply_or_send_long(target_msg, content, reply_markup=builder.as_markup())

        @dp.message(Command("drafts"))
        async def cmd_drafts(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_drafts_list(message, is_callback=False)

        @dp.callback_query(F.data == "drafts:refresh")
        async def handle_drafts_refresh(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer("Qoralamalar yangilandi")
            await _render_drafts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("draft_reject:"))
        async def handle_draft_reject(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            draft_id = int(callback.data.split(":")[1])
            success = await self.db_manager.resolve_draft_atomic(draft_id, "rejected")
            if not success:
                return await callback.answer("Allaqachon bajarilgan.", show_alert=True)
            await callback.answer(f"❌ #{draft_id} qoralama rad etildi!", show_alert=True)
            await _render_drafts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("draft_send:"))
        async def handle_draft_send(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            draft_id = int(callback.data.split(":")[1])
            draft = await self.db_manager.get_draft_by_id(draft_id)
            if not draft or not draft.is_pending:
                return await callback.answer("Allaqachon bajarilgan.", show_alert=True)

            # Expiration check (24 hours)
            now_iso = datetime.now().isoformat()
            if draft.expires_at <= now_iso:
                await self.db_manager.resolve_draft_atomic(draft.id, "expired")
                return await callback.answer("⚠️ Qoralama muddati tugagan (24 soat). Yuborib bo'lmaydi.", show_alert=True)

            # Check if owner already replied manually
            if self._manual_reply_check_fn and (draft.contact_user_id or draft.contact_username):
                target_peer = draft.contact_user_id or f"@{draft.contact_username}"
                replied_manually = await self._manual_reply_check_fn(target_peer, draft.source_message_id)
                if replied_manually:
                    await self.db_manager.resolve_draft_atomic(draft.id, "superseded")
                    try:
                        await callback.message.edit_text(
                            f"ℹ️ <b>Siz allaqachon bu kontaktga o'zingiz qo'lda yozgansiz!</b>\n"
                            f"Qoralama #{draft.id} avtomatik bekor qilindi (status: superseded).",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    return await callback.answer("Siz allaqachon javob bergansiz (superseded).", show_alert=True)

            success = await self.db_manager.resolve_draft_atomic(draft_id, "sent")
            if not success:
                return await callback.answer("Allaqachon bajarilgan.", show_alert=True)

            if self._userbot_send_fn:
                peer = f"@{draft.contact_username}" if draft.contact_username else str(draft.contact_user_id or "")
                if peer:
                    await self._userbot_send_fn(peer, draft.proposed_reply)

            await callback.answer(f"✅ #{draft_id} qoralama yuborildi!", show_alert=True)
            try:
                safe_reply_prev = html.escape(draft.proposed_reply)
                await callback.message.edit_text(
                    f"✅ <b>Qoralama #{draft_id} profilingiz nomidan yuborildi!</b>\n\n"
                    f"💬 <b>Matn:</b> <i>\"{safe_reply_prev}\"</i>",
                    parse_mode="HTML",
                )
            except Exception:
                await _render_drafts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("draft_edit:"))
        async def handle_draft_edit(callback: types.CallbackQuery, state: FSMContext):
            if not self._is_admin(callback.from_user.id):
                return
            draft_id = int(callback.data.split(":")[1])
            draft = await self.db_manager.get_draft_by_id(draft_id)
            if not draft or not draft.is_pending:
                return await callback.answer("Allaqachon bajarilgan yoki muddati o'tgan.", show_alert=True)

            now_iso = datetime.now().isoformat()
            if draft.expires_at <= now_iso:
                await self.db_manager.resolve_draft_atomic(draft.id, "expired")
                return await callback.answer("⚠️ Qoralama muddati tugagan (24 soat).", show_alert=True)

            await state.set_state(ReplyState.waiting_for_draft_edit)
            await state.update_data(draft_id=draft_id, created_ts=datetime.now().timestamp())
            await callback.message.reply(
                f"✍️ <b>Qoralama #{draft_id} uchun yangi javob matnini yozing:</b>\n\n"
                f"<i>(Bekor qilish uchun <code>/cancel</code> deb yozing; vaqt limiti 5 daqiqa)</i>",
                parse_mode="HTML",
            )
            await callback.answer()

        @dp.message(ReplyState.waiting_for_draft_edit)
        async def process_draft_edit_message(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return

            if message.text and message.text.strip().lower() == "/cancel":
                await state.clear()
                return await safe_reply(message, "🛑 Qoralamani tahrirlash bekor qilindi.")

            data = await state.get_data()
            created_ts = data.get("created_ts", 0)
            if datetime.now().timestamp() - created_ts > 300:
                await state.clear()
                return await safe_reply(message, "⚠️ Tahrirlash vaqti tugadi (5 daqiqa). Qaytadan urinib ko'ring.")

            draft_id = data.get("draft_id")
            new_text = message.text.strip() if message.text else ""
            if not new_text:
                return await safe_reply(message, "⚠️ Xabar matni bo'sh bo'lishi mumkin emas.")

            draft = await self.db_manager.get_draft_by_id(draft_id)
            if not draft or not draft.is_pending:
                await state.clear()
                return await safe_reply(message, "⚠️ Qoralama allaqachon ko'rib chiqilgan yoki eskirgan.")

            now_iso = datetime.now().isoformat()
            if draft.expires_at <= now_iso:
                await self.db_manager.resolve_draft_atomic(draft.id, "expired")
                await state.clear()
                return await safe_reply(message, "⚠️ Qoralama muddati tugagan (24 soat).")

            # Check if owner already replied manually before sending edited reply
            if self._manual_reply_check_fn and (draft.contact_user_id or draft.contact_username):
                target_peer = draft.contact_user_id or f"@{draft.contact_username}"
                replied_manually = await self._manual_reply_check_fn(target_peer, draft.source_message_id)
                if replied_manually:
                    await self.db_manager.resolve_draft_atomic(draft.id, "superseded")
                    await state.clear()
                    return await safe_reply(
                        message,
                        f"ℹ️ <b>Siz allaqachon bu kontaktga o'zingiz qo'lda yozgansiz!</b>\n"
                        f"Qoralama #{draft.id} bekor qilindi (status: superseded).",
                        parse_mode="HTML",
                    )

            success = await self.db_manager.resolve_draft_atomic(draft_id, "edited_sent", edited_reply=new_text)
            await state.clear()
            if not success:
                return await safe_reply(message, "⚠️ Qoralama allaqachon ko'rib chiqilgan yoki eskirgan.")

            if self._userbot_send_fn:
                peer = f"@{draft.contact_username}" if draft.contact_username else str(draft.contact_user_id or "")
                if peer:
                    await self._userbot_send_fn(peer, new_text)

            safe_text = html.escape(new_text)
            await safe_reply(
                message,
                f"✅ <b>Tahrirlangan javob muvaffaqiyatli yuborildi! (Qoralama #{draft_id})</b>\n\n"
                f"💬 <b>Matn:</b> <i>\"{safe_text}\"</i>",
                parse_mode="HTML",
            )

    async def send_draft_for_approval(self, draft: Draft):
        """Sends interactive draft approval request to Boss with inline action buttons."""
        if not self.bot or not self.admin_id:
            logger.info(f"Draft #{draft.id} approval notification: {draft.proposed_reply}")
            return

        contact_str = f"@{draft.contact_username}" if draft.contact_username else str(draft.contact_user_id or "Noma'lum")
        safe_contact = html.escape(contact_str)
        safe_incoming = html.escape(draft.incoming_text)
        safe_reply_text = html.escape(draft.proposed_reply)

        if draft.is_sensitive:
            sens_reason = html.escape(draft.sensitive_reason or "Nozik mavzu aniqlandi")
            header = "⚠️ <b>MUHIM - TASDIQ KERAK!</b>"
            notice = f"🔒 <b>Sabab:</b> <i>{sens_reason}</i>\n<i>Xavfsizlik qoidasiga ko'ra, xabar avtomatik yuborilmadi.</i>"
        else:
            header = "📝 <b>YANGI QORALAMA JAVOB TAYYORLANDI</b>"
            notice = "<i>Ushbu kontakt <code>draft</code> rejimida. Javob sizning tasdig'ingizdan so'ng yuboriladi.</i>"

        text = (
            f"{header}\n\n"
            f"👤 <b>Kontakt:</b> <code>{safe_contact}</code>\n"
            f"📩 <b>Kelgan xabar:</b> <i>\"{safe_incoming}\"</i>\n\n"
            f"💡 <b>Tavsiya etilgan AI javobi:</b>\n"
            f"<code>{safe_reply_text}</code>\n\n"
            f"{notice}\n\n"
            "Nima qilamiz, Xo'jayin?"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Yuborish", callback_data=f"draft_send:{draft.id}")
        builder.button(text="✍️ Tahrirlash", callback_data=f"draft_edit:{draft.id}")
        builder.button(text="❌ Rad etish", callback_data=f"draft_reject:{draft.id}")
        builder.adjust(3)

        await safe_send(
            bot=self.bot,
            chat_id=self.admin_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )



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
