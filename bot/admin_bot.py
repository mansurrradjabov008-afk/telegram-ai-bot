"""
Admin Cockpit Bot implementation using aiogram 3.x.
Provides an interactive command center and inline keyboard escalation alerts for the Boss.
Bulletproof HTML escaping and fail-safe error handling.
"""

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any, List
import pytz
import time

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from config import settings
from database.crud import DatabaseManager
from database.models import Draft
from services.ai_engine import AIEngine, extract_clean_first_name, sanitize_outreach_constraints
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
from services.memory_categories import normalize_category, CANONICAL_CATEGORIES, CATEGORY_SYNONYMS
from services.memory_injection import is_forbidden_topic

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


def get_main_reply_keyboard() -> types.ReplyKeyboardRemove:
    """Returns ReplyKeyboardRemove to completely eliminate persistent reply keyboard buttons."""
    return types.ReplyKeyboardRemove(remove_keyboard=True)


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


def split_telegram_text(text: str, max_chars: int = 3800) -> List[str]:
    """Splits text safely by lines or character boundaries into chunks under max_chars."""
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    chunks = []
    curr = ""
    for line in text.split("\n"):
        if len(line) > max_chars:
            if curr:
                chunks.append(curr.rstrip("\n"))
                curr = ""
            for i in range(0, len(line), max_chars):
                chunks.append(line[i : i + max_chars])
        elif len(curr) + len(line) + 1 > max_chars:
            chunks.append(curr.rstrip("\n"))
            curr = line + "\n"
        else:
            curr += line + "\n"
    if curr:
        chunks.append(curr.rstrip("\n"))
    return [c for c in chunks if c.strip()] or [text[:max_chars]]


async def safe_reply(
    message: types.Message,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "HTML",
) -> Optional[types.Message]:
    """Sends a reply safely; if text is long, splits into chunks; if formatting fails, falls back to plain text."""
    if not message:
        return None
    chunks = split_telegram_text(text, max_chars=3800)
    first_msg = None
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        if idx == 0:
            try:
                first_msg = await message.reply(chunk, reply_markup=markup, parse_mode=parse_mode)
            except TelegramBadRequest as e:
                logger.warning(f"Telegram parse error on reply ({type(e).__name__}), sending plain text...")
                clean_chunk = (
                    chunk.replace("<b>", "")
                    .replace("</b>", "")
                    .replace("<code>", "")
                    .replace("</code>", "")
                    .replace("<i>", "")
                    .replace("</i>", "")
                )
                try:
                    first_msg = await message.reply(clean_chunk, reply_markup=markup, parse_mode=None)
                except Exception as e2:
                    logger.error(f"Fallback reply also failed: {type(e2).__name__}")
            except Exception as e:
                logger.error(f"Unexpected error in safe_reply: {type(e).__name__}")
        else:
            bot = message.bot
            if bot:
                await safe_send(bot, message.chat.id, chunk, reply_markup=markup, parse_mode=parse_mode)
    return first_msg


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "HTML",
) -> Optional[types.Message]:
    """Sends a message safely; if text is long, splits into chunks; if formatting fails, falls back to plain text."""
    if not bot:
        return None
    chunks = split_telegram_text(text, max_chars=3800)
    last_msg = None
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        try:
            last_msg = await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup, parse_mode=parse_mode)
        except TelegramBadRequest as e:
            logger.warning(f"Telegram parse error on send ({type(e).__name__}), sending plain text...")
            clean_chunk = (
                chunk.replace("<b>", "")
                .replace("</b>", "")
                .replace("<code>", "")
                .replace("</code>", "")
                .replace("<i>", "")
                .replace("</i>", "")
            )
            try:
                last_msg = await bot.send_message(chat_id=chat_id, text=clean_chunk, reply_markup=markup, parse_mode=None)
            except Exception as e2:
                logger.error(f"Fallback send also failed: {type(e2).__name__}")
        except Exception as e:
            logger.error(f"Unexpected error in safe_send: {type(e).__name__}")
    return last_msg


def split_message_chunks(text: str, max_chars: int = 4000) -> List[str]:
    """Splits long text across item/newline boundaries under max_chars."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


async def safe_reply_or_send_long(
    message: types.Message,
    text: str,
    reply_markup=None,
):
    """Sends text splitting nicely across chunks if needed under 4096 chars."""
    chunks = split_message_chunks(text, max_chars=4000)
    last_msg = None
    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        markup = reply_markup if is_last else None
        last_msg = await safe_reply(message, chunk, reply_markup=markup, parse_mode="HTML")
    return last_msg


async def safe_edit(
    message: types.Message,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "HTML",
) -> Optional[types.Message]:
    """Edits message safely; catches 'message is not modified', parse errors, and falls back to plain text."""
    if not message:
        return None
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return message
        logger.warning(f"Telegram parse/edit error ({type(e).__name__}), falling back to plain text...")
        clean_text = (
            text.replace("<b>", "")
            .replace("</b>", "")
            .replace("<code>", "")
            .replace("</code>", "")
            .replace("<i>", "")
            .replace("</i>", "")
        )
        try:
            return await message.edit_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        except Exception:
            return None
    except Exception as e:
        logger.error(f"Unexpected error in safe_edit: {type(e).__name__}")
        return None




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


def normalize_uzbek_username_token(raw_token: str) -> str:
    """
    Cleans Uzbek grammatical case suffixes attached to @username:
    e.g. '@gulzodaxursandovadan' -> 'gulzodaxursandova'
         '@umid_namozovga' -> 'umid_namozov'
         '@sah_60ni' -> 'sah_60'
    """
    clean = raw_token.lstrip("@").strip()
    suffixes = [
        "dagi", "bilan", "ning", "dan", "den", "ga", "ka", "qa", "ni", "da", "la"
    ]
    for sfx in suffixes:
        if clean.lower().endswith(sfx) and len(clean) - len(sfx) >= 3:
            return clean[:-len(sfx)]
    return clean


MODE_ALIASES: Dict[str, str] = {
    "auto": "auto",
    "avto": "auto",
    "avtomatik": "auto",
    "automatic": "auto",
    "draft": "draft",
    "qoralama": "draft",
    "tasdiq": "draft",
    "tasdiqlash": "draft",
    "tekshir": "draft",
    "mute": "mute",
    "ovozsiz": "mute",
    "ochir": "mute",
    "o'chir": "mute",
    "jim": "mute",
    "ignore": "mute",
}


def parse_autonomy_mode(raw: Optional[str]) -> Optional[str]:
    """
    Parses and normalizes autonomy mode string with support for Uzbek and English aliases.
    Returns 'auto', 'draft', or 'mute', or None if unrecognized.
    """
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return MODE_ALIASES.get(cleaned)


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
        memory_service: Optional[Any] = None,
        token: Optional[str] = None,
        admin_id: Optional[int] = None,
    ):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self.scheduler_service = scheduler_service
        self.digest_service = digest_service
        self.memory_service = memory_service
        self.token = token or settings.ADMIN_BOT_TOKEN
        self.admin_id = admin_id if admin_id is not None else settings.owner_id_effective

        self.bot = Bot(token=self.token) if self.token and not self.token.startswith("1234567890:") else None
        self.dp = Dispatcher(storage=MemoryStorage())
        self._userbot_send_fn: Optional[Callable] = None
        self._manual_reply_check_fn: Optional[Callable] = None
        self._pending_escalations: Dict[str, Dict[str, Any]] = {}
        self._pending_outreach: Optional[Dict[str, Any]] = None

        self._setup_handlers()

    def set_memory_service(self, memory_service: Any):
        """Sets Contact Memory service instance."""
        self.memory_service = memory_service

    def set_userbot_sender(self, fn: Callable):
        self._userbot_send_fn = fn

    def set_manual_reply_checker(self, fn: Callable):
        self._manual_reply_check_fn = fn


    async def setup_bot_commands(self):
        """Registers clean, descriptive Telegram bot commands with BotFather/Telegram API."""
        if not self.bot:
            return
        commands = [
            types.BotCommand(command="start", description="👑 Boshqaruv paneli (Cockpit)"),
            types.BotCommand(command="mode", description="⚙️ Kontaktlar avtonomiya rejimlarini boshqarish"),
            types.BotCommand(command="contacts", description="👥 Saqlangan kontaktlar ro'yxati"),
            types.BotCommand(command="drafts", description="📝 Kutilayotgan qoralamalar va tasdiqlash"),
            types.BotCommand(command="crm", description="👤 Kontakt munosabati va ko'rsatma"),
            types.BotCommand(command="status", description="📊 Tizim holati va avtonomiya hisoboti"),
            types.BotCommand(command="memory", description="🧠 Kontakt xotirasi boshqaruvi"),
            types.BotCommand(command="digest", description="📰 Executive yangiliklar xulosasi"),
            types.BotCommand(command="tasks", description="⏰ Rejalashtirilgan vazifalar"),
        ]
        try:
            await self.bot.set_my_commands(commands)
            logger.info("✅ Telegram bot commands menu registered successfully.")
        except Exception as e:
            logger.warning(f"Could not register Telegram bot commands: {type(e).__name__}")

    def _is_admin(self, user_id: int) -> bool:
        """Owner-only check comparing numeric Telegram ID with owner ID. Returns False if unconfigured or mismatched."""
        effective_id = self.admin_id if self.admin_id is not None else settings.owner_id_effective
        if not effective_id:
            return False
        return user_id == effective_id

    async def _execute_draft_edit_and_send(
        self,
        message: types.Message,
        draft_id: int,
        new_text: str,
        state: Optional[FSMContext] = None,
        card_chat_id: Optional[int] = None,
        card_msg_id: Optional[int] = None,
    ) -> bool:
        clean_text = new_text.strip() if new_text else ""
        if not clean_text:
            await safe_reply(message, "⚠️ Xabar matni bo'sh bo'lishi mumkin emas.")
            return False

        draft = await self.db_manager.get_draft_by_id(draft_id)
        if not draft:
            if state:
                await state.clear()
            await safe_reply(message, f"⚠️ Qoralama #{draft_id} topilmadi.")
            return False

        if not draft.is_pending:
            if state:
                await state.clear()
            status_lbl = getattr(draft, "status", "noma'lum")
            await safe_reply(
                message,
                f"ℹ️ <b>Qoralama #{draft_id} allaqachon ko'rib chiqilgan!</b>\nHolat: <code>{html.escape(str(status_lbl))}</code>",
                parse_mode="HTML",
            )
            return False

        now_iso = datetime.now().isoformat()
        if draft.expires_at <= now_iso:
            await self.db_manager.resolve_draft_atomic(draft.id, "expired")
            if state:
                await state.clear()
            await safe_reply(message, f"⚠️ Qoralama #{draft_id} muddati tugagan (24 soat).")
            return False

        # Check if owner already replied manually before sending edited reply
        if self._manual_reply_check_fn and (draft.contact_user_id or draft.contact_username):
            target_peer = draft.contact_user_id or f"@{draft.contact_username}"
            replied_manually = await self._manual_reply_check_fn(target_peer, draft.source_message_id)
            if replied_manually:
                await self.db_manager.resolve_draft_atomic(draft.id, "superseded")
                if state:
                    await state.clear()
                await safe_reply(
                    message,
                    f"ℹ️ <b>Siz allaqachon bu kontaktga o'zingiz qo'lda yozgansiz!</b>\n"
                    f"Qoralama #{draft.id} bekor qilindi (status: superseded).",
                    parse_mode="HTML",
                )
                return False

        success = await self.db_manager.resolve_draft_atomic(draft_id, "edited_sent", edited_reply=clean_text)
        if state:
            await state.clear()
        if not success:
            await safe_reply(message, f"⚠️ Qoralama #{draft_id} allaqachon ko'rib chiqilgan.")
            return False

        sent_msg = None
        send_error = None
        if self._userbot_send_fn:
            peers_to_try = []
            if draft.contact_username:
                peers_to_try.append(f"@{draft.contact_username}")
            if draft.contact_user_id:
                peers_to_try.append(str(draft.contact_user_id))

            for peer in peers_to_try:
                try:
                    sent_msg = await self._userbot_send_fn(peer, clean_text)
                    if sent_msg:
                        send_error = None
                        break
                except Exception as e:
                    logger.warning(f"Userbot send failed for peer {peer} on draft #{draft_id}: {type(e).__name__}")
                    send_error = e

            if sent_msg and self.memory_service:
                target_user_id = draft.contact_user_id
                if not target_user_id and draft.contact_username:
                    c = await self.db_manager.get_contact_by_username(draft.contact_username)
                    target_user_id = c.user_id if c else None
                if target_user_id:
                    try:
                        await self.memory_service.log_message(
                            contact_user_id=target_user_id,
                            tg_message_id=getattr(sent_msg, "id", 0),
                            sender="owner_edited",
                            text=clean_text,
                            username=draft.contact_username,
                        )
                    except Exception as e:
                        logger.error(f"Failed to log message in memory_service: {type(e).__name__}")

        # Update original approval card if identifiers are provided
        if card_chat_id and card_msg_id and self.bot:
            try:
                safe_reply_prev = html.escape(clean_text)
                await self.bot.edit_message_text(
                    f"✅ <b>Qoralama #{draft_id} tahrirlanib profilingiz nomidan yuborildi!</b>\n\n"
                    f"💬 <b>Yuborilgan javob:</b> <i>\"{safe_reply_prev}\"</i>",
                    chat_id=card_chat_id,
                    message_id=card_msg_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass

        safe_text = html.escape(clean_text)
        target_info = f"@{draft.contact_username}" if draft.contact_username else f"ID: {draft.contact_user_id}"
        if send_error and not sent_msg:
            await safe_reply(
                message,
                f"⚠️ <b>Qoralama saqlandi, ammo Telegram orqali yuborishda xatolik yuz berdi:</b>\n"
                f"<code>{html.escape(str(send_error))}</code>",
                parse_mode="HTML",
            )
        else:
            await safe_reply(
                message,
                f"✅ <b>Tahrirlangan javob muvaffaqiyatli yuborildi! (Qoralama #{draft_id})</b>\n\n"
                f"👤 <b>Kimga:</b> <code>{html.escape(target_info)}</code>\n"
                f"💬 <b>Matn:</b> <i>\"{safe_text}\"</i>",
                parse_mode="HTML",
            )
        return True

    def _setup_handlers(self):
        dp = self.dp

        async def _render_dashboard(target_msg: types.Message, is_callback: bool = False, remove_keyboard: bool = False):
            """Renders the executive Antigravity Cockpit dashboard with live metrics (pure conversational / buttonless)."""
            mode_counts = await self.db_manager.get_mode_counts()
            pending_drafts = await self.db_manager.count_pending_drafts()
            pending_tasks = len(await self.db_manager.get_pending_tasks())
            mem_stats = await self.db_manager.get_overall_mem_stats()

            total_contacts = mode_counts.get("total", mode_counts.get("auto", 0) + mode_counts.get("draft", 0) + mode_counts.get("mute", 0))
            auto_count = mode_counts.get("auto", 0)
            draft_count = mode_counts.get("draft", 0)
            mute_count = mode_counts.get("mute", 0)
            facts_count = mem_stats.get("facts", 0)
            contacts_enabled = mem_stats.get("contacts_enabled", 0)

            text = (
                "👑 <b>ANTIGRAVITY EXECUTIVE COCKPIT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ <b>Tizim:</b> 🟢 100% To'liq Avtomatlashtirilgan  •  🤖 <b>AI:</b> Gemini 2.5 Flash\n"
                "🛡️ <b>Himoya:</b> 100% Xavfsiz  •  👑 <b>Murojaat:</b> Xo'jayin\n\n"
                "📊 <b>JONLI KO'RSATKICHLAR:</b>\n"
                f"├ 👥 Kontaktlar (CRM): <b>{total_contacts} ta</b> (100% ⚡ auto)\n"
                f"├ 📝 Kutilayotgan qoralamalar: <b>{pending_drafts} ta</b>\n"
                f"├ ⏰ Rejalashtirilgan vazifalar: <b>{pending_tasks} ta</b>\n"
                f"└ 🧠 Faol xotira faktlari: <b>{facts_count} ta</b> ({contacts_enabled} kontakt)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🤖 <b>To'liq avtonom va suhbatdosh boshqaruv:</b>\n"
                "Bot barcha kontaktlar bilan to'liq avtomatik muloqot qiladi.\n"
                "Barcha amallarni oddiy xabar sifatida yozishingiz mumkin:\n"
                "• <i>\"Mansurjonni senla\"</i> yoki <i>\"Alini sizla\"</i> — murojaat uslubi\n"
                "• <i>\"Ertaga soat 10 da Aliga salom deb yoz\"</i> — reja qo'shish\n"
                "• <i>\"Sherzod haqida ma'lumot\"</i> — xotira ma'lumotlari\n"
                "• <i>\"Dayjest\"</i> — so'nggi yangiliklar va dayjest\n"
                "• <i>\"Hammasini yubor\"</i> — barcha qoralamalarni tasdiqlash"
            )

            reply_markup = types.ReplyKeyboardRemove(remove_keyboard=True) if remove_keyboard else None

            if is_callback:
                await safe_edit(target_msg, text, reply_markup=None, parse_mode="HTML")
            else:
                await safe_reply(target_msg, text, reply_markup=reply_markup, parse_mode="HTML")

        @dp.message(CommandStart())
        async def cmd_start(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            await _render_dashboard(message, is_callback=False, remove_keyboard=True)

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
            mem_stats = await self.db_manager.get_overall_mem_stats()

            status_text = (
                "⚡ <b>ANTIGRAVITY BUTLER — TIZIM HOLATI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🟢 <b>Holat:</b> Barcha xizmatlar 100% faol va barqaror\n"
                f"🤖 <b>AI Modeli:</b> <code>{model_str}</code>\n"
                f"⚡ <b>Avto-javob:</b> <code>{auto_reply_str}</code>\n"
                f"⏰ <b>Vaqt mintaqasi:</b> <code>{tz_str}</code>\n"
                f"📱 <b>Telegram Profil:</b> <code>{phone_str}</code>\n"
                "🛡️ <b>Kontaktlar filtri:</b> 🟢 Faol (Qat'iy izolyatsiya)\n\n"
                "📊 <b>AVTONOMIYA VA MA'LUMOTLAR:</b>\n"
                f"├ 👥 Jami kontaktlar: <b>{total_contacts} ta</b>\n"
                f"│  ├ ⚡ Auto: <b>{mode_counts.get('auto', 0)} ta</b>\n"
                f"│  ├ 📝 Draft: <b>{mode_counts.get('draft', 0)} ta</b>\n"
                f"│  └ 🔇 Mute: <b>{mode_counts.get('mute', 0)} ta</b>\n"
                f"├ 📝 Kutilayotgan qoralamalar: <b>{pending_drafts} ta</b>\n"
                f"└ 🧠 Xotira faktlari: <b>{mem_stats.get('facts', 0)} ta</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="🔄 Yangilash", callback_data="menu:status")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(2)
            await safe_reply(message, status_text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("restart"))
        async def cmd_restart(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            try:
                await safe_reply(
                    message,
                    "🔄 <b>Antigravity Butler qayta yuklanmoqda...</b>\n\n"
                    "<i>Watchdog jarayoni 5 soniya ichida botni eng so'nggi yangilangan kod bilan qayta ishga tushiradi.</i>",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Restart acknowledgement failed: {type(e).__name__}")
            finally:
                import os
                os._exit(0)



        async def _render_digest_sources(target_msg: types.Message, is_callback: bool = False):
            if not self.digest_service:
                text = "❌ <b>Digest xizmati ulanmagan.</b>"
                if is_callback:
                    return await safe_edit(target_msg, text, parse_mode="HTML")
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
                    return await safe_edit(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
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
                await safe_edit(target_msg, content, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, content, reply_markup=builder.as_markup(), parse_mode="HTML")

        async def _execute_digest(
            target_msg: types.Message,
            is_callback: bool = False,
            force_recent: bool = False,
            topic_filter: Optional[str] = None,
        ):
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
                        active_msg = await safe_reply(target_msg, loading_text, parse_mode="HTML") or target_msg
                else:
                    active_msg = await safe_reply(target_msg, loading_text, parse_mode="HTML") or target_msg

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
                    logger.error(f"Digest generation failed: {type(e).__name__}")
                    err_text = f"❌ <b>Digest tayyorlashda xatolik yuz berdi:</b>\n<code>{html.escape(type(e).__name__)}</code>"
                    if is_callback:
                        await active_msg.edit_text(err_text, parse_mode="HTML")
                    else:
                        await safe_reply(active_msg, err_text, parse_mode="HTML")
                return

            # Smart Digest V2 execution
            loading_text = "⏳ <i>Smart Digest V2 tayyorlanmoqda...</i>"
            if is_callback:
                try:
                    await target_msg.edit_text(loading_text, parse_mode="HTML")
                    active_msg = target_msg
                except Exception:
                    active_msg = await safe_reply(target_msg, loading_text, parse_mode="HTML") or target_msg
            else:
                active_msg = await safe_reply(target_msg, loading_text, parse_mode="HTML") or target_msg

            try:
                res = await self.digest_service.run_smart_digest(trigger="manual", topic_filter=topic_filter)
                status = res.get("status")

                if status == "in_progress":
                    progress_text = "⚠️ <b>Smart Digest V2 hozirda tayyorlanmoqda!</b> Iltimos, jarayon tugashini kuting."
                    builder = InlineKeyboardBuilder()
                    builder.button(text="🔄 Qayta tekshirish", callback_data="digest:open")
                    builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                    builder.adjust(1, 1)
                    if is_callback:
                        await active_msg.edit_text(progress_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    else:
                        await safe_reply(active_msg, progress_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    return

                if status == "no_sources":
                    messages = res.get("messages", ["ℹ️ Kuzatiladigan manbalar mavjud emas!"])
                    builder = InlineKeyboardBuilder()
                    builder.button(text="📋 Manbalar ro'yxati", callback_data="digest_sources:list")
                    builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                    builder.adjust(1, 1)
                    if is_callback:
                        await active_msg.edit_text(messages[0], reply_markup=builder.as_markup(), parse_mode="HTML")
                    else:
                        await safe_reply(active_msg, messages[0], reply_markup=builder.as_markup(), parse_mode="HTML")
                    return

                if status == "error":
                    err_msg = res.get("messages", ["❌ Noma'lum xatolik yuz berdi."])[0]
                    builder = InlineKeyboardBuilder()
                    builder.button(text="🔄 Qayta urinish", callback_data="digest:refresh")
                    builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                    builder.adjust(1, 1)
                    if is_callback:
                        await active_msg.edit_text(err_msg, reply_markup=builder.as_markup(), parse_mode="HTML")
                    else:
                        await safe_reply(active_msg, err_msg, reply_markup=builder.as_markup(), parse_mode="HTML")
                    return

                # Completed: messages are pre-split under 4000 characters
                messages = res.get("messages", [])
                if not messages:
                    messages = ["ℹ️ <b>Hozircha tahlil uchun ma'lumot topilmadi.</b>"]

                builder = InlineKeyboardBuilder()
                builder.button(text="🔄 Yangilash", callback_data="digest:refresh")
                builder.button(text="🔍 Debug", callback_data="digest:debug")
                builder.button(text="📋 Manbalar", callback_data="digest_sources:list")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(2, 2)

                for idx, m in enumerate(messages):
                    markup = builder.as_markup() if idx == len(messages) - 1 else None
                    if idx == 0 and is_callback:
                        try:
                            await active_msg.edit_text(m, reply_markup=markup, parse_mode="HTML")
                            continue
                        except Exception:
                            pass
                    if idx == 0 and not is_callback:
                        await safe_reply(active_msg, m, reply_markup=markup, parse_mode="HTML")
                    elif idx > 0:
                        if self.bot and getattr(active_msg, "chat", None):
                            await safe_send(self.bot, active_msg.chat.id, m, reply_markup=markup, parse_mode="HTML")
                        else:
                            await safe_reply(active_msg, m, reply_markup=markup, parse_mode="HTML")

            except Exception as e:
                logger.error(f"Smart Digest V2 execution failed: {type(e).__name__}")
                err_text = f"❌ <b>Digest tayyorlashda xatolik yuz berdi:</b>\n<code>{html.escape(type(e).__name__)}</code>"
                if is_callback:
                    await active_msg.edit_text(err_text, parse_mode="HTML")
                else:
                    await safe_reply(active_msg, err_text, parse_mode="HTML")

        async def _render_digest_debug(
            target_msg: types.Message,
            page: int = 0,
            is_callback: bool = False,
        ):
            if not self.digest_service:
                return await safe_reply(target_msg, "❌ <b>Digest xizmati ulanmagan.</b>", parse_mode="HTML")

            page_size = 10
            posts, total = await self.db_manager.get_excluded_digest_posts(offset=page * page_size, limit=page_size)
            text = self.digest_service.formatter.format_debug_posts_page(
                posts=posts,
                page=page,
                total_count=total,
                page_size=page_size,
            )

            total_pages = max(1, (total + page_size - 1) // page_size)
            builder = InlineKeyboardBuilder()
            if page > 0:
                builder.button(text="⬅️ Oldingi", callback_data=f"digest_debug:page:{page - 1}")
            builder.button(text=f"📄 {page + 1}/{total_pages}", callback_data="noop")
            if page < total_pages - 1:
                builder.button(text="Keyingi ➡️", callback_data=f"digest_debug:page:{page + 1}")

            builder.button(text="🔄 Yangilash", callback_data=f"digest_debug:page:{page}")
            builder.button(text="📰 Digest", callback_data="digest:open")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            nav_count = (1 if page > 0 else 0) + 1 + (1 if page < total_pages - 1 else 0)
            builder.adjust(nav_count, 3)

            if is_callback:
                res = await safe_edit(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")
                if res:
                    return
            await safe_reply(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        async def _render_digest_stats(
            target_msg: types.Message,
            is_callback: bool = False,
        ):
            if not self.digest_service:
                return await safe_reply(target_msg, "❌ <b>Digest xizmati ulanmagan.</b>", parse_mode="HTML")

            summary = await self.db_manager.get_digest_stats_summary()
            last_run = summary.get("last_run")
            avg_duration = summary.get("avg_duration_last_7", 0.0)
            active_sources = summary.get("active_sources_count", 0)

            tz = pytz.timezone(settings.TIMEZONE)
            if last_run and last_run.get("started_at"):
                try:
                    raw_st = str(last_run["started_at"])
                    if "T" in raw_st:
                        dt_obj = datetime.fromisoformat(raw_st.replace("Z", "+00:00"))
                    else:
                        dt_obj = datetime.strptime(raw_st, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    loc_dt = dt_obj.astimezone(tz)
                    last_run_time = loc_dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    last_run_time = str(last_run.get("started_at"))
            else:
                last_run_time = "Mavjud emas"

            posts_analyzed = last_run.get("posts_collected", 0) if last_run else 0
            items_delivered = last_run.get("items_in_digest", 0) if last_run else 0
            spam_dropped = last_run.get("posts_filtered", 0) if last_run else 0
            ai_calls = last_run.get("ai_calls_count", 0) if last_run else 0
            last_dur = last_run.get("duration_seconds", 0.0) if last_run else 0.0

            text = (
                "📊 <b>SMART DIGEST V2 — STATISTIKA VA TELEMETRIYA</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⏱ <b>So'nggi tahlil vaqti:</b> <code>{last_run_time}</code>\n"
                f"📥 <b>Tahlil qilingan postlar:</b> <code>{posts_analyzed}</code> ta\n"
                f"🎯 <b>Digestga kiritilgan xulosalar:</b> <code>{items_delivered}</code> ta\n"
                f"🛡 <b>Tozalangan reklama/spam:</b> <code>{spam_dropped}</code> ta\n"
                f"🤖 <b>AI so'rovlari:</b> <code>{ai_calls}</code> ta\n"
                f"⚡️ <b>Oxirgi generatsiya davomiyligi:</b> <code>{last_dur:.1f}</code> soniya\n"
                f"📈 <b>O'rtacha davomiylik (so'nggi 7 ta run):</b> <code>{avg_duration:.1f}</code> soniya\n\n"
                f"📡 <b>Kuzatilayotgan faol manbalar:</b> <code>{active_sources}</code> ta\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Har kuni soat {settings.DIGEST_TIME} da avtomatik yuboriladi.</i>"
            )

            builder = InlineKeyboardBuilder()
            builder.button(text="📰 Yangi Digest", callback_data="digest:open")
            builder.button(text="🔄 Yangilash", callback_data="digest:stats")
            builder.button(text="📋 Manbalar", callback_data="digest_sources:list")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(2, 2)

            if is_callback:
                res = await safe_edit(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")
                if res:
                    return
            await safe_reply(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("digest"))
        async def cmd_digest(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            raw_text = (message.text or "").strip()
            parts = raw_text.split(maxsplit=2)
            subcmd = parts[1].lower() if len(parts) > 1 else ""

            if subcmd == "stats":
                await _render_digest_stats(message, is_callback=False)
                return
            elif subcmd == "sources":
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
                try:
                    client = self.digest_service.get_client() if self.digest_service else None
                    success, resp_text, source = await self.digest_service.resolve_and_add_source(client, target)
                except Exception as e:
                    logger.error(f"Error in /digest add for target '{target}': {type(e).__name__}")
                    resp_text = f"❌ <b>Manbani qo'shishda xatolik yuz berdi:</b>\n<code>{html.escape(type(e).__name__)}</code>"

                if loading_msg:
                    try:
                        await loading_msg.edit_text(resp_text, reply_markup=None, parse_mode="HTML")
                        return
                    except Exception as e:
                        logger.warning(f"Failed to edit loading_msg ({type(e).__name__}), sending new reply...")
                await safe_reply(message, resp_text, reply_markup=None, parse_mode="HTML")
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
                try:
                    success, resp_text = await self.digest_service.remove_source(target)
                except Exception as e:
                    logger.error(f"Error in /digest remove for target '{target}': {type(e).__name__}")
                    resp_text = f"❌ <b>Manbani o'chirishda xatolik yuz berdi:</b>\n<code>{html.escape(type(e).__name__)}</code>"

                return await safe_reply(message, resp_text, reply_markup=None, parse_mode="HTML")
            elif subcmd == "topic":
                if len(parts) < 3:
                    return await safe_reply(
                        message,
                        "⚠️ <b>Mavzu kodini kiriting!</b>\n"
                        "Foydalanish: <code>/digest topic &lt;A-F&gt;</code>\n\n"
                        "<b>Mavjud mavzular:</b>\n"
                        "• <b>F</b> — Imkoniyatlar (grantlar, stipendiyalar)\n"
                        "• <b>E</b> — Sun'iy intellekt (AI)\n"
                        "• <b>C</b> — TSUE yangiliklari\n"
                        "• <b>B</b> — Tashqi siyosat va muzokaralar\n"
                        "• <b>A</b> — Siyosiy yangiliklar\n"
                        "• <b>D</b> — Kasblar narxlari (maoshlar)",
                        parse_mode="HTML",
                    )
                topic = parts[2].strip().upper()
                if topic not in ["A", "B", "C", "D", "E", "F"]:
                    return await safe_reply(
                        message,
                        f"⚠️ <b>Noto'g'ri mavzu kodi: '{html.escape(topic)}'</b>\n"
                        "Faqat <b>A, B, C, D, E, F</b> kodlaridan birini tanlang.",
                        parse_mode="HTML",
                    )
                await _execute_digest(message, is_callback=False, topic_filter=topic)
                return
            elif subcmd == "debug":
                await _render_digest_debug(message, page=0, is_callback=False)
                return

            await _execute_digest(message, is_callback=False)

        async def _handle_natural_digest_action(message: types.Message) -> bool:
            """
            Detects natural language channel/digest management requests:
            - Add channel to digest: '@kunuz bu kanalni qushib quy dayjest bulimiga', 'dayjestga @kunuz ni qo'sh'
            - Remove channel from digest: '@kunuz ni dayjestdan o'chir', 'dayjestdan @kunuz ni olib tashla'
            - List digest sources: 'dayjest manbalari', 'dayjest kanallari'
            - Run/generate digest: 'dayjest tayyorla', 'bugungi dayjest'
            Returns True if handled, False otherwise.
            """
            raw_text = (message.text or "").strip()
            if not raw_text:
                return False

            lower_text = raw_text.lower()

            # Target channel extraction
            target = None
            target_match = re.search(r"@([a-zA-Z0-9_]{3,32})", raw_text)
            link_match = re.search(r"(?:https?://)?t\.me/([a-zA-Z0-9_+/]+)", raw_text)
            if target_match:
                target = f"@{target_match.group(1)}"
            elif link_match:
                target = link_match.group(0)
            else:
                stand_match = re.search(r"\b([a-zA-Z0-9_]{3,32})\s+kanali(?:ni|ga|da|dan)?\b", lower_text)
                if stand_match:
                    target = f"@{stand_match.group(1)}"

            # Indicators
            has_digest_kw = any(w in lower_text for w in [
                "dayjest", "digest", "dayjestga", "digestga", "dayjestdan", "digestdan",
                "dayjestdagi", "digestdagi", "dayjest bulimiga", "dayjest bo'limiga", "dayjest bulimi",
                "dayjest bo'limi", "kuzatuv", "kuzatish"
            ])
            has_channel_kw = any(w in lower_text for w in ["kanal", "kanali", "kanalini", "manba", "manbasi", "manbani"])

            has_add_kw = any(w in lower_text for w in [
                "qo'sh", "qush", "qo'shib", "qushib", "qo'shish", "qushish", "ulan", "ulansin", "biriktir", "kirit", "add"
            ])
            has_remove_kw = any(w in lower_text for w in [
                "o'chir", "ochir", "o'chirib", "ochirib", "olib tashla", "chiqar", "chiqarib", "remove", "delete", "del"
            ])
            has_list_kw = any(w in lower_text for w in [
                "manbalari", "manbalar", "kanallari", "kanallar", "ro'yxat", "ro'yxati", "qaysi kanallar", "sources"
            ])

            # 1. Add source to digest
            if target and has_add_kw and (has_digest_kw or has_channel_kw):
                clean_target = target.lstrip("@").strip()
                try:
                    if self.digest_service:
                        client = self.digest_service.get_client()
                        success, resp_text, source = await self.digest_service.resolve_and_add_source(client, target)
                        if not success and ("Userbot mijozi ulanmagan" in resp_text or "topilmadi" in resp_text):
                            source = await self.db_manager.add_digest_source(
                                peer_id=abs(hash(clean_target)) % 1000000000,
                                title=clean_target,
                                peer_type="channel",
                                username=clean_target,
                            )
                            resp_text = (
                                f"✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
                                f"📢 <b>Nomi:</b> {html.escape(clean_target)}\n"
                                f"🔗 <b>Username:</b> <code>@{clean_target}</code>\n"
                                f"🏷 <b>Turi:</b> Kanal (Kuzatuvga olindi)\n\n"
                                f"<i>Executive Digest har kuni yangi postlarni tahlil qilib boradi.</i>"
                            )
                    else:
                        source = await self.db_manager.add_digest_source(
                            peer_id=abs(hash(clean_target)) % 1000000000,
                            title=clean_target,
                            peer_type="channel",
                            username=clean_target,
                        )
                        resp_text = (
                            f"✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
                            f"📢 <b>Nomi:</b> {html.escape(clean_target)}\n"
                            f"🔗 <b>Username:</b> <code>@{clean_target}</code>\n"
                            f"🏷 <b>Turi:</b> Kanal (Kuzatuvga olindi)\n\n"
                            f"<i>Executive Digest har kuni yangi postlarni tahlil qilib boradi.</i>"
                        )
                except Exception as e:
                    logger.error(f"Error handling natural digest add for '{target}': {type(e).__name__}")
                    resp_text = f"❌ <b>Manbani qo'shishda xatolik yuz berdi:</b>\n<code>{html.escape(type(e).__name__)}</code>"

                await safe_reply(message, resp_text, reply_markup=None, parse_mode="HTML")
                return True

            # 2. Remove source from digest
            if target and has_remove_kw and (has_digest_kw or has_channel_kw):
                clean_target = target.lstrip("@").strip()
                try:
                    if self.digest_service:
                        success, resp_text = await self.digest_service.remove_source(clean_target)
                    else:
                        success = await self.db_manager.remove_digest_source(clean_target)
                        if success:
                            resp_text = f"✅ <code>@{clean_target}</code> kuzatiladigan manbalar ro'yxatidan o'chirildi."
                        else:
                            resp_text = f"❌ <code>@{clean_target}</code> manbalar ro'yxatida topilmadi."
                except Exception as e:
                    logger.error(f"Error handling natural digest remove for '{target}': {type(e).__name__}")
                    resp_text = f"❌ <b>Manbani o'chirishda xatolik yuz berdi:</b>\n<code>{html.escape(type(e).__name__)}</code>"

                await safe_reply(message, resp_text, reply_markup=None, parse_mode="HTML")
                return True

            # 3. List sources
            if (has_digest_kw or "kuzatilayotgan" in lower_text) and has_list_kw:
                sources = await self.db_manager.list_digest_sources(active_only=True)
                if not sources:
                    empty_text = (
                        "📋 <b>KUZATILAYOTGAN MANBALAR RO'YXATI (DIGEST SOURCES):</b>\n\n"
                        "<i>Hozircha hech qanday manba qo'shilmagan.</i>\n\n"
                        "💡 <b>Manba qo'shish:</b>\n"
                        "• Shunchaki yozing: <i>\"dayjestga @kanal nomini qo'sh\"</i>\n"
                        "• Yoki buyruq: <code>/digest add @kanal</code>"
                    )
                    await safe_reply(message, empty_text, reply_markup=None, parse_mode="HTML")
                    return True

                counts = await self.db_manager.get_all_source_post_counts()
                lines = [
                    f"📋 <b>KUZATILAYOTGAN MANBALAR (Jami: {len(sources)} ta):</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                ]
                for s in sources:
                    type_badge = "📢 Kanal" if s.peer_type == "channel" else "👥 Guruh"
                    uname = f"@{s.username}" if s.username else f"ID: {s.peer_id}"
                    post_cnt = counts.get(s.peer_id, 0)
                    lines.append(
                        f"• <b>{html.escape(s.title)}</b> [{type_badge}]\n"
                        f"  🔗 <code>{html.escape(uname)}</code> | 📝 Postlar: <b>{post_cnt}</b> ta\n"
                    )
                lines.append(
                    "\n💡 <b>Boshqarish:</b>\n"
                    "• <i>'dayjestga @kanal ni qo'sh'</i> — Yangi manba qo'shish\n"
                    "• <i>'dayjestdan @kanal ni o'chir'</i> — Ro'yxatdan chiqarish"
                )
                await safe_reply(message, "\n".join(lines), reply_markup=None, parse_mode="HTML")
                return True

            # 4. Run/Generate digest
            if has_digest_kw and any(w in lower_text for w in ["tayyorla", "chiqar", "ber", "tuz", "yarat", "bugungi", "ko'rsat", "korsat"]):
                await _execute_digest(message, is_callback=False)
                return True

            return False

        async def _handle_contact_inquiry_or_outreach(message: types.Message) -> bool:
            """
            Handles natural language contact inquiries, check-ins, and direct outreach:
            - '@gulzodaxursandovadan habar olib quychi nima gaplar ekan hozir'
            - 'Gulzodadan xabar olib qo'y'
            - '@umid_namozovga salom deb yoz'
            - '@ali bilan oxirgi marta nima gaplashgan edik'
            """
            raw_text = (message.text or "").strip()
            if not raw_text:
                return False

            lower_text = raw_text.lower()

            # Inquiry / outreach intent keywords
            inquiry_keywords = [
                "habar ol", "xabar ol", "habar olib", "xabar olib", "nima gap",
                "nima yangilik", "hol-ahvol", "bilib ber", "ko'rib ber", "gaplashgan",
                "nima dedi", "nima yozgan", "salom deb", "deb yoz", "deb jo'nat",
                "deb yubor", "xabar yoz", "habar yoz", "xabar jo'nat", "habar jo'nat",
                "yozib qo'y", "yozvor", "yozib yubor", "xabar yubor", "habar yubor",
                "menga yetkaz", "menga ayt", "menga xabar", "menga bildir", "menga top",
                "shugullanyapti", "shug'ullanyapti", "nima qilyapti", "nimalar bilan",
                "vaqti bormikin", "vaqti bormi", "kurishishga vaqti", "ko'rishishga vaqti",
                "rejelari qanaqa", "rejalari qanaqa", "rejalari nima", "rejelari nima",
                "surishtir", "so'rab ber", "sorab ber", "aniqlab ber", "gacha"
            ]
            if not any(k in lower_text for k in inquiry_keywords):
                return False

            # Don't intercept digest or channel actions
            if any(w in lower_text for w in ["kanal", "channel", "dayjest", "digest", "manba"]):
                return False

            # Extract target username (@username) or name
            target_username = None
            target_match = re.search(r"@([a-zA-Z0-9_]{3,32})", raw_text)
            if target_match:
                raw_handle = target_match.group(1)
                target_username = normalize_uzbek_username_token(raw_handle)
            else:
                # Check if an existing contact is mentioned by name
                contacts = await self.db_manager.list_contacts()
                for c in contacts:
                    keys = []
                    if c.username:
                        keys.append(c.username.lower())
                    if c.full_name:
                        parts = [p.lower() for p in re.split(r"[\s_]+", c.full_name) if len(p) >= 3]
                        keys.extend(parts)
                    for k in keys:
                        if re.search(rf"(?i)\b{re.escape(k)}(?:ni|ga|da|dan|ning)?\b", lower_text):
                            target_username = c.username or str(c.user_id)
                            break
                    if target_username:
                        break

            if not target_username:
                return False

            clean_username = target_username.lstrip("@").strip()

            # 1. Fetch real-time Telegram context via Userbot client
            messages_history: List[Dict[str, Any]] = []
            resolved_title = clean_username
            resolved_peer_id = None
            client = self.digest_service.get_client() if self.digest_service else None

            if client:
                try:
                    entity = await asyncio.wait_for(client.get_entity(clean_username), timeout=10.0)
                    resolved_peer_id = getattr(entity, "id", None)
                    resolved_title = getattr(entity, "first_name", None) or clean_username
                    raw_msgs = await asyncio.wait_for(client.get_messages(entity, limit=8), timeout=10.0)
                    tz = pytz.timezone(settings.TIMEZONE)
                    for m in reversed(raw_msgs):
                        if not getattr(m, "text", None):
                            continue
                        sender_label = "Siz (Xo'jayin)" if getattr(m, "out", False) else resolved_title
                        msg_dt = m.date.astimezone(tz) if getattr(m, "date", None) else None
                        time_str = msg_dt.strftime("%d-%B, %H:%M") if msg_dt else ""
                        messages_history.append({
                            "sender": sender_label,
                            "time": time_str,
                            "text": m.text,
                        })
                except Exception as e:
                    logger.warning(f"Userbot entity/messages fetch for '{clean_username}' failed: {type(e).__name__}")

            # Check local DB contact and messages
            contact = await self.db_manager.get_contact_by_username(clean_username)
            if not contact and resolved_peer_id:
                contact = await self.db_manager.get_contact_by_id(resolved_peer_id)

            if contact and not messages_history:
                local_msgs = await self.db_manager.get_mem_messages(contact.user_id, limit=8)
                for lm in local_msgs:
                    sender_label = "Siz (Xo'jayin)" if lm.sender in ["owner_manual", "bot_auto", "bot_approved"] else contact.full_name
                    messages_history.append({
                        "sender": sender_label,
                        "time": str(lm.created_at or ""),
                        "text": lm.text,
                    })

            # Auto-register contact if newly resolved from Telegram
            if not contact and resolved_peer_id:
                try:
                    contact = await self.db_manager.upsert_contact(
                        user_id=resolved_peer_id,
                        username=clean_username,
                        full_name=resolved_title,
                        mode="auto",
                        addressing_style="siz",
                    )
                except Exception as e:
                    logger.warning(f"Auto-register contact for '{clean_username}' failed: {type(e).__name__}")

            # Background memory facts
            facts_list = []
            if contact:
                active_facts = await self.db_manager.get_mem_facts(contact.user_id)
                facts_list = [f.text for f in active_facts]
                resolved_title = contact.full_name or resolved_title

            # 1. Clean first name for display & greetings
            clean_display_name = extract_clean_first_name(resolved_title) or resolved_title
            addressing_style = contact.addressing_style if contact else "siz"

            # 2. Call AI Engine for Intelligence Synthesis
            ai_res = await self.ai_engine.analyze_contact_inquiry(
                contact_name=clean_display_name,
                username=clean_username,
                messages=messages_history,
                user_query=raw_text,
                addressing_style=addressing_style,
                facts=facts_list,
            )

            # Check if this is a direct send instruction (e.g. '@usernamega salom deb yoz')
            if ai_res.get("is_direct_send") and ai_res.get("direct_message"):
                direct_text = ai_res["direct_message"]
                if self._userbot_send_fn:
                    try:
                        await self._userbot_send_fn(f"@{clean_username}", direct_text)
                        await safe_reply(
                            message,
                            f"🚀 <b>Xabar yuborildi!</b>\n\n"
                            f"👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(clean_username)}</code> ({html.escape(clean_display_name)})\n"
                            f"📝 <b>Matn:</b> <i>\"{html.escape(direct_text)}\"</i>",
                            parse_mode="HTML",
                            reply_markup=None,
                        )
                        return True
                    except Exception as e:
                        logger.error(f"Direct send to {clean_username} failed: {type(e).__name__}")

            # Present the executive intelligence briefing + suggested check-in message
            summary = ai_res.get("summary", "Yozishmalar mavjud emas.")
            suggested = ai_res.get("suggested_outreach", f"Salom {clean_display_name}, yaxshimisiz?")
            suggested = sanitize_outreach_constraints(suggested, raw_text, clean_display_name)

            # Check if this is a JARVIS delegation mission or inquiry
            is_delegation = ai_res.get("is_delegation_mission") or any(k in lower_text for k in [
                "shugullanyapti", "shug'ullanyapti", "vaqti bormikin", "vaqti bormi",
                "rejalari", "rejelari", "menga yetkaz", "menga ayt", "menga xabar",
                "bilib ber", "aniqlab ber", "surishtir", "nima qilyapti"
            ])

            # Extract optional deadline (e.g. '20:00gacha', 'bugun 20:00 gacha')
            deadline_str = None
            dl_match = re.search(r"(\b(?:bugun|ertaga)\s+)?(\d{1,2}:\d{2})\s*gacha", raw_text, re.IGNORECASE)
            if dl_match:
                prefix = dl_match.group(1) or "Bugun "
                t_val = dl_match.group(2)
                deadline_str = f"{prefix.strip()} soat {t_val} gacha"

            mission_goal = ai_res.get("mission_goal") or "Bugungi rejalari, bandligi va ko'rishish imkoniyatini aniqlash."

            # Check if direct action was explicitly commanded ('yozchi', 'yoz', 'jo'natchi', etc.)
            is_direct_action = ai_res.get("is_direct_action") or bool(re.search(
                r"\b(?:yoz(?:chi|gin)?|jo['`’‘]?nat(?:chi|gin)?|yubor(?:chi|gin)?|so['`’‘]ra(?:chi)?)\b",
                lower_text
            ))

            # IF direct action ordered AND userbot sender available: DISPATCH IMMEDIATELY!
            if is_direct_action and self._userbot_send_fn and suggested:
                try:
                    await self._userbot_send_fn(f"@{clean_username}", suggested)
                    self._pending_outreach = None

                    if not hasattr(self, "_active_missions"):
                        self._active_missions = {}
                    self._active_missions[clean_username.lower()] = {
                        "peer": f"@{clean_username}",
                        "display_name": clean_display_name,
                        "goal": mission_goal,
                        "deadline": deadline_str,
                        "dispatched_at": time.time(),
                    }

                    deadline_line = f"⏰ <b>Muddat (Deadline):</b> <code>{html.escape(deadline_str)}</code>\n" if deadline_str else ""
                    resp = (
                        f"🚀 <b>TOPSHIRIQ ZUDLIK BILAN BAJARILDI, JANOB! (JARVIS Protocol)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(clean_username)}</code> ({html.escape(clean_display_name)})\n"
                        f"{deadline_line}"
                        f"🎯 <b>Vazifa:</b> {html.escape(mission_goal)}\n\n"
                        f"📝 <b>Yuborilgan xabar:</b>\n"
                        f"<i>\"{html.escape(suggested)}\"</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📡 <i>{html.escape(clean_display_name)} javob berishi bilanoq ma'lumotlarni tahlil qilib, sizga hisobot beraman!</i>"
                    )
                    await safe_reply(message, resp, parse_mode="HTML", reply_markup=None)
                    return True
                except Exception as e:
                    logger.error(f"Direct dispatch to {clean_username} failed: {type(e).__name__}")

            # Save pending outreach for 1-word confirmation ('yubor', 'ha')
            self._pending_outreach = {
                "peer": f"@{clean_username}",
                "display_name": clean_display_name,
                "text": suggested,
                "time": time.time(),
                "is_mission": is_delegation,
                "deadline": deadline_str,
            }

            if is_delegation:
                deadline_line = f"⏰ <b>Muddat (Deadline):</b> <code>{html.escape(deadline_str)}</code>\n" if deadline_str else ""
                resp = (
                    f"🎯 <b>TOPSHIRIQ QABUL QILINDI, JANOB! (JARVIS Protocol)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Aloqa:</b> <code>@{html.escape(clean_username)}</code> ({html.escape(clean_display_name)})\n"
                    f"{deadline_line}"
                    f"🎯 <b>Vazifa:</b> {html.escape(mission_goal)}\n\n"
                    f"📊 <b>Oxirgi yozishmalar tahlili:</b>\n"
                    f"ℹ️ {html.escape(summary)}\n\n"
                    f"✍️ <b>Tayyorlangan diplomatiya xabari:</b>\n"
                    f"<i>\"{html.escape(suggested)}\"</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🚀 <b>Xabarni darhol yuborish uchun:</b> Chatga <b>\"yubor\"</b> yoki <b>\"ha\"</b> deb yozing.\n"
                    f"⚡️ <i>Javob kelishi bilanoq ma'lumotlarni tahlil qilib sizga hisobot beraman!</i>"
                )
            else:
                resp = (
                    f"📋 <b>@{html.escape(clean_username)} ({html.escape(clean_display_name)}) bilan muloqot tahlili:</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"ℹ️ <b>Holat:</b> {html.escape(summary)}\n\n"
                    f"✍️ <b>Tavsiya etilgan xabar (Hol-ahvol so'rash):</b>\n"
                    f"<i>\"{html.escape(suggested)}\"</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🚀 <b>Xabarni yuborish uchun:</b> Chatga <b>\"yubor\"</b> yoki <b>\"ha\"</b> deb yozing.\n"
                    f"✏️ <i>Yoki o'zingiz xohlagan matnni yozishingiz mumkin.</i>"
                )

            await safe_reply(message, resp, parse_mode="HTML", reply_markup=None)
            return True

        async def _handle_pending_outreach_confirmation(message: types.Message) -> bool:
            if not getattr(self, "_pending_outreach", None):
                return False

            raw_text = (message.text or "").strip().lower()
            pending = self._pending_outreach
            # Expiry: 15 minutes
            if time.time() - pending.get("time", 0) > 900:
                self._pending_outreach = None
                return False

            # Positive confirmation
            if raw_text in ["yubor", "ha", "jo'nat", "yuborilsin", "yoz", "yozib yubor", "send", "yes", "ok"]:
                peer = pending["peer"]
                text_to_send = pending["text"]
                dname = pending.get("display_name", peer)

                if self._userbot_send_fn:
                    try:
                        await self._userbot_send_fn(peer, text_to_send)
                        self._pending_outreach = None
                        mission_note = (
                            f"\n\n📡 <i>JARVIS topshiriq faollashtirildi: {html.escape(peer)} dan javob kelishi bilan "
                            f"tahlil qilinadi va sizga darhol hisobot taqdim etiladi.</i>"
                        ) if pending.get("is_mission") else ""
                        await safe_reply(
                            message,
                            f"🚀 <b>Xabar muvaffaqiyatli yuborildi!</b>\n\n"
                            f"👤 <b>Qabul qiluvchi:</b> <code>{html.escape(peer)}</code> ({html.escape(dname)})\n"
                            f"📝 <b>Yuborilgan matn:</b> <i>\"{html.escape(text_to_send)}\"</i>"
                            f"{mission_note}",
                            parse_mode="HTML",
                            reply_markup=None,
                        )
                        return True
                    except Exception as e:
                        logger.error(f"Error sending outreach to {peer}: {type(e).__name__}")
                        self._pending_outreach = None
                        await safe_reply(
                            message,
                            f"❌ <b>Xabarni yuborishda xatolik yuz berdi:</b> <code>{html.escape(type(e).__name__)}</code>",
                            parse_mode="HTML",
                            reply_markup=None,
                        )
                        return True
                else:
                    self._pending_outreach = None
                    await safe_reply(
                        message,
                        "⚠️ Userbot yuborish xizmati faol emas.",
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                    return True

            # Negative confirmation / cancellation
            if raw_text in ["yo'q", "yoq", "kerak emas", "bekor qil", "no", "cancel"]:
                self._pending_outreach = None
                await safe_reply(
                    message,
                    "❌ <b>Xabar yuborish bekor qilindi.</b>",
                    parse_mode="HTML",
                    reply_markup=None,
                )
                return True

            return False

        async def _apply_contact_instruction(
            msg_obj: types.Message,
            target_user: str,
            instruction_text: str,
            forced_style: Optional[str] = None,
        ):
            target = target_user.replace("@", "").strip()
            instruction = instruction_text.strip()

            # Strict Rule H1 Guard: channels/groups/digest must NEVER enter contacts table!
            lower_inst = instruction.lower()
            if any(w in lower_inst for w in ["dayjest", "digest", "kanal", "channel", "guruh", "group", "manba"]):
                handled = await _handle_natural_digest_action(msg_obj)
                if handled:
                    return
                return await safe_reply(
                    msg_obj,
                    f"⚠️ <b>Kanallar yoki guruhlar shaxsiy CRM kontaktlariga qo'shilmaydi (Rule H1)!</b>\n\n"
                    f"📢 <code>@{html.escape(target)}</code> manbasini Executive Digest bo'limiga qo'shish uchun:\n"
                    f"• Chatga: <i>\"dayjestga @{html.escape(target)} kanalini qo'sh\"</i> deb yozing\n"
                    f"• Yoki buyruq: <code>/digest add @{html.escape(target)}</code>",
                    parse_mode="HTML",
                    reply_markup=None,
                )

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
            mode = existing.mode if existing else "auto"

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
            else:
                style_badge = "🔵 <b>SIZ (Sizlash / Hurmat bilan)</b>"
                style_exp = f"Bot <code>@{safe_target}</code> ga faqat <i>'siz', 'qilyapsiz'</i> deb yuksak hurmat va odob bilan gaplashadi!"

            await safe_reply(
                msg_obj,
                f"✨ <b>CRM PROFILI SOZLANDI, XO'JAYIN!</b>\n\n"
                f"👤 <b>Kontakt:</b> <code>@{safe_target}</code>\n"
                f"⚙️ <b>Avtonomiya rejimi:</b> {mode_badge}\n"
                f"🗣 <b>Murojaat shakli:</b> {style_badge}\n"
                f"🎭 <b>Munosabat:</b> <code>{safe_role_label}</code>\n"
                f"📜 <b>Ko'rsatma:</b> <i>\"{safe_instruction}\"</i>\n\n"
                f"🧠 <b>Butler xotirasiga muhrlandi!</b>\n"
                f"{style_exp}\n\n"
                f"💡 <i>Uslubni o'zgartirish: chatga 'sen' yoki 'siz' deb yozishingiz kifoya.</i>",
                reply_markup=None,
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

        async def _extract_contact_from_reply(reply_msg: types.Message) -> Optional[Any]:
            """Helper to extract a CRM contact from a replied message."""
            if not reply_msg:
                return None
            if getattr(reply_msg, "forward_from", None) and reply_msg.forward_from.id:
                c = await self.db_manager.get_contact_by_id(reply_msg.forward_from.id)
                if c:
                    return c
            text = reply_msg.text or reply_msg.caption or ""
            # Check draft id pattern: #(\d+)
            d_match = re.search(r"#(\d+)", text)
            if d_match:
                draft = await self.db_manager.get_draft_by_id(int(d_match.group(1)))
                if draft:
                    if draft.contact_user_id:
                        c = await self.db_manager.get_contact_by_id(draft.contact_user_id)
                        if c:
                            return c
                    if draft.contact_username:
                        c = await self.db_manager.get_contact_by_username(draft.contact_username)
                        if c:
                            return c
            # Check @username pattern
            u_match = re.search(r"@([a-zA-Z0-9_]{3,32})", text)
            if u_match:
                c = await self.db_manager.find_contact(u_match.group(1))
                if c:
                    return c
            return None

        async def _render_unified_contact_card(target_msg: types.Message, contact: Any, is_callback: bool = False):
            """Renders the Unified VIP Contact Card combining CRM tone, autonomy mode, memory stats and 1-tap controls."""
            mode = getattr(contact, "mode", "draft") or "draft"
            uname = f"@{contact.username}" if contact.username else f"ID:{contact.user_id}"
            safe_uname = html.escape(uname)
            safe_name = html.escape(contact.full_name or "Noma'lum")
            style = getattr(contact, "addressing_style", "siz") or "siz"
            safe_style = "🔵 SIZ (Sizlash / Hurmat)" if style == "siz" else "🟢 SEN (Sensirash / Yaqin do'st)"
            safe_instr = html.escape(contact.custom_instructions or "Mavjud emas")

            mode_badges = {
                "auto": "⚡ auto (Avtomatik javob)",
                "draft": "📝 draft (Qoralama / Tasdiq bilan)",
                "mute": "🔇 mute (Ovozsiz / E'tiborsiz)",
            }

            mem_contact = await self.db_manager.get_mem_contact(contact.user_id)
            stats = await self.db_manager.get_mem_stats(contact.user_id)
            summary = await self.db_manager.get_latest_mem_summary(contact.user_id)
            open_items = await self.db_manager.get_mem_open_items(contact.user_id, status="open")

            status_str = "🟢 Faol" if (mem_contact and mem_contact.memory_enabled) else "⏸ To'xtatilgan"
            if mem_contact and not mem_contact.memory_enabled and mem_contact.paused_reason:
                status_str += f" (Sabab: {mem_contact.paused_reason})"

            formatted_summary = html.escape(summary.summary_text) if (summary and summary.summary_text) else "<i>(Hozircha xulosa mavjud emas)</i>"
            open_items_text = ""
            if open_items:
                open_items_text = "\n\n<b>📌 Ochiq vazifalar va va'dalar (Faqat siz uchun):</b>\n" + "\n".join(
                    [f"• {html.escape(it.text)}" for it in open_items[:5]]
                )

            embed_count = stats.get("embeddings", 0)
            embed_str = f"<code>{embed_count}</code> ta" if embed_count > 0 else "o'chiq (FTS ishlaydi)"

            text = (
                f"🧠 <b>KONTAKT XOTIRASI VA BOSHQARUV KARTASI (KONTAKT AVTONOMIYA REJIMI):</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Kontakt:</b> <b>{safe_name}</b> (<code>{safe_uname}</code>)\n"
                f"🆔 <b>Telegram ID:</b> <code>{contact.user_id}</code>\n"
                f"🗣 <b>Murojaat uslubi:</b> {safe_style}\n"
                f"🔄 <b>Avtonomiya rejimi:</b> <b>{mode_badges.get(mode, mode)}</b>\n"
                f"📜 <b>Ko'rsatma:</b> <i>\"{safe_instr}\"</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Xotira holati:</b> {status_str}\n"
                f"• <b>Saqlangan xabarlar:</b> <code>{stats.get('messages', 0)}</code> ta\n"
                f"• <b>Faol faktlar:</b> <code>{stats.get('facts', 0)}</code> ta\n"
                f"• <b>Vektorlar:</b> {embed_str}\n\n"
                f"<b>📝 Aylanma xulosa (Rolling Summary):</b>\n"
                f"{formatted_summary}"
                f"{open_items_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>Quyidagi tezkor tugmalar orqali uslub, rejim va xotirani 1 ta bosishda boshqaring:</i>"
            )

            builder = InlineKeyboardBuilder()

            # Row 1: SIZ / SEN 1-tap toggle
            switch_to = "sen" if style == "siz" else "siz"
            switch_btn = "🟢 'SEN' ga o'tkazish" if style == "siz" else "🔵 'SIZ' ga o'tkazish"
            target_user = contact.username or str(contact.user_id)
            builder.button(text=switch_btn, callback_data=f"crm_style:{target_user}:{switch_to}")

            # Row 2: Mode 1-tap toggles with checkmark
            builder.button(
                text="✅ ⚡ Auto" if mode == "auto" else "⚡ Auto",
                callback_data=f"mode_set:{contact.user_id}:auto",
            )
            builder.button(
                text="✅ 📝 Draft" if mode == "draft" else "📝 Draft",
                callback_data=f"mode_set:{contact.user_id}:draft",
            )
            builder.button(
                text="✅ 🔇 Mute" if mode == "mute" else "🔇 Mute",
                callback_data=f"mode_set:{contact.user_id}:mute",
            )

            # Row 3: Memory actions
            builder.button(text=f"📋 Faktlar ({stats.get('facts', 0)})", callback_data=f"mem_facts_view:{contact.user_id}")
            builder.button(text="🗑 Xotirani tozalash (/forget)", callback_data=f"mem_forget_prompt:{contact.user_id}")

            # Row 4: Navigation
            builder.button(text="👥 Kontaktlar", callback_data="menu:contacts")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            builder.adjust(1, 3, 2, 2)

            if is_callback:
                await target_msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        async def _render_single_contact_mode(target_msg: types.Message, contact: Any, is_callback: bool = False):
            """Renders individual contact card by delegating to unified card."""
            await _render_unified_contact_card(target_msg, contact, is_callback=is_callback)

        async def _render_mode_panel(target_msg: types.Message, is_callback: bool = False):
            """Renders interactive autonomy mode management cockpit with one-tap toggle buttons."""
            contacts = await self.db_manager.list_contacts()
            counts = await self.db_manager.get_mode_counts()

            lines = [
                "⚙️ <b>AVTONOMIYA REJIMLARI BOSHQARUVI:</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <b>Mavjud rejimlar:</b>\n"
                "• ⚡ <b>auto</b> — AI darhol va avtomatik javob beradi (xavfsiz mavzularda)\n"
                "• 📝 <b>draft</b> — Qoralama tayyorlaydi, faqat siz tasdiqlagach yuboriladi\n"
                "• 🔇 <b>mute</b> — AI bu kontaktni to'liq e'tiborsiz qoldiradi\n\n"
                f"📊 <b>Statistika:</b> ⚡ auto: <b>{counts.get('auto', 0)}</b> | 📝 draft: <b>{counts.get('draft', 0)}</b> | 🔇 mute: <b>{counts.get('mute', 0)}</b> (Jami: <b>{counts.get('total', 0)} ta</b>)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            ]

            builder = InlineKeyboardBuilder()

            if not contacts:
                lines.append("<i>Hozircha kontaktlar mavjud emas. Uni /crm orqali qo'shing.</i>")
            else:
                lines.append("📋 <b>Kontaktlar va ularning faol rejimlari:</b>")
                for idx, c in enumerate(contacts, 1):
                    uname = f"@{c.username}" if c.username else f"ID:{c.user_id}"
                    safe_uname = html.escape(uname)
                    safe_name = html.escape(c.full_name or "")
                    mode = getattr(c, "mode", "draft") or "draft"

                    if mode == "auto":
                        badge = "⚡ auto (Avtomatik)"
                    elif mode == "mute":
                        badge = "🔇 mute (Ovozsiz)"
                    else:
                        badge = "📝 draft (Qoralama)"

                    lines.append(f"<b>{idx}. {safe_uname}</b> ({safe_name}): <code>{badge}</code>")

                    # Row 1: Contact card opener
                    builder.button(
                        text=f"👤 {uname} ({safe_name[:12]})",
                        callback_data=f"mode_pick:{c.user_id}",
                    )
                    # Row 2: 3 inline buttons for instant mode toggle
                    builder.button(
                        text="✅ ⚡ Auto" if mode == "auto" else "⚡ Auto",
                        callback_data=f"mode_set:{c.user_id}:auto",
                    )
                    builder.button(
                        text="✅ 📝 Draft" if mode == "draft" else "📝 Draft",
                        callback_data=f"mode_set:{c.user_id}:draft",
                    )
                    builder.button(
                        text="✅ 🔇 Mute" if mode == "mute" else "🔇 Mute",
                        callback_data=f"mode_set:{c.user_id}:mute",
                    )

            # Bulk actions
            builder.button(text="⚡ Barchasini Auto", callback_data="mode_bulk:auto")
            builder.button(text="📝 Barchasini Draft", callback_data="mode_bulk:draft")
            builder.button(text="🔇 Barchasini Mute", callback_data="mode_bulk:mute")

            # Navigations
            builder.button(text="🔄 Yangilash", callback_data="mode:refresh")
            builder.button(text="👥 CRM Kontaktlar", callback_data="crm:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            layout = []
            for _ in contacts:
                layout.extend([1, 3])
            layout.extend([3, 3])
            builder.adjust(*layout)

            content = "\n".join(lines)
            if is_callback:
                await target_msg.edit_text(content, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply_or_send_long(target_msg, content, reply_markup=builder.as_markup())

        @dp.message(Command("mode"))
        async def cmd_mode(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            text_after = message.text[len("/mode"):].strip()

            # 1. No arguments provided
            if not text_after:
                if message.reply_to_message:
                    replied_contact = await _extract_contact_from_reply(message.reply_to_message)
                    if replied_contact:
                        return await _render_single_contact_mode(message, replied_contact, is_callback=False)
                return await _render_mode_panel(message, is_callback=False)

            parts = text_after.split()

            # 2. Check for bulk command: /mode all auto, /mode barchasi draft, /mode hammasi mute
            if len(parts) >= 2 and parts[0].lower() in ("all", "barchasi", "hammasi", "hamma"):
                target_mode = parse_autonomy_mode(parts[1])
                if not target_mode:
                    return await safe_reply(
                        message,
                        f"⚠️ <b>Noto'g'ri rejim:</b> '{html.escape(parts[1])}'\n"
                        f"Mavjud rejimlar: <code>auto</code>, <code>draft</code>, <code>mute</code>",
                        parse_mode="HTML",
                    )
                count = await self.db_manager.update_all_contacts_mode(target_mode)
                label = {"auto": "⚡ auto (Avtomatik)", "draft": "📝 draft (Qoralama)", "mute": "🔇 mute (Ovozsiz)"}.get(target_mode, target_mode)
                await safe_reply(
                    message,
                    f"✅ <b>Barcha {count} ta kontakt rejimi {label} ga o'zgartirildi!</b>",
                    parse_mode="HTML",
                )
                return await _render_mode_panel(message, is_callback=False)

            # 3. Check if single argument provided
            if len(parts) == 1:
                single_arg = parts[0]
                mode_cand = parse_autonomy_mode(single_arg)
                if mode_cand and message.reply_to_message:
                    replied_contact = await _extract_contact_from_reply(message.reply_to_message)
                    if replied_contact:
                        await self.db_manager.update_contact_mode(replied_contact.user_id, mode_cand)
                        label = {"auto": "⚡ auto (Avtomatik)", "draft": "📝 draft (Qoralama)", "mute": "🔇 mute (Ovozsiz)"}.get(mode_cand, mode_cand)
                        return await safe_reply(
                            message,
                            f"✅ <code>@{replied_contact.username or replied_contact.user_id}</code> rejimi o'zgartirildi: <b>{label}</b>.",
                            parse_mode="HTML",
                        )
                elif mode_cand:
                    contacts = await self.db_manager.list_contacts()
                    builder = InlineKeyboardBuilder()
                    for c in contacts:
                        uname = f"@{c.username}" if c.username else c.full_name
                        builder.button(text=f"👤 {uname}", callback_data=f"mode_set:{c.user_id}:{mode_cand}")
                    builder.button(text="🔙 Barcha rejimlar", callback_data="mode:refresh")
                    builder.adjust(2)
                    label = {"auto": "⚡ auto", "draft": "📝 draft", "mute": "🔇 mute"}.get(mode_cand, mode_cand)
                    return await safe_reply(
                        message,
                        f"📌 <b>Qaysi kontaktni {label} rejimiga o'tkazmoqchisiz?</b>\n"
                        f"Quyidagi ro'yxatdan tanlang:",
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML",
                    )

                # Target contact lookup
                contact = await self.db_manager.find_contact(single_arg)
                if contact:
                    return await _render_single_contact_mode(message, contact, is_callback=False)
                else:
                    return await safe_reply(
                        message,
                        f"❌ <b>'{html.escape(single_arg)}' nomli kontakt topilmadi.</b>\n\n"
                        f"📌 <b>Foydalanish:</b> <code>/mode @username auto|draft|mute</code>\n"
                        f"Yoki barcha kontaktlarni ko'rish uchun <code>/mode</code> deb yozing.",
                        parse_mode="HTML",
                    )

            # 4. Two or more arguments: flexible ordering (/mode @username auto or /mode auto @username)
            target_mode = None
            target_contact_query = None

            if parse_autonomy_mode(parts[0]):
                target_mode = parse_autonomy_mode(parts[0])
                target_contact_query = " ".join(parts[1:])
            elif parse_autonomy_mode(parts[-1]):
                target_mode = parse_autonomy_mode(parts[-1])
                target_contact_query = " ".join(parts[:-1])
            elif parse_autonomy_mode(parts[1]):
                target_mode = parse_autonomy_mode(parts[1])
                target_contact_query = parts[0]
            else:
                return await safe_reply(
                    message,
                    f"⚠️ <b>Noto'g'ri format yoki rejim!</b>\n\n"
                    f"📌 <b>To'g'ri format:</b> <code>/mode @username auto|draft|mute</code>\n"
                    f"💡 <b>Mavjud rejimlar:</b> <code>auto</code>, <code>draft</code>, <code>mute</code>\n"
                    f"Misol: <code>/mode @ali auto</code> yoki <code>/mode auto @ali</code>",
                    parse_mode="HTML",
                )

            contact = await self.db_manager.find_contact(target_contact_query)
            if not contact:
                clean_uname = target_contact_query.lstrip("@").strip()
                return await safe_reply(
                    message,
                    f"❌ <code>@{html.escape(clean_uname)}</code> kontaktlar ro'yxatida topilmadi.\n\n"
                    f"Uni avval qo'shish uchun: <code>/crm @{html.escape(clean_uname)} siz [ko'rsatma]</code>\n"
                    f"Yoki barcha mavjud kontaktlar uchun: <code>/mode</code> deb yozing.",
                    parse_mode="HTML",
                )

            await self.db_manager.update_contact_mode(contact.user_id, target_mode)
            mode_labels = {
                "auto": "⚡ auto (Avtomatik javob)",
                "draft": "📝 draft (Qoralama / Tasdiq kerak)",
                "mute": "🔇 mute (Ovozsiz / E'tiborsiz)",
            }
            label = mode_labels.get(target_mode, target_mode)

            builder = InlineKeyboardBuilder()
            builder.button(
                text="⚡ Auto" if target_mode != "auto" else "✅ Auto",
                callback_data=f"mode_set:{contact.user_id}:auto",
            )
            builder.button(
                text="📝 Draft" if target_mode != "draft" else "✅ Draft",
                callback_data=f"mode_set:{contact.user_id}:draft",
            )
            builder.button(
                text="🔇 Mute" if target_mode != "mute" else "✅ Mute",
                callback_data=f"mode_set:{contact.user_id}:mute",
            )
            builder.button(text="📋 Barcha rejimlar", callback_data="mode:refresh")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(3, 2)

            display_name = f"@{contact.username}" if contact.username else contact.full_name
            await safe_reply(
                message,
                f"✅ <code>{html.escape(display_name)}</code> rejimi o'zgartirildi: <b>{label}</b>.",
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

                await safe_reply(
                    msg_obj,
                    f"⏰ <b>VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI! (ID #{task.id})</b>\n\n"
                    f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                    f"🕒 <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                    f"💬 <b>Xabar matni:</b>\n<i>\"{safe_msg}\"</i>\n\n"
                    f"🧠 <i>Belgilangan vaqtda bot profilingiz nomidan xabarni avtomatik yuboradi.</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🗑 <i>Bekor qilish uchun:</i> <code>bekor #{task.id}</code> deb yozing.",
                    reply_markup=None,
                    parse_mode="HTML",
                )
                return True
            except Exception as e:
                logger.error(f"Error creating scheduled task: {type(e).__name__}")
                await safe_reply(msg_obj, f"❌ Rejalashtirishda xatolik: {html.escape(type(e).__name__)}", parse_mode="HTML")
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
                    logger.error(f"Error creating wizard scheduled task: {type(e).__name__}")
                    return await safe_reply(message, f"❌ Rejalashtirishda xatolik: {html.escape(type(e).__name__)}", parse_mode="HTML")

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
                logger.error(f"Error creating wizard scheduled task: {type(e).__name__}")
                await safe_reply(message, f"❌ Rejalashtirishda xatolik: {html.escape(type(e).__name__)}", parse_mode="HTML")

        async def _render_memory_panel(target_msg: types.Message, is_callback: bool = False):
            """Renders rich interactive contact memory hub with one-tap contact memory buttons."""
            stats = await self.db_manager.get_overall_mem_stats()
            embed_count = stats.get("embeddings", 0)
            embed_str = f"<code>{embed_count}</code> ta" if embed_count > 0 else "o'chiq (FTS5 faol)"
            contacts = await self.db_manager.list_contacts()

            text = (
                "🧠 <b>XOTIRA MARKAZI (CONTACT MEMORY)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <i>AI kontaktlaringiz haqidagi muhim faktlar, kelishuvlar va suhbat xulosalarini xavfsiz saqlaydi.</i>\n\n"
                "📊 <b>STATISTIKA:</b>\n"
                f"├ 👤 Xotirasi faol kontaktlar: <b>{stats.get('contacts_enabled', 0)} ta</b>\n"
                f"├ 💬 Saqlangan xabarlar: <b>{stats.get('messages', 0)} ta</b>\n"
                f"├ 💡 Faol faktlar: <b>{stats.get('facts', 0)} ta</b>\n"
                f"├ 📝 Aylanma xulosalar: <b>{stats.get('summaries', 0)} ta</b>\n"
                f"└ ⚡ Qidiruv tizimi: <b>{embed_str}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "👇 <b>Kontakt xotirasini boshqarish uchun tanlang:</b>"
            )

            builder = InlineKeyboardBuilder()
            adjust_layout = []
            if contacts:
                for c in contacts:
                    c_name = c.full_name or c.username or str(c.user_id)
                    builder.button(text=f"👤 {c_name}"[:28], callback_data=f"mem_back:{c.user_id}")
                    adjust_layout.append(1)
            else:
                text += "\n\n<i>(Hozircha saqlangan kontaktlar yo'q. /contacts orqali kontakt qo'shing)</i>"

            builder.button(text="👥 Kontaktlar ro'yxati", callback_data="menu:contacts")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            adjust_layout.append(2)
            builder.adjust(*adjust_layout)

            if is_callback:
                await target_msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await safe_reply(target_msg, text, reply_markup=builder.as_markup(), parse_mode="HTML")

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
                f"👥 <b>SAQLANGAN KONTAKTLAR ({len(contacts)} ta):</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🛡 <i>AI FAQAT shu ro'yxatdagi insonlar bilan muloqot qiladi va ularning xotirasini yuritadi.</i>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
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
                    f"   ├ ⚙️ Rejim: {mode_badge}\n"
                    f"   ├ 🗣 Murojaat: {style_badge}\n"
                    f"   ├ 🎭 Munosabat: <code>{safe_rel}</code>\n"
                    f"   └ 📜 Ko'rsatma: <i>\"{safe_instr}\"</i>\n"
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

                # Row 3: Memory shortcut button
                builder.button(
                    text=f"🧠 Xotira ({safe_uname})",
                    callback_data=f"mem_back:{c.user_id}",
                )

            builder.button(text="➕ Yangi kontakt", callback_data="crm:add_prompt")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_layout = []
            for _ in contacts:
                adjust_layout.extend([2, 3, 1])
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

        @dp.message(StateFilter(None), F.text & ~F.text.startswith("/") & F.text.regexp(r"(?i)(?:^|.*?)@([a-zA-Z0-9_]{3,32})\s+(.+)"))
        async def handle_natural_crm_or_schedule(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return

            # -1. Check if this is a natural digest action (e.g. @kunuz bu kanalni qushib quy dayjest bulimiga)
            if await _handle_natural_digest_action(message):
                return

            # -0.5. Check if this is a contact inquiry or outreach request (e.g. @gulzodaxursandovadan habar olib quychi nima gaplar ekan hozir)
            if await _handle_contact_inquiry_or_outreach(message):
                return

            # 0. Check if this is a natural mode change request (e.g. @username ni auto qil)
            mode_intent_match = re.search(
                r"(?i)(?:^|.*?)(@?[a-zA-Z0-9_]{3,32})\s+(?:ni\s+)?(auto|avto|avtomatik|draft|qoralama|tasdiq|mute|ovozsiz|ochir)\s*(?:rejimiga\s+o'tkaz|rejimiga\s+quyil|rejimiga\s+quy|rejimiga\s+o't|rejimiga|rejimida|qil|qoy|qo'y)?",
                message.text
            )
            if mode_intent_match:
                tgt = mode_intent_match.group(1)
                raw_m = mode_intent_match.group(2)
                parsed_m = parse_autonomy_mode(raw_m)
                if parsed_m:
                    contact = await self.db_manager.find_contact(tgt)
                    if contact:
                        await self.db_manager.update_contact_mode(contact.user_id, parsed_m)
                        labels = {"auto": "⚡ auto (Avtomatik)", "draft": "📝 draft (Qoralama)", "mute": "🔇 mute (Ovozsiz)"}
                        lbl = labels.get(parsed_m, parsed_m)
                        uname = f"@{contact.username}" if contact.username else contact.full_name
                        return await safe_reply(
                            message,
                            f"✅ <code>{html.escape(uname)}</code> avtonomiya rejimi o'zgartirildi: <b>{lbl}</b>.",
                            parse_mode="HTML",
                        )

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

            # 3. Fallback regex match - require explicit CRM keywords to avoid false positives
            match = re.search(r"(?i)@([a-zA-Z0-9_]{3,32})\s+(.+)", message.text)
            if match:
                target = match.group(1)
                instruction = match.group(2)
                crm_indicators = [
                    "siz", "sen", "sizla", "senla", "sensira", "sizlab", "uslub",
                    "ko'rsatma", "korsatma", "muloqot", "gaplash", "muomala", "jigar",
                    "hurmat", "aka", "opa", "do'st", "dost", "hazil", "rasmiy",
                ]
                if any(w in instruction.lower() for w in crm_indicators):
                    return await _apply_contact_instruction(message, target, instruction)

        async def _render_tasks_list(target_msg: types.Message, is_callback: bool = False):
            """Renders pending tasks list with clean human-readable dates and action buttons."""
            tasks = await self.db_manager.get_pending_tasks()
            tz = pytz.timezone(settings.TIMEZONE)

            if not tasks:
                empty_text = (
                    "⏰ <b>REJALASHTIRILGAN VAZIFALAR:</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "<i>Hozircha kutilayotgan xabarlar mavjud emas.</i>\n\n"
                    "💡 <b>Yangi reja qo'shish juda oson (AI yordamida):</b>\n"
                    "Chatga shunchaki yozing, AI o'zi tushunadi:\n"
                    "• <i>\"Ertaga soat 10 da @ali ga salom deb yoz\"</i>\n"
                    "• <i>\"15 daqiqadan keyin @umid_namozov ga loyiha tayyor deb jo'nat\"</i>\n"
                    "• <code>/schedule @username 14:00 Matn</code>"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="✍️ Yangi vazifa rejalashtirish", callback_data="schedule:wizard")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1, 1)
                if is_callback:
                    await target_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                else:
                    await safe_reply(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            lines = [f"⏰ <b>REJALASHTIRILGAN VAZIFALAR ({len(tasks)} ta):</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
            builder = InlineKeyboardBuilder()

            for t in tasks:
                safe_target = html.escape(t.target_peer)
                formatted_time = format_task_time_human(t.scheduled_time, tz)
                safe_msg = html.escape(t.message_text)

                lines.append(
                    f"📌 <b>Vazifa #{t.id}</b>\n"
                    f"👤 <b>Kimga:</b> <code>{safe_target}</code>\n"
                    f"⏰ <b>Vaqti:</b> <code>{formatted_time}</code>\n"
                    f"💬 <b>Matn:</b>\n<blockquote>{safe_msg}</blockquote>\n"
                )
                builder.button(text=f"🚀 Yuborish #{t.id}", callback_data=f"task_send:{t.id}")
                builder.button(text=f"❌ Bekor #{t.id}", callback_data=f"task_cancel:{t.id}")

            if len(tasks) >= 2:
                builder.button(text="🛑 Barchasini bekor qilish", callback_data="task_bulk:cancel_all")

            builder.button(text="✍️ Yangi vazifa qo'shish", callback_data="schedule:wizard")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_pattern = [2] * len(tasks)
            if len(tasks) >= 2:
                adjust_pattern.append(1)
            adjust_pattern.extend([1, 1])
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
                mode="auto",
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
                f"⚙️ <b>Avtonomiya rejimi:</b> ⚡ <b>auto (To'liq avtomat / AI bevosita javob beradi)</b>\n"
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


        @dp.message(ReplyState.waiting_for_draft_edit)
        async def process_draft_edit_message(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return

            if message.text and message.text.strip().lower() == "/cancel":
                await state.clear()
                return await safe_reply(message, "🛑 Qoralamani tahrirlash bekor qilindi.")

            data = await state.get_data()
            created_ts = data.get("created_ts", 0)
            if datetime.now().timestamp() - created_ts > 1800:
                await state.clear()
                return await safe_reply(message, "⚠️ Tahrirlash vaqti tugadi (30 daqiqa). Qaytadan urinib ko'ring.")

            draft_id = data.get("draft_id")
            if not draft_id:
                await state.clear()
                return await safe_reply(message, "⚠️ Qoralama ID topilmadi. Qaytadan urinib ko'ring.")

            card_chat_id = data.get("card_chat_id")
            card_msg_id = data.get("card_msg_id")

            await self._execute_draft_edit_and_send(
                message=message,
                draft_id=draft_id,
                new_text=message.text or "",
                state=state,
                card_chat_id=card_chat_id,
                card_msg_id=card_msg_id,
            )

        # ------------------ PERSISTENT REPLY KEYBOARD DISPATCHERS ------------------
        @dp.message(F.text.in_(["🧠 Xotira", "Xotira"]))
        async def handle_btn_memory(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_memory_panel(message, is_callback=False)

        @dp.message(F.text.in_(["👥 Kontaktlar", "Kontaktlar", "CRM", "Kontaktlar (CRM)"]))
        async def handle_btn_contacts(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_contacts_list(message, is_callback=False)

        @dp.message(F.text.in_(["📝 Qoralamalar", "Qoralamalar", "Drafts"]))
        async def handle_btn_drafts(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_drafts_list(message, is_callback=False)

        @dp.message(F.text.in_(["⏰ Vazifalar", "Vazifalar", "⏰ Rejadagi vazifalar", "Rejalar"]))
        async def handle_btn_tasks(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_tasks_list(message, is_callback=False)

        @dp.message(F.text.in_(["📰 Executive Digest", "Executive Digest", "Digest", "📰 Dayjest", "Dayjest"]))
        async def handle_btn_digest(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _execute_digest(message, is_callback=False)

        @dp.message(F.text.in_(["⚙️ Avtonomiya (Mode)", "Avtonomiya", "Mode", "⚙️ Rejimlar", "Rejimlar"]))
        async def handle_btn_mode(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await _render_mode_panel(message, is_callback=False)

        @dp.message(F.text.in_(["⚡ Tizim holati", "Tizim holati", "Status", "⚡ Holat", "Holat"]))
        async def handle_btn_status(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await cmd_status(message)

        @dp.message(F.text.in_(["❓ Yordam", "Yordam", "Help", "🏠 Bosh menyu", "Bosh menyu", "🏠 Bosh sahifa", "Bosh sahifa", "Asosiy menyu", "Cockpit"]))
        async def handle_btn_help(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            await cmd_start(message)

        async def _handle_natural_contact_action(message: types.Message) -> bool:
            """
            Parses natural language commands targeting contacts without requiring '@'.
            Recognizes:
            - Tone: 'Alisherni senla', 'Dilshodni sizla', 'Farruxni sensirab gaplash'
            - Mode: 'Alisherni auto qil', 'Dilshodni draft rejimiga o'tkaz', 'Murodni mute qil'
            - Info/Memory: 'Alisher haqida', 'Alisher kartasi', 'Alisher xotirasi'
            """
            raw_text = (message.text or "").strip()
            if not raw_text:
                return False

            contacts = await self.db_manager.list_contacts()
            if not contacts:
                return False

            lower_text = raw_text.lower()

            # Find matching contact from text
            matched_contact = None
            for c in contacts:
                keys = []
                if c.username:
                    keys.append(c.username.lower().lstrip("@"))
                if c.full_name:
                    parts = [p.lower() for p in re.split(r"[\s_]+", c.full_name) if len(p) >= 3]
                    keys.extend(parts)
                keys.append(str(c.user_id))

                for k in keys:
                    if re.search(rf"(?i)\b{re.escape(k)}(?:ni|ga|da|dan|ning)?\b", lower_text):
                        matched_contact = c
                        break
                if matched_contact:
                    break

            if not matched_contact:
                return False

            # Check 1: Tone/Style intent (senla / sizla)
            is_sen = any(w in lower_text for w in ["senla", "sensira", "sensirab", "sen deb", " sen ", "sen bo'l", "sen bol", "dost", "do'st"])
            is_siz = any(w in lower_text for w in ["sizla", "sizlab", "siz deb", " siz ", "hurmat", "rasmiy"])

            if is_sen and not is_siz:
                await self.db_manager.update_addressing_style(matched_contact.user_id, "sen")
                display_name = matched_contact.full_name or matched_contact.username or str(matched_contact.user_id)
                await safe_reply(
                    message,
                    f"✅ <b>{html.escape(display_name)}</b> endi 🟢 <b>SEN (Sensirash)</b> deb muloqot qilinadi!",
                    parse_mode="HTML",
                )
                updated_c = await self.db_manager.get_contact_by_id(matched_contact.user_id)
                await _render_unified_contact_card(message, updated_c, is_callback=False)
                return True

            if is_siz and not is_sen:
                await self.db_manager.update_addressing_style(matched_contact.user_id, "siz")
                display_name = matched_contact.full_name or matched_contact.username or str(matched_contact.user_id)
                await safe_reply(
                    message,
                    f"✅ <b>{html.escape(display_name)}</b> endi 🔵 <b>SIZ (Sizlash)</b> deb muloqot qilinadi!",
                    parse_mode="HTML",
                )
                updated_c = await self.db_manager.get_contact_by_id(matched_contact.user_id)
                await _render_unified_contact_card(message, updated_c, is_callback=False)
                return True

            # Check 2: Mode intent (auto / draft / mute)
            is_auto = any(w in lower_text for w in ["auto", "avto", "avtomatik"])
            is_draft = any(w in lower_text for w in ["draft", "qoralama", "tasdiq"])
            is_mute = any(w in lower_text for w in ["mute", "ovozsiz", "o'chir", "ochir", "jim"])

            target_mode = None
            if is_auto and not is_draft and not is_mute:
                target_mode = "auto"
            elif is_draft and not is_auto and not is_mute:
                target_mode = "draft"
            elif is_mute and not is_auto and not is_draft:
                target_mode = "mute"

            if target_mode:
                await self.db_manager.update_contact_mode(matched_contact.user_id, target_mode)
                labels = {
                    "auto": "⚡ auto (Avtomatik javob)",
                    "draft": "📝 draft (Qoralama / Tasdiq bilan)",
                    "mute": "🔇 mute (Ovozsiz)",
                }
                display_name = matched_contact.full_name or matched_contact.username or str(matched_contact.user_id)
                await safe_reply(
                    message,
                    f"✅ <b>{html.escape(display_name)}</b> avtonomiya rejimi <b>{labels.get(target_mode, target_mode)}</b> ga o'zgartirildi!",
                    parse_mode="HTML",
                )
                updated_c = await self.db_manager.get_contact_by_id(matched_contact.user_id)
                await _render_unified_contact_card(message, updated_c, is_callback=False)
                return True

            # Check 3: Fact intent (haqida fakt / yoqtiradi / qiziqadi / eslab qol)
            fact_match = re.search(
                r"(?:haqida\s+fakt|haqida\s+ma'lumot|haqida\s+eslab\s+qol|fakt|yoqtiradi|qiziqadi):?\s*(.+)",
                raw_text,
                re.IGNORECASE,
            )
            if fact_match:
                fact_text = fact_match.group(1).strip()
                if fact_text:
                    category = "interest" if any(w in lower_text for w in ["yoqtiradi", "qiziqadi"]) else "preference"
                    is_forb, reason = is_forbidden_topic(fact_text, category)
                    if is_forb:
                        await safe_reply(
                            message,
                            f"⚠️ <b>Taqiqlangan mavzu:</b> Fakt saqlanmadi ({html.escape(reason or 'maxfiy')}).",
                            parse_mode="HTML",
                        )
                        return True

                    fact_id = await self.db_manager.create_mem_fact(
                        contact_user_id=matched_contact.user_id,
                        category=category,
                        text=fact_text,
                        source_type="owner_confirmed",
                        source_message_ids=[-1],
                        confidence=1.0,
                    )
                    display_name = matched_contact.full_name or matched_contact.username or str(matched_contact.user_id)
                    await safe_reply(
                        message,
                        f"💡 <b>Yangi fakt muhrlandi! (#{fact_id})</b>\n\n"
                        f"👤 <b>Kontakt:</b> {html.escape(display_name)}\n"
                        f"📝 <b>Fakt:</b> <i>\"{html.escape(fact_text)}\"</i>",
                        parse_mode="HTML",
                    )
                    updated_c = await self.db_manager.get_contact_by_id(matched_contact.user_id)
                    await _render_unified_contact_card(message, updated_c, is_callback=False)
                    return True

            # Check 4: Card / Memory query intent
            is_card_query = any(w in lower_text for w in ["kartasi", "karta", "xotirasi", "xotira", "haqida", "profil", "kim bu"])
            if is_card_query:
                await _render_unified_contact_card(message, matched_contact, is_callback=False)
                return True

            return False

        @dp.message(StateFilter(None), F.text & ~F.text.startswith("/"))
        async def handle_fallback_natural_text(message: types.Message, state: FSMContext):
            if not self._is_admin(message.from_user.id):
                return
            current_state = await state.get_state()
            if current_state:
                return

            # BULLETPROOF FALLBACK: Check if this message is a direct reply to a draft prompt or draft card
            if message.reply_to_message:
                reply_txt = message.reply_to_message.text or message.reply_to_message.caption or ""
                m = re.search(r"(?:qoralama\s*(?:javob\s*)?\(?#|#)(\d+)", reply_txt, re.IGNORECASE)
                if m:
                    extracted_draft_id = int(m.group(1))
                    d = await self.db_manager.get_draft_by_id(extracted_draft_id)
                    if d and d.is_pending:
                        card_chat_id = message.reply_to_message.chat.id
                        card_msg_id = message.reply_to_message.message_id
                        return await self._execute_draft_edit_and_send(
                            message=message,
                            draft_id=extracted_draft_id,
                            new_text=message.text or "",
                            state=state,
                            card_chat_id=card_chat_id,
                            card_msg_id=card_msg_id,
                        )

            # 0. Check if this is a confirmation for pending outreach (e.g. 'yubor', 'ha', 'jo'nat')
            if await _handle_pending_outreach_confirmation(message):
                return

            # 0.1 Check for natural text task cancellation or execution (e.g. 'bekor #17', 'bekor 17', 'yubor #17')
            raw_t = (message.text or "").strip()
            m_cancel = re.match(r"(?i)^(?:bekor\s+(?:qil\s+)?|cancel\s+)#?(\d+)$", raw_t)
            if m_cancel:
                tid = int(m_cancel.group(1))
                task = await self.db_manager.get_task_by_id(tid)
                if task:
                    await self.scheduler_service.cancel_task(tid)
                    return await safe_reply(message, f"🗑 <b>Vazifa #{tid} bekor qilindi.</b>", parse_mode="HTML", reply_markup=None)
                else:
                    return await safe_reply(message, f"ℹ️ <b>Vazifa #{tid} topilmadi yoki allaqachon bekor qilingan.</b>", parse_mode="HTML", reply_markup=None)

            m_send_now = re.match(r"(?i)^(?:yubor\s+|send\s+)#?(\d+)$", raw_t)
            if m_send_now:
                tid = int(m_send_now.group(1))
                success = await self.scheduler_service.execute_now(tid)
                if success:
                    return await safe_reply(message, f"🚀 <b>Vazifa #{tid} darhol yuborildi!</b>", parse_mode="HTML", reply_markup=None)
                else:
                    return await safe_reply(message, f"❌ <b>Vazifa #{tid} topilmadi yoki allaqachon yuborilgan.</b>", parse_mode="HTML", reply_markup=None)

            # 0.2 Check if this is a contact inquiry or outreach request BEFORE scheduling
            handled_inquiry = await _handle_contact_inquiry_or_outreach(message)
            if handled_inquiry:
                return

            handled = await _schedule_and_respond(message, message.text)
            if handled:
                return

            handled_digest = await _handle_natural_digest_action(message)
            if handled_digest:
                return

            handled_contact = await _handle_natural_contact_action(message)
            if handled_contact:
                return

            lower_msg = (message.text or "").strip().lower()

            # 1. Bulk Drafts Actions
            if any(p in lower_msg for p in ["hammasini tasdiqla", "barchasini tasdiqla", "barcha qoralamalarni yubor", "hammasini yubor", "qoralamalarni tasdiqla"]):
                drafts = await self.db_manager.list_pending_drafts()
                if not drafts:
                    await safe_reply(message, "📝 <b>Kutilayotgan qoralamalar mavjud emas.</b>", parse_mode="HTML")
                    return
                sent_count = 0
                for d in drafts:
                    ok, _ = await _execute_single_draft_send(d.id)
                    if ok:
                        sent_count += 1
                await safe_reply(message, f"🚀 <b>Barcha {sent_count} ta qoralama muvaffaqiyatli yuborildi!</b>", parse_mode="HTML")
                return

            if any(p in lower_msg for p in ["hammasini rad et", "barchasini rad et", "qoralamalarni rad et", "hammasini bekor qil"]):
                drafts = await self.db_manager.list_pending_drafts()
                if not drafts:
                    await safe_reply(message, "📝 <b>Kutilayotgan qoralamalar mavjud emas.</b>", parse_mode="HTML")
                    return
                rej_count = 0
                for d in drafts:
                    ok = await self.db_manager.resolve_draft_atomic(d.id, "rejected")
                    if ok:
                        rej_count += 1
                await safe_reply(message, f"❌ <b>Barcha {rej_count} ta qoralama rad etildi.</b>", parse_mode="HTML")
                return

            # 2. Tasks Actions: Vazifa #ID ni bekor qil / yubor, yoki barcha vazifalarni bekor qil
            if any(p in lower_msg for p in ["barcha vazifalarni bekor qil", "hamma rejalarni bekor qil"]):
                tasks = await self.db_manager.get_pending_tasks()
                for t in tasks:
                    await self.scheduler_service.cancel_task(t.id)
                await safe_reply(message, f"🛑 <b>Barcha {len(tasks)} ta rejalashtirilgan vazifa bekor qilindi.</b>", parse_mode="HTML")
                return

            task_cmd_match = re.search(r"(?:vazifa\s*#?|vazifani\s*#?)(\d+)\s*(?:ni\s*)?(bekor\s*qil|hozir\s*yubor|yubor)", lower_msg)
            if task_cmd_match:
                t_id = int(task_cmd_match.group(1))
                action = task_cmd_match.group(2)
                if "bekor" in action:
                    await self.scheduler_service.cancel_task(t_id)
                    await safe_reply(message, f"🛑 <b>#{t_id} vazifa bekor qilindi.</b>", parse_mode="HTML")
                    return
                else:
                    success = await self.scheduler_service.execute_now(t_id)
                    if success:
                        await safe_reply(message, f"🚀 <b>#{t_id} vazifa darhol yuborildi!</b>", parse_mode="HTML")
                    else:
                        await safe_reply(message, f"❌ <b>#{t_id} vazifa topilmadi yoki allaqachon bajarilgan.</b>", parse_mode="HTML")
                    return

            # 3. Single Draft Action: #ID ni yubor / bekor qil (e.g. qoralama #1 ni yubor or #1 ni yubor)
            draft_single_match = re.search(r"(?:qoralama\s*#?|#)(\d+)\s*(?:ni\s*)?(yubor|tasdiqla|bekor\s*qil|rad\s*et)", lower_msg)
            if draft_single_match:
                d_id = int(draft_single_match.group(1))
                act = draft_single_match.group(2)
                if any(w in act for w in ["yubor", "tasdiqla"]):
                    ok, res_msg = await _execute_single_draft_send(d_id)
                    if ok:
                        await safe_reply(message, f"✅ <b>Qoralama #{d_id} muvaffaqiyatli yuborildi!</b>", parse_mode="HTML")
                    else:
                        await safe_reply(message, f"⚠️ <b>#{d_id}:</b> {res_msg}", parse_mode="HTML")
                    return
                else:
                    ok = await self.db_manager.resolve_draft_atomic(d_id, "rejected")
                    if ok:
                        await safe_reply(message, f"❌ <b>Qoralama #{d_id} rad etildi.</b>", parse_mode="HTML")
                    else:
                        await safe_reply(message, f"⚠️ <b>#{d_id}</b> topilmadi yoki allaqachon bajarilgan.", parse_mode="HTML")
                    return

            # 4. Digest and Status Quick Triggers
            if lower_msg in ["dayjest", "dayjestni ko'rsat", "dayjestni korsat", "bugungi dayjest"]:
                await _execute_digest(message, is_callback=False)
                return

            if lower_msg in ["status", "tizim holati", "bot holati", "tizim"]:
                await cmd_status(message)
                return

            text = (
                "👑 <b>Xo'jayin, buyruq qabul qilindi.</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Menga istalgan tabiiy tilda topshiriq bera olasiz:\n\n"
                "• <b>Vazifa rejalashtirish (AI):</b> <i>\"Ertaga soat 10:00 da @ali ga loyiha tayyor deb yoz\"</i>\n"
                "• <b>Muloqot uslubini sozlash:</b> <i>\"@dilshod ni sizla, u muhim hamkor\"</i>\n"
                "• <b>Avtonomiya rejimi:</b> <i>\"@bobur ni auto rejimiga o'tkaz\"</i>\n"
                "• <b>Boshqaruv markazi:</b> Pastdagi menyu tugmalaridan foydalaning\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━"
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
            elif action == "debug":
                await callback.answer()
                await _render_digest_debug(callback.message, page=0, is_callback=True)
            elif action == "stats":
                await callback.answer()
                await _render_digest_stats(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("digest_debug:page:"))
        async def handle_digest_debug_page(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer()
            try:
                page_str = callback.data.split(":")[2]
                page = int(page_str)
            except Exception:
                page = 0
            await _render_digest_debug(callback.message, page=page, is_callback=True)

        @dp.callback_query(F.data == "noop")
        async def handle_noop(callback: types.CallbackQuery):
            await callback.answer()

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
                await _render_dashboard(callback.message, is_callback=True)
            elif action == "memory":
                await callback.answer()
                await _render_memory_panel(callback.message, is_callback=True)
            elif action == "mode":
                await callback.answer()
                await _render_mode_panel(callback.message, is_callback=True)
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
                mem_stats = await self.db_manager.get_overall_mem_stats()

                status_text = (
                    "⚡ <b>ANTIGRAVITY BUTLER — TIZIM HOLATI</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🟢 <b>Holat:</b> Barcha xizmatlar 100% faol va barqaror\n"
                    f"🤖 <b>AI Modeli:</b> <code>{model_str}</code>\n"
                    f"⚡ <b>Avto-javob:</b> <code>{auto_reply_str}</code>\n"
                    f"⏰ <b>Vaqt mintaqasi:</b> <code>{tz_str}</code>\n"
                    f"📱 <b>Telegram Profil:</b> <code>{phone_str}</code>\n"
                    "🛡️ <b>Kontaktlar filtri:</b> 🟢 Faol (Qat'iy izolyatsiya)\n\n"
                    "📊 <b>AVTONOMIYA VA MA'LUMOTLAR:</b>\n"
                    f"├ 👥 Jami kontaktlar: <b>{total_contacts} ta</b>\n"
                    f"│  ├ ⚡ Auto: <b>{mode_counts.get('auto', 0)} ta</b>\n"
                    f"│  ├ 📝 Draft: <b>{mode_counts.get('draft', 0)} ta</b>\n"
                    f"│  └ 🔇 Mute: <b>{mode_counts.get('mute', 0)} ta</b>\n"
                    f"├ 📝 Kutilayotgan qoralamalar: <b>{pending_drafts} ta</b>\n"
                    f"└ 🧠 Xotira faktlari: <b>{mem_stats.get('facts', 0)} ta</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔄 Yangilash", callback_data="menu:status")
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(2)
                await callback.message.edit_text(status_text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data == "crm:add_prompt")
        async def handle_crm_add_prompt(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer()
            help_text = (
                "➕ <b>YANGI KONTAKT QO'SHISH</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "AI bilan muloqot qilishi uchun yangi kontakt qo'shish juda oson.\n\n"
                "💡 <b>1-usul: Tabiiy tilda yozing:</b>\n"
                "• <i>\"@alisher_dev ni do'stim sifatida qo'sh va senla\"</i>\n"
                "• <i>\"@dilshod_aka ni hurmatli mijoz sifatida qo'sh, sizla\"</i>\n\n"
                "💡 <b>2-usul: Buyruq orqali:</b>\n"
                "• <code>/crm @username siz [ko'rsatma]</code>\n"
                "• <code>/crm @username sen [ko'rsatma]</code>\n\n"
                "<i>Kontakt qo'shilgach, u avtomatik ravishda CRM va Xotira tizimiga kiritiladi.</i>"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="👥 Kontaktlar ro'yxati", callback_data="menu:contacts")
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            builder.adjust(1, 1)
            await safe_edit(callback.message, help_text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("crm_style:"))
        async def handle_crm_style_toggle(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            parts = callback.data.split(":")
            username = parts[1]
            new_style = parts[2]

            contact = await self.db_manager.find_contact(username)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            await self.db_manager.update_addressing_style(contact.user_id, new_style)
            style_name = "SIZ (Sizlash / Hurmat)" if new_style == "siz" else "SEN (Sensirash / Do'st)"
            await callback.answer(f"✅ @{contact.username or username} endi {style_name} deb gaplashiladi!", show_alert=True)
            if "KONTAKT" in (callback.message.text or ""):
                updated_c = await self.db_manager.get_contact_by_id(contact.user_id)
                await _render_unified_contact_card(callback.message, updated_c, is_callback=True)
            else:
                await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("crm_del:"))
        async def handle_crm_delete(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            username = callback.data.split(":")[1]
            contact = await self.db_manager.get_contact_by_username(username)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            text = (
                f"⚠️ <b>@{username} ni CRM dan o'chirish:</b>\n\n"
                f"Ushbu kontakt xotirasi (yozishmalar, faktlar, aylanma xulosalar) ham to'liq o'chirilsinmi?\n\n"
                f"<i>Eslatma: Xotira o'chirilsa, uni qayta tiklab bo'lmaydi (arxiv DB zaxiralari o'zgartirilmaydi).</i>"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="🗑 Kontakt o'chirilsin (Xotira to'xtatiladi)", callback_data=f"crm_del_confirm:{username}:keep")
            builder.button(text="🔥 Hammasi o'chirilsin (Xotira ham tozalansin)", callback_data=f"crm_del_confirm:{username}:wipe")
            builder.button(text="❌ Bekor qilish", callback_data="crm:refresh")
            builder.adjust(1)
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("crm_del_confirm:"))
        async def handle_crm_delete_confirm(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            parts = callback.data.split(":")
            username = parts[1]
            action = parts[2]
            contact = await self.db_manager.get_contact_by_username(username)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            if action == "wipe":
                await self.db_manager.wipe_contact_memory(contact.user_id)
                await self.db_manager.delete_contact(contact.user_id)
                await callback.answer(f"🔥 @{username} va uning barcha xotiralari to'liq o'chirildi!", show_alert=True)
            else:
                # Default per H10: memory stays but disabled for that contact
                await self.db_manager.pause_mem_contact(contact.user_id, reason="contact_deleted_memory_retained")
                await self.db_manager.delete_contact(contact.user_id)
                await callback.answer(f"🗑 @{username} o'chirildi (xotira to'xtatildi)!", show_alert=True)

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

            contact = await self.db_manager.find_contact(target_user)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            if contact.mode == new_mode:
                return await callback.answer("Allaqachon ushbu rejimda.")

            await self.db_manager.update_contact_mode(contact.user_id, new_mode)

            mode_labels = {
                "auto": "⚡ auto (Avtomatik)",
                "draft": "📝 draft (Qoralama)",
                "mute": "🔇 mute (Ovozsiz)",
            }
            await callback.answer(f"✅ @{contact.username or target_user} rejimi {mode_labels.get(new_mode, new_mode)} ga o'zgartirildi!")
            await _render_contacts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("mode_set:"))
        async def handle_mode_set(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            parts = callback.data.split(":")
            user_id = int(parts[1])
            new_mode = parts[2]

            contact = await self.db_manager.get_contact_by_id(user_id)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)

            if contact.mode == new_mode:
                return await callback.answer("Allaqachon ushbu rejimda.")

            await self.db_manager.update_contact_mode(user_id, new_mode)
            labels = {
                "auto": "⚡ auto (Avtomatik)",
                "draft": "📝 draft (Qoralama)",
                "mute": "🔇 mute (Ovozsiz)",
            }
            display_name = f"@{contact.username}" if contact.username else contact.full_name
            await callback.answer(f"✅ {display_name} rejimi {labels.get(new_mode, new_mode)} ga o'zgartirildi!")

            # If inside contact card, refresh contact card in-place, else full panel
            if "KONTAKT" in (callback.message.text or ""):
                updated_c = await self.db_manager.get_contact_by_id(user_id)
                await _render_unified_contact_card(callback.message, updated_c, is_callback=True)
            else:
                await _render_mode_panel(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("mode_bulk:"))
        async def handle_mode_bulk(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            new_mode = callback.data.split(":")[1]
            count = await self.db_manager.update_all_contacts_mode(new_mode)
            labels = {
                "auto": "⚡ auto (Avtomatik)",
                "draft": "📝 draft (Qoralama)",
                "mute": "🔇 mute (Ovozsiz)",
            }
            await callback.answer(f"✅ Barcha {count} ta kontakt {labels.get(new_mode, new_mode)} ga o'tkazildi!", show_alert=True)
            await _render_mode_panel(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("mode_pick:"))
        async def handle_mode_pick(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            user_id = int(callback.data.split(":")[1])
            contact = await self.db_manager.get_contact_by_id(user_id)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)
            await callback.answer()
            await _render_single_contact_mode(callback.message, contact, is_callback=True)

        @dp.callback_query(F.data == "mode:refresh")
        async def handle_mode_refresh(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            await callback.answer("Rejimlar yangilandi")
            await _render_mode_panel(callback.message, is_callback=True)

        # ------------------ CONTACT MEMORY MANAGEMENT (PHASE 6) ------------------
        async def _resolve_target_contact(target_str: str) -> Optional[Any]:
            target_str = target_str.strip()
            if not target_str:
                return None
            return await self.db_manager.find_contact(target_str)

        @dp.message(Command("memory"))
        async def cmd_memory(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            args = message.text[len("/memory"):].strip()
            if not args or args.lower() == "help":
                return await _render_memory_panel(message, is_callback=False)

            if args.lower() == "stats":
                stats = await self.db_manager.get_overall_mem_stats()
                embed_count = stats.get("embeddings", 0)
                embed_str = f"<code>{embed_count}</code> ta" if embed_count > 0 else "o'chiq (FTS ishlaydi)"
                text = (
                    "🧠 <b>UMUMIY XOTIRA STATISTIKASI:</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Xotirasi faol kontaktlar:</b> <code>{stats.get('contacts_enabled', 0)}</code> ta\n"
                    f"• <b>Jami xabarlar (mem_messages):</b> <code>{stats.get('messages', 0)}</code> ta\n"
                    f"• <b>Faol faktlar (mem_facts):</b> <code>{stats.get('facts', 0)}</code> ta\n"
                    f"• <b>Ochiq vazifalar (mem_open_items):</b> <code>{stats.get('open_items', 0)}</code> ta\n"
                    f"• <b>Aylanma xulosalar (mem_summaries):</b> <code>{stats.get('summaries', 0)}</code> ta\n"
                    f"• <b>Vektorlar (mem_embeddings):</b> {embed_str}\n"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                return await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

            contact = await _resolve_target_contact(args)
            if not contact:
                return await safe_reply(
                    message,
                    f"❌ <b>'{html.escape(args)}'</b> nomli kontakt /contacts ro'yxatida topilmadi.",
                    parse_mode="HTML",
                )

            await _render_unified_contact_card(message, contact, is_callback=False)

        @dp.message(Command("remember"))
        async def cmd_remember(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            args = message.text[len("/remember"):].strip()
            if not args:
                return await safe_reply(
                    message,
                    "ℹ️ <b>Faktni qo'lda kiritish (Tasdiqlangan):</b>\n\n"
                    "Format: <code>/remember @username [kategoriya] Fakt matni</code>\n"
                    "Misol: <code>/remember @farrux preference Ertalab doim qahva ichadi</code>\n\n"
                    "💡 <b>Mavjud kategoriyalar:</b>\n"
                    "<code>identity</code>, <code>preference</code>, <code>interest</code>, <code>important_date</code>, <code>relationship_note</code>, <code>ongoing_topic</code>, <code>other</code>",
                    parse_mode="HTML",
                )

            parts = args.split(maxsplit=2)
            target = parts[0]
            contact = await _resolve_target_contact(target)
            if not contact:
                return await safe_reply(
                    message,
                    f"❌ Kontakt <b>'{html.escape(target)}'</b> topilmadi.",
                    parse_mode="HTML",
                )

            if len(parts) < 2:
                return await safe_reply(
                    message,
                    "❌ Fakt matni kiritilmadi.\nFormat: <code>/remember @username [kategoriya] Fakt matni</code>",
                    parse_mode="HTML",
                )

            category = "other"
            if len(parts) >= 3:
                cand = parts[1].lower()
                if cand in CANONICAL_CATEGORIES or cand in CATEGORY_SYNONYMS:
                    category = normalize_category(cand)
                    fact_text = parts[2].strip()
                else:
                    fact_text = args[len(target):].strip()
            else:
                fact_text = parts[1].strip()

            if not fact_text:
                return await safe_reply(message, "❌ Fakt matni bo'sh bo'lishi mumkin emas.", parse_mode="HTML")

            is_forbidden, reason = is_forbidden_topic(fact_text, category)
            if is_forbidden:
                reason_label = reason or "maxfiy ma'lumot"
                return await safe_reply(
                    message,
                    f"⚠️ <b>Taqiqlangan mavzu:</b> Ushbu fakt xotiraga saqlanmadi (sabab: {html.escape(reason_label)}).",
                    parse_mode="HTML",
                )

            fact_id = await self.db_manager.create_mem_fact(
                contact_user_id=contact.user_id,
                category=category,
                text=fact_text,
                source_type="owner_confirmed",
                source_message_ids=[-1],
                confidence=1.0,
            )

            await safe_reply(
                message,
                f"✅ <b>Fakt muvaffaqiyatli saqlandi!</b>\n\n"
                f"• <b>Kontakt:</b> {html.escape(contact.full_name)} (<code>@{contact.username or contact.user_id}</code>)\n"
                f"• <b>Fakt #{fact_id}</b> [<code>{html.escape(category)}</code>]:\n  {html.escape(fact_text)}\n"
                f"• <b>Manba:</b> egasi tasdiqlagan (confidence: 1.0)",
                parse_mode="HTML",
            )

        @dp.message(Command("memory_facts"))
        async def cmd_memory_facts(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            args = message.text[len("/memory_facts"):].strip()
            if not args:
                return await safe_reply(
                    message,
                    "ℹ️ <b>Faktlarni boshqarish:</b>\n"
                    "Format: <code>/memory_facts @username</code> yoki <code>/memory_facts &lt;user_id&gt;</code>",
                    parse_mode="HTML",
                )
            contact = await _resolve_target_contact(args)
            if not contact:
                return await safe_reply(message, f"❌ Kontakt <b>'{html.escape(args)}'</b> topilmadi.", parse_mode="HTML")

            facts = await self.db_manager.get_mem_facts(contact.user_id, status="active")
            if not facts:
                return await safe_reply(
                    message,
                    f"ℹ️ <b>{html.escape(contact.full_name)}</b> uchun saqlangan faol faktlar mavjud emas.",
                    parse_mode="HTML",
                )

            builder = InlineKeyboardBuilder()
            adjust_layout = []
            lines = [f"📋 <b>FAKTLAR RO'YXATI:</b> {html.escape(contact.full_name)}\n━━━━━━━━━━━━━━━━━━━━"]
            for f in facts[:10]:
                src_label = "kontakt da'vosi" if f.source_type == "contact_claim" else ("egasi tasdiqlagan" if f.source_type == "owner_confirmed" else "egasi ta'kidlagan")
                lines.append(f"• <b>#{f.id}</b> [{html.escape(f.category or 'umumiy')}] ({src_label}):\n  {html.escape(f.text)}")
                if f.source_type != "owner_confirmed":
                    builder.button(text=f"✅ #{f.id} Tasdiqlash", callback_data=f"mem_fact_confirm:{contact.user_id}:{f.id}")
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(2)
                else:
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(1)

            builder.button(text="🔙 Xotiraga qaytish", callback_data=f"mem_back:{contact.user_id}")
            adjust_layout.append(1)
            builder.adjust(*adjust_layout)

            await safe_reply(message, "\n\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.message(Command("forget"))
        async def cmd_forget(message: types.Message):
            if not self._is_admin(message.from_user.id):
                return
            args = message.text[len("/forget"):].strip()
            if not args:
                return await safe_reply(
                    message,
                    "ℹ️ <b>Xotirani to'liq tozalash:</b>\n"
                    "Format: <code>/forget @username</code> yoki <code>/forget &lt;user_id&gt;</code>",
                    parse_mode="HTML",
                )
            contact = await _resolve_target_contact(args)
            if not contact:
                return await safe_reply(message, f"❌ Kontakt <b>'{html.escape(args)}'</b> topilmadi.", parse_mode="HTML")

            text = (
                f"⚠️ <b>DIQQAT: Xotirani butunlay o'chirish tasdig'i!</b>\n\n"
                f"Siz <b>{html.escape(contact.full_name)}</b> (<code>@{contact.username or contact.user_id}</code>) "
                f"haqidagi barcha xotiralarni to'liq o'chirmoqchimisiz?\n\n"
                f"• Barcha yozishmalar va xabarlar o'chiriladi\n"
                f"• Barcha faktlar va ochiq vazifalar o'chiriladi\n"
                f"• Barcha aylanma xulosalar va vektorlar o'chiriladi\n"
                f"• Qoralamalar jadvalidagi matnlar bo'shatiladi\n\n"
                f"<i>Eslatma: Ushbu amal qaytarilmasdir. Eski DB zaxiralari o'zgartirilmaydi.</i>"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="🔥 Ha, xotira butunlay tozalansin", callback_data=f"mem_forget_confirm:{contact.user_id}")
            builder.button(text="❌ Bekor qilish", callback_data="crm:refresh")
            builder.adjust(1)

            await safe_reply(message, text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("mem_facts_view:"))
        async def handle_mem_facts_view(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            contact_user_id = int(callback.data.split(":")[1])
            contact = await self.db_manager.get_contact_by_id(contact_user_id)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)
            facts = await self.db_manager.get_mem_facts(contact_user_id, status="active")
            if not facts:
                return await callback.answer("Faol faktlar mavjud emas.", show_alert=True)

            builder = InlineKeyboardBuilder()
            adjust_layout = []
            lines = [f"📋 <b>FAKTLAR RO'YXATI:</b> {html.escape(contact.full_name)}\n━━━━━━━━━━━━━━━━━━━━"]
            for f in facts[:10]:
                src_label = "kontakt da'vosi" if f.source_type == "contact_claim" else ("egasi tasdiqlagan" if f.source_type == "owner_confirmed" else "egasi ta'kidlagan")
                lines.append(f"• <b>#{f.id}</b> [{html.escape(f.category or 'umumiy')}] ({src_label}):\n  {html.escape(f.text)}")
                if f.source_type != "owner_confirmed":
                    builder.button(text=f"✅ #{f.id} Tasdiqlash", callback_data=f"mem_fact_confirm:{contact.user_id}:{f.id}")
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(2)
                else:
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(1)

            builder.button(text="🔙 Xotiraga qaytish", callback_data=f"mem_back:{contact.user_id}")
            adjust_layout.append(1)
            builder.adjust(*adjust_layout)
            await callback.message.edit_text("\n\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("mem_fact_confirm:"))
        async def handle_mem_fact_confirm(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            parts = callback.data.split(":")
            contact_user_id = int(parts[1])
            fact_id = int(parts[2])
            ok = await self.db_manager.confirm_mem_fact(contact_user_id, fact_id)
            if ok:
                await callback.answer(f"✅ #{fact_id} fakt tasdiqlandi!", show_alert=True)
            else:
                await callback.answer("Fakt topilmadi yoki allaqachon tasdiqlangan.", show_alert=True)

            contact = await self.db_manager.get_contact_by_id(contact_user_id)
            if not contact:
                return await callback.message.edit_text("Kontakt topilmadi.")
            facts = await self.db_manager.get_mem_facts(contact_user_id, status="active")
            if not facts:
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Xotiraga qaytish", callback_data=f"mem_back:{contact_user_id}")
                await callback.message.edit_text("ℹ️ Faol faktlar qolmadi.", reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            builder = InlineKeyboardBuilder()
            adjust_layout = []
            lines = [f"📋 <b>FAKTLAR RO'YXATI:</b> {html.escape(contact.full_name)}\n━━━━━━━━━━━━━━━━━━━━"]
            for f in facts[:10]:
                src_label = "kontakt da'vosi" if f.source_type == "contact_claim" else ("egasi tasdiqlagan" if f.source_type == "owner_confirmed" else "egasi ta'kidlagan")
                lines.append(f"• <b>#{f.id}</b> [{html.escape(f.category or 'umumiy')}] ({src_label}):\n  {html.escape(f.text)}")
                if f.source_type != "owner_confirmed":
                    builder.button(text=f"✅ #{f.id} Tasdiqlash", callback_data=f"mem_fact_confirm:{contact.user_id}:{f.id}")
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(2)
                else:
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(1)

            builder.button(text="🔙 Xotiraga qaytish", callback_data=f"mem_back:{contact.user_id}")
            adjust_layout.append(1)
            builder.adjust(*adjust_layout)
            await callback.message.edit_text("\n\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("mem_fact_del:"))
        async def handle_mem_fact_del(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            parts = callback.data.split(":")
            contact_user_id = int(parts[1])
            fact_id = int(parts[2])
            ok = await self.db_manager.delete_mem_fact(contact_user_id, fact_id)
            if ok:
                await callback.answer(f"🗑 #{fact_id} fakt o'chirildi!", show_alert=True)
            else:
                await callback.answer("Fakt topilmadi yoki allaqachon o'chirilgan.", show_alert=True)

            contact = await self.db_manager.get_contact_by_id(contact_user_id)
            if not contact:
                return await callback.message.edit_text("Kontakt topilmadi.")
            facts = await self.db_manager.get_mem_facts(contact_user_id, status="active")
            if not facts:
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Xotiraga qaytish", callback_data=f"mem_back:{contact_user_id}")
                await callback.message.edit_text("ℹ️ Faol faktlar qolmadi.", reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            builder = InlineKeyboardBuilder()
            adjust_layout = []
            lines = [f"📋 <b>FAKTLAR RO'YXATI:</b> {html.escape(contact.full_name)}\n━━━━━━━━━━━━━━━━━━━━"]
            for f in facts[:10]:
                src_label = "kontakt da'vosi" if f.source_type == "contact_claim" else ("egasi tasdiqlagan" if f.source_type == "owner_confirmed" else "egasi ta'kidlagan")
                lines.append(f"• <b>#{f.id}</b> [{html.escape(f.category or 'umumiy')}] ({src_label}):\n  {html.escape(f.text)}")
                if f.source_type != "owner_confirmed":
                    builder.button(text=f"✅ #{f.id} Tasdiqlash", callback_data=f"mem_fact_confirm:{contact.user_id}:{f.id}")
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(2)
                else:
                    builder.button(text=f"🗑 #{f.id} O'chirish", callback_data=f"mem_fact_del:{contact.user_id}:{f.id}")
                    adjust_layout.append(1)

            builder.button(text="🔙 Xotiraga qaytish", callback_data=f"mem_back:{contact.user_id}")
            adjust_layout.append(1)
            builder.adjust(*adjust_layout)
            await callback.message.edit_text("\n\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("mem_forget_prompt:"))
        async def handle_mem_forget_prompt(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            user_id = int(callback.data.split(":")[1])
            contact = await self.db_manager.get_contact_by_id(user_id)
            name = contact.full_name if contact else str(user_id)
            text = (
                f"⚠️ <b>DIQQAT: Xotirani butunlay o'chirish tasdig'i!</b>\n\n"
                f"Siz <b>{html.escape(name)}</b> haqidagi barcha xotiralarni to'liq tozalashni tasdiqlaysizmi?\n\n"
                f"<i>Eslatma: Ushbu amal qaytarilmasdir. Eski DB zaxiralari o'zgartirilmaydi.</i>"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="🔥 Ha, xotira butunlay tozalansin", callback_data=f"mem_forget_confirm:{user_id}")
            builder.button(text="❌ Bekor qilish", callback_data=f"mem_back:{user_id}")
            builder.adjust(1)
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("mem_forget_confirm:"))
        async def handle_mem_forget_confirm(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            user_id = int(callback.data.split(":")[1])
            counts = await self.db_manager.wipe_contact_memory(user_id)
            report = (
                f"✅ <b>Xotira to'liq o'chirildi!</b>\n\n"
                f"• O'chirilgan xabarlar: <code>{counts.get('messages', 0)}</code> ta\n"
                f"• O'chirilgan faktlar: <code>{counts.get('facts', 0)}</code> ta\n"
                f"• O'chirilgan xulosalar: <code>{counts.get('summaries', 0)}</code> ta\n"
                f"• O'chirilgan vektorlar: <code>{counts.get('embeddings', 0)}</code> ta\n"
                f"• Tozalangan qoralamalar: <code>{counts.get('drafts_blanked', 0)}</code> ta\n\n"
                f"<i>Eslatma: Eski DB zaxiralari o'zgartirilmaydi.</i>"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
            await callback.message.edit_text(report, reply_markup=builder.as_markup(), parse_mode="HTML")

        @dp.callback_query(F.data.startswith("mem_back:"))
        async def handle_mem_back(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            user_id = int(callback.data.split(":")[1])
            contact = await self.db_manager.get_contact_by_id(user_id)
            if not contact:
                return await callback.answer("Kontakt topilmadi.", show_alert=True)
            await callback.answer()
            await _render_unified_contact_card(callback.message, contact, is_callback=True)

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

        @dp.callback_query(F.data == "task_bulk:cancel_all")
        async def handle_task_bulk_cancel_all(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            tasks = await self.db_manager.get_pending_tasks()
            cancel_count = 0
            for t in tasks:
                await self.scheduler_service.cancel_task(t.id)
                cancel_count += 1
            await callback.answer(f"🛑 Barcha {cancel_count} ta vazifa bekor qilindi!", show_alert=True)
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
                    "📝 <b>KUTILAYOTGAN QORALAMALAR:</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "✨ <b>Qoralamalar qutisi bo'sh.</b> Hozircha tasdiq kutayotgan xabarlar yo'q.\n\n"
                    "💡 <b>Qoralama qachon hosil bo'ladi?</b>\n"
                    "• <code>draft</code> rejimdagi kontaktlardan xabar kelganda\n"
                    "• <code>auto</code> rejimdagi kontakt xabarida nozik mavzu (to'lov, karta, uchrashuv) aniqlanganda"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="🔙 Bosh menyu", callback_data="menu:main")
                builder.adjust(1)
                if is_callback:
                    await target_msg.edit_text(empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                else:
                    await safe_reply(target_msg, empty_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return

            lines = [
                f"📝 <b>KUTILAYOTGAN QORALAMALAR ({len(drafts)} ta):</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>AI tayyorlagan javobni yuborish uchun tasdiqlang yoki tahrirlang:</i>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            ]
            builder = InlineKeyboardBuilder()

            for d in drafts:
                contact_str = f"@{d.contact_username}" if d.contact_username else str(d.contact_user_id or "Noma'lum")
                safe_contact = html.escape(contact_str)
                safe_incoming = html.escape(d.incoming_text[:200] + ("..." if len(d.incoming_text) > 200 else ""))
                safe_reply_text = html.escape(d.proposed_reply[:250] + ("..." if len(d.proposed_reply) > 250 else ""))

                sens_badge = ""
                if d.is_sensitive:
                    sens_reason = html.escape(d.sensitive_reason or "Nozik mavzu")
                    sens_badge = f"\n⚠️ <b>Diqqat - Tasdiq talab etiladi!</b> (<i>{sens_reason}</i>)"

                lines.append(
                    f"📌 <b>Qoralama #{d.id}</b>{sens_badge}\n"
                    f"👤 <b>Kimdan:</b> <code>{safe_contact}</code>\n"
                    f"📩 <b>Kelgan xabar:</b>\n<blockquote>{safe_incoming}</blockquote>\n"
                    f"🤖 <b>AI tavsiya qilgan javob:</b>\n<code>{safe_reply_text}</code>\n"
                )

                builder.button(text=f"✅ Yuborish #{d.id}", callback_data=f"draft_send:{d.id}")
                builder.button(text=f"✍️ Tahrirlash #{d.id}", callback_data=f"draft_edit:{d.id}")
                builder.button(text=f"❌ Rad #{d.id}", callback_data=f"draft_reject:{d.id}")

            if len(drafts) >= 1:
                builder.button(text=f"🚀 Barchasini tasdiqlash ({len(drafts)})", callback_data="draft_bulk:send_all")
                builder.button(text="❌ Barchasini rad etish", callback_data="draft_bulk:reject_all")

            builder.button(text="🔙 Bosh menyu", callback_data="menu:main")

            adjust_layout = [3] * len(drafts)
            if len(drafts) >= 1:
                adjust_layout.append(2)
            adjust_layout.append(1)
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

        async def _execute_single_draft_send(draft_id: int) -> tuple[bool, str]:
            """Executes atomic resolution and sending of a single draft."""
            draft = await self.db_manager.get_draft_by_id(draft_id)
            if not draft or not draft.is_pending:
                return False, "Allaqachon bajarilgan"

            now_iso = datetime.now().isoformat()
            if draft.expires_at <= now_iso:
                await self.db_manager.resolve_draft_atomic(draft.id, "expired")
                return False, "Qoralama muddati tugagan (24 soat)"

            if self._manual_reply_check_fn and (draft.contact_user_id or draft.contact_username):
                target_peer = draft.contact_user_id or f"@{draft.contact_username}"
                replied_manually = await self._manual_reply_check_fn(target_peer, draft.source_message_id)
                if replied_manually:
                    await self.db_manager.resolve_draft_atomic(draft.id, "superseded")
                    return False, "Siz allaqachon qo'lda javob bergansiz (superseded)"

            success = await self.db_manager.resolve_draft_atomic(draft_id, "sent")
            if not success:
                return False, "Bajarilmadi"

            if self._userbot_send_fn:
                peer = f"@{draft.contact_username}" if draft.contact_username else str(draft.contact_user_id or "")
                if peer:
                    sent_msg = await self._userbot_send_fn(peer, draft.proposed_reply)
                    if self.memory_service and sent_msg:
                        target_user_id = draft.contact_user_id
                        if not target_user_id and draft.contact_username:
                            c = await self.db_manager.get_contact_by_username(draft.contact_username)
                            target_user_id = c.user_id if c else None
                        if target_user_id:
                            await self.memory_service.log_message(
                                contact_user_id=target_user_id,
                                tg_message_id=getattr(sent_msg, "id", 0),
                                sender="bot_approved",
                                text=draft.proposed_reply,
                                username=draft.contact_username,
                            )
            return True, "Yuborildi"

        @dp.callback_query(F.data == "draft_bulk:send_all")
        async def handle_draft_bulk_send_all(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            drafts = await self.db_manager.list_pending_drafts()
            sent_count = 0
            for d in drafts:
                ok, _ = await _execute_single_draft_send(d.id)
                if ok:
                    sent_count += 1
            await callback.answer(f"🚀 Barcha {sent_count} ta qoralama yuborildi!", show_alert=True)
            await _render_drafts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data == "draft_bulk:reject_all")
        async def handle_draft_bulk_reject_all(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            drafts = await self.db_manager.list_pending_drafts()
            reject_count = 0
            for d in drafts:
                ok = await self.db_manager.resolve_draft_atomic(d.id, "rejected")
                if ok:
                    reject_count += 1
            await callback.answer(f"❌ Barcha {reject_count} ta qoralama rad etildi!", show_alert=True)
            await _render_drafts_list(callback.message, is_callback=True)

        @dp.callback_query(F.data.startswith("draft_send:"))
        async def handle_draft_send(callback: types.CallbackQuery):
            if not self._is_admin(callback.from_user.id):
                return
            draft_id = int(callback.data.split(":")[1])
            success, msg = await _execute_single_draft_send(draft_id)
            if not success:
                return await callback.answer(f"⚠️ {msg}", show_alert=True)

            await callback.answer(f"✅ #{draft_id} qoralama yuborildi!", show_alert=True)
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
            card_chat_id = callback.message.chat.id if callback.message else None
            card_msg_id = callback.message.message_id if callback.message else None
            await state.update_data(
                draft_id=draft_id,
                created_ts=datetime.now().timestamp(),
                card_chat_id=card_chat_id,
                card_msg_id=card_msg_id,
            )
            prompt_msg = await callback.message.reply(
                f"✍️ <b>Qoralama #{draft_id} uchun yangi javob matnini yozing:</b>\n\n"
                f"<i>(Bekor qilish uchun <code>/cancel</code> deb yozing; vaqt limiti 30 daqiqa)</i>",
                parse_mode="HTML",
            )
            await state.update_data(prompt_msg_id=prompt_msg.message_id)
            await callback.answer()

    async def send_draft_for_approval(self, draft: Draft):
        """Sends interactive draft approval request to Boss with inline action buttons."""
        if not self.bot or not self.admin_id:
            logger.info(f"Draft #{draft.id} approval notification (chars={len(draft.proposed_reply)})")
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
            logger.info(f"Escalation notification for peer {peer} (chars={len(incoming_message)})")
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
            logger.info(f"Notification to Boss (chars={len(text)})")
            return
        await safe_send(bot=self.bot, chat_id=self.admin_id, text=text, parse_mode="HTML")
