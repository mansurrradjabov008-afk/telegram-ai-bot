"""
MTProto Userbot client implementation using Telethon.
Connects directly to the user's personal Telegram account, listens to chats, channels,
and executes AI-powered actions with anti-ban safeguards.
Implements 8-step message pipeline, debounce buffer, sensitive guard, and draft flows.
"""

import asyncio
import html
import logging
import random
import time
from pathlib import Path
from typing import Optional, Callable, Dict, List, Any
from telethon import TelegramClient, events
from telethon.tl.types import User, Channel, Chat, MessageMediaWebPage
from telethon.errors import FloodWaitError

from config import settings
from database.crud import DatabaseManager
from database.models import Contact, Draft
from services.ai_engine import AIEngine
from services.rate_limiter import HumanRateLimiter
from services.digest_service import DigestService
from services.sensitive_guard import check_sensitive

logger = logging.getLogger(__name__)


class UserbotService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        ai_engine: AIEngine,
        rate_limiter: HumanRateLimiter,
        digest_service: DigestService,
        memory_service: Optional[Any] = None,
        style_service: Optional[Any] = None,
    ):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self.rate_limiter = rate_limiter
        self.digest_service = digest_service
        self.memory_service = memory_service
        self.style_service = style_service

        self.session_path = Path("data") / settings.TELEGRAM_SESSION_NAME
        self.client: Optional[TelegramClient] = None
        self._escalate_callback: Optional[Callable] = None
        self._draft_approval_callback: Optional[Callable] = None
        self._notification_callback: Optional[Callable] = None

        # Debounce state: buffers per sender_id and background timer tasks
        self._debounce_buffers: Dict[int, List[Dict[str, Any]]] = {}
        self._debounce_tasks: Dict[int, asyncio.Task] = {}

        # Throttled notification timestamps for unlisted/unknown senders
        self._unknown_sender_notified: Dict[int, float] = {}

        self._is_running = False

    def set_memory_service(self, memory_service: Any):
        """Sets the Contact Memory service instance."""
        self.memory_service = memory_service

    def set_style_service(self, style_service: Any):
        """Sets the Writing Style service instance."""
        self.style_service = style_service

    def set_escalation_callback(self, callback: Callable):
        """Callback to notify Admin Bot Cockpit when an escalation happens."""
        self._escalate_callback = callback

    def set_draft_approval_callback(self, callback: Callable):
        """Callback to send draft approval requests to Admin Bot Cockpit."""
        self._draft_approval_callback = callback

    def set_notification_callback(self, callback: Callable):
        """Callback to send general notifications to Admin Bot Cockpit."""
        self._notification_callback = callback

    def get_client(self) -> Optional[TelegramClient]:
        """Returns active Telethon client instance."""
        return self.client

    def init_client(self) -> bool:
        """Initializes Telethon TelegramClient instance."""
        if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
            logger.warning("TELEGRAM_API_ID or TELEGRAM_API_HASH not set.")
            return False

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.session_path),
            api_id=settings.TELEGRAM_API_ID,
            api_hash=settings.TELEGRAM_API_HASH,
            connection_retries=None,
            retry_delay=2,
            auto_reconnect=True,
            flood_sleep_threshold=60,
        )
        self._setup_event_handlers()
        return True

    def _setup_event_handlers(self):
        client = self.client

        @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_private_message(event):
            try:
                await self._handle_private_message(event)
            except Exception as e:
                logger.error(f"Error handling private message: {type(e).__name__}")

        @client.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private))
        async def on_outgoing_private_message(event):
            try:
                await self._handle_outgoing_message(event)
            except Exception as e:
                logger.error(f"Error handling outgoing message: {type(e).__name__}")

        @client.on(events.MessageEdited(func=lambda e: e.is_private))
        async def on_message_edited(event):
            try:
                await self._handle_edited_message(event)
            except Exception as e:
                logger.error(f"Error handling edited message: {type(e).__name__}")

        @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_channel or e.is_group))
        async def on_feed_message(event):
            try:
                await self._handle_feed_message(event)
            except Exception as e:
                logger.error(f"Error handling feed message: {type(e).__name__}")


    async def check_owner_manually_replied(self, peer: Any, after_message_id: int) -> bool:
        """
        Checks if the account owner has manually posted a message to the target peer
        after after_message_id.
        Returns True if owner replied manually, False otherwise.
        """
        if not self.client:
            return False
        try:
            peer_target = int(peer) if (isinstance(peer, str) and peer.lstrip("-").isdigit()) else peer
            messages = await self.client.get_messages(peer_target, limit=10)
            for msg in messages:
                if getattr(msg, "out", False) and getattr(msg, "id", 0) > after_message_id:
                    logger.info(
                        f"Owner manually replied to {peer} (msg {msg.id} > {after_message_id})."
                    )
                    return True
        except Exception as e:
            logger.warning(f"Could not check manual reply history for {peer}: {type(e).__name__}")
        return False

    async def _handle_outgoing_message(self, event):
        """
        Captures manual outgoing messages typed by owner in private chats with saved contacts.
        Assigns sender='owner_manual' unless message was sent by the bot (bot_auto, bot_approved, owner_edited).
        """
        chat_id = getattr(event, "chat_id", None)
        if not chat_id:
            return

        # Check if recipient is a saved contact (H1: 1-to-1 with saved contacts only)
        contact = await self.db_manager.get_contact_by_id(chat_id)
        if not contact:
            return

        # Check if this message was already logged (e.g. by bot_auto, bot_approved, owner_edited)
        existing = await self.db_manager.get_mem_message(contact_user_id=chat_id, tg_message_id=event.id)
        if existing:
            return

        # It's an owner manual message typed from Telegram client!
        raw_text = (event.raw_text or "").strip()
        is_media = (
            getattr(event.message, "media", None) is not None
            and not isinstance(event.message.media, MessageMediaWebPage)
        )
        media_marker = None
        if is_media:
            if getattr(event.message, "voice", False):
                media_marker = "ovozli xabar"
            elif getattr(event.message, "photo", False):
                media_marker = "rasm"
            elif getattr(event.message, "video_note", False):
                media_marker = "dumaloq video"
            elif getattr(event.message, "video", False):
                media_marker = "video"
            elif getattr(event.message, "document", False):
                media_marker = "hujjat"
            elif getattr(event.message, "sticker", False):
                media_marker = "stiker"
            else:
                media_marker = "fayl"

        if raw_text and self.db_manager:
            try:
                await self.db_manager.log_message(
                    chat_id=chat_id,
                    sender_name="Mansur (Xo'jayin)",
                    role="owner",
                    content=raw_text,
                )
            except Exception as e:
                logger.warning(f"Failed to log owner manual message to conversation history: {type(e).__name__}")

        if self.memory_service:
            await self.memory_service.log_message(
                contact_user_id=chat_id,
                tg_message_id=event.id,
                sender="owner_manual",
                text=raw_text,
                is_media=is_media,
                media_type=media_marker,
                contact_mode=getattr(contact, "mode", "draft"),
                username=getattr(contact, "username", None),
            )

        # Live Writing Style Ingestion (learn Mansur's tone and style in real time)
        if raw_text and getattr(self, "style_service", None) and self.db_manager:
            try:
                from services.memory_redactor import redact_text
                from services.style_sampler import is_hygienic_sample, detect_language_script, detect_register
                redacted = redact_text(raw_text)
                hygiene = is_hygienic_sample(text=redacted, sent_at=datetime.now())
                if hygiene.passed:
                    lang = detect_language_script(redacted)
                    register = detect_register(redacted) or getattr(contact, "addressing_style", None)
                    await self.db_manager.insert_style_sample(
                        contact_user_id=chat_id,
                        tg_message_id=event.id,
                        text_redacted=redacted,
                        char_len=len(redacted),
                        token_len=len(redacted.split()),
                        lang=lang,
                        register=register,
                        sent_at=datetime.now().isoformat(),
                        source="live",
                    )
            except Exception as e:
                logger.warning(f"Failed to record live style sample: {type(e).__name__}")

    async def _handle_edited_message(self, event):
        """
        Updates edited message in Contact Memory if it was previously captured.
        """
        if not self.memory_service:
            return

        chat_id = getattr(event, "chat_id", None)
        if not chat_id:
            return

        contact = await self.db_manager.get_contact_by_id(chat_id)
        if not contact:
            return

        new_text = (event.raw_text or "").strip()
        await self.memory_service.handle_message_edit(
            contact_user_id=chat_id,
            tg_message_id=event.id,
            new_text=new_text,
        )

    async def _send_with_floodwait(self, peer: Any, text: str, max_retries: int = 3) -> Optional[Any]:
        """
        Sends message to peer with Telethon FloodWaitError safety.
        Safely waits out the flood restriction and retries, never crashing.
        Falls back to plain text (parse_mode=None) on markdown formatting/parse errors.
        """
        if not self.client:
            logger.error("Client not initialized; cannot send message.")
            return None

        for attempt in range(max_retries):
            try:
                return await self.client.send_message(peer, text)
            except FloodWaitError as e:
                wait_time = int(getattr(e, "seconds", 1))
                logger.warning(
                    f"Telegram FloodWait encountered: sleeping for {wait_time}s "
                    f"(attempt {attempt + 1}/{max_retries})..."
                )
                await asyncio.sleep(wait_time + 1)
            except Exception as e:
                logger.warning(
                    f"Send error with markdown for {peer}: {type(e).__name__}. "
                    f"Falling back to plain text (parse_mode=None)..."
                )
                try:
                    return await self.client.send_message(peer, text, parse_mode=None)
                except FloodWaitError as fwe:
                    wait_time = int(getattr(fwe, "seconds", 1))
                    await asyncio.sleep(wait_time + 1)
                    return await self.client.send_message(peer, text, parse_mode=None)
                except Exception as e_plain:
                    logger.error(f"Error sending plain text message to {peer}: {type(e_plain).__name__}")
                    return None
        return None

    async def _handle_private_message(self, event):
        """
        Executes the 8-step message pipeline for incoming private messages:
        1. Ignore: own messages, bots, groups/channels, service messages, and unlisted senders.
        2. Look up contact mode. mute -> stop.
        3. Non-text messages -> notify owner, do not auto-reply.
        4. Debounce -> buffer consecutive messages (default 8s window) and combine into one.
        Steps 5-8 continue in _process_debounced_batch.
        """
        # Step 1: Ignore filters
        sender = await event.get_sender()
        if not isinstance(sender, User) or getattr(sender, "is_self", False) or getattr(sender, "bot", False):
            return

        # Ignore service messages
        if getattr(event.message, "action", None) is not None:
            return

        sender_id = sender.id
        username = (sender.username or "").lstrip("@")
        first_name = sender.first_name or "Foydalanuvchi"
        incoming_text = (event.raw_text or "").strip()
        peer_repr = f"@{username}" if username else str(sender_id)

        # Contact lookup
        contact = await self.db_manager.get_contact_by_id(sender_id)
        if not contact and username:
            contact = await self.db_manager.get_contact_by_username(username)
            if contact and contact.user_id != sender_id:
                old_id = contact.user_id
                contact = await self.db_manager.upsert_contact(
                    user_id=sender_id,
                    username=username,
                    full_name=first_name,
                    relationship_type=contact.relationship_type,
                    addressing_style=getattr(contact, "addressing_style", "siz"),
                    custom_instructions=contact.custom_instructions,
                    mode=getattr(contact, "mode", "draft"),
                )
                if old_id != sender_id:
                    await self.db_manager.delete_contact(old_id)

        # STRICT WHITELIST CHECK: Only private chats with saved contacts are handled
        if not contact:
            logger.info(
                f"🛡 [FILTER] Sender {peer_repr} ({first_name}) is not in /contacts. Pipeline ignored."
            )
            # Notify owner via cockpit notification callback (throttled to 1 alert per sender per hour)
            now_ts = time.time()
            last_alert = self._unknown_sender_notified.get(sender_id, 0)
            if now_ts - last_alert > 3600:
                self._unknown_sender_notified[sender_id] = now_ts
                if self._notification_callback:
                    try:
                        preview_text = incoming_text[:120] if incoming_text else "[Fayl / Ovozli xabar / Media]"
                        user_ref = f"@{username}" if username else f"ID: {sender_id}"
                        alert_msg = (
                            f"🔔 Yangi inson yozdi:\n"
                            f"👤 {first_name} ({user_ref})\n"
                            f"💬 Xabar: \"{preview_text}\"\n"
                            f"💡 Ro'yxatga qo'shish uchun: /add_contact {sender_id}"
                        )
                        asyncio.create_task(self._notification_callback(alert_msg))
                    except Exception as e_notif:
                        logger.warning(f"Failed to send unknown sender notification: {type(e_notif).__name__}")
            return

        # Step 2: Mode lookup
        mode = getattr(contact, "mode", "draft") or "draft"
        if mode == "mute":
            logger.info(f"🔇 Contact {peer_repr} is in 'mute' mode. Pipeline stopped.")
            return

        # Step 3: Non-text messages (voice, photo, file, video note, sticker, etc.)
        is_media = (
            getattr(event.message, "media", None) is not None
            and not isinstance(event.message.media, MessageMediaWebPage)
        )
        media_marker = None
        if is_media:
            if getattr(event.message, "voice", False):
                media_marker = "ovozli xabar"
            elif getattr(event.message, "photo", False):
                media_marker = "rasm"
            elif getattr(event.message, "video_note", False):
                media_marker = "dumaloq video"
            elif getattr(event.message, "video", False):
                media_marker = "video"
            elif getattr(event.message, "document", False):
                media_marker = "hujjat"
            elif getattr(event.message, "sticker", False):
                media_marker = "stiker"
            else:
                media_marker = "fayl"

        if is_media or not incoming_text:
            logger.info(f"📎 Non-text message from {peer_repr}. Notifying owner.")
            # Contact Memory Hook (Incoming Media Message)
            if self.memory_service:
                await self.memory_service.log_message(
                    contact_user_id=sender_id,
                    tg_message_id=event.id,
                    sender="contact",
                    text=incoming_text,
                    is_media=True,
                    media_type=media_marker,
                    contact_mode=mode,
                    username=username,
                )

            if self._notification_callback:
                media_type_label = f"{media_marker} ({media_marker})" if media_marker else "ovozli xabar / rasm / fayl"
                safe_name = html.escape(first_name)
                safe_peer = html.escape(peer_repr)
                await self._notification_callback(
                    f"📎 <b>{safe_name}</b> (<code>{safe_peer}</code>) sizga <i>{media_type_label}</i> yubordi.\n"
                    f"🛡 <i>Avto-javob yuborilmadi.</i>"
                )
            return

        # Log incoming text to DB
        await self.db_manager.log_message(
            chat_id=sender_id,
            sender_name=first_name,
            role="contact",
            content=incoming_text,
        )

        # Contact Memory Hook (Incoming Text Message)
        if self.memory_service:
            await self.memory_service.log_message(
                contact_user_id=sender_id,
                tg_message_id=event.id,
                sender="contact",
                text=incoming_text,
                is_media=is_media,
                media_type=media_marker,
                contact_mode=mode,
                username=username,
            )

        # Step 4: Debounce buffer and timer
        await self._debounce_enqueue(
            sender_id=sender_id,
            first_name=first_name,
            username=username,
            peer_repr=peer_repr,
            contact=contact,
            event=event,
            incoming_text=incoming_text,
        )

    async def _debounce_enqueue(
        self,
        sender_id: int,
        first_name: str,
        username: str,
        peer_repr: str,
        contact: Any,
        event: Any,
        incoming_text: str,
    ):
        """
        Enqueues incoming text message into sender's debounce buffer and resets the window timer.
        """
        if sender_id not in self._debounce_buffers:
            self._debounce_buffers[sender_id] = []

        self._debounce_buffers[sender_id].append({
            "message_id": event.id,
            "text": incoming_text,
            "chat_id": event.chat_id,
            "first_name": first_name,
            "username": username,
            "peer_repr": peer_repr,
            "contact": contact,
        })

        # Cancel existing pending timer if new consecutive message arrived
        existing_task = self._debounce_tasks.get(sender_id)
        if existing_task and not existing_task.done():
            existing_task.cancel()

        window = getattr(settings, "DEBOUNCE_WINDOW_SECONDS", 8.0)
        self._debounce_tasks[sender_id] = asyncio.create_task(
            self._debounce_timer(sender_id, window)
        )

    async def _debounce_timer(self, sender_id: int, window: float):
        """
        Waits for the debounce window, then triggers batch processing.
        """
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Error in debounce timer for sender {sender_id}: {type(e).__name__}")
            return

        try:
            await self._process_debounced_batch(sender_id)
        except Exception as e:
            logger.error(f"Error processing debounced batch for sender {sender_id}: {type(e).__name__}")
        finally:
            self._debounce_tasks.pop(sender_id, None)

    async def _process_debounced_batch(self, sender_id: int):
        """
        Combines consecutive messages from the debounce buffer and executes Steps 5-8:
        5. Generate reply with existing AI logic (using untrusted input wrapper).
        6. Run sensitive-topic guard.
        7. If mode == auto and NOT sensitive: check manual reply check, random delay + typing, send with FloodWait.
        8. If mode == draft OR sensitive: create draft record and send approval request to Boss.
        """
        batch = self._debounce_buffers.pop(sender_id, [])
        if not batch:
            return

        combined_text = "\n".join(m["text"] for m in batch if m.get("text")).strip()
        if not combined_text:
            return

        last_item = batch[-1]
        last_msg_id = last_item["message_id"]
        chat_id = last_item["chat_id"]
        from services.digest_cleaner import transliterate_uzbek_cyrillic_to_latin
        first_name = transliterate_uzbek_cyrillic_to_latin(last_item["first_name"] or "")
        username = last_item["username"]
        peer_repr = last_item["peer_repr"]
        contact = last_item["contact"]
        mode = getattr(contact, "mode", "draft") or "draft"

        logger.info(
            f"Processing debounced batch ({len(batch)} messages) for {peer_repr} (chars={len(combined_text)})"
        )

        # Dynamic Register & Addressing Adaptation (Street Uzbek / Sen detection)
        from services.style_sampler import detect_register
        detected_reg = detect_register(combined_text)
        if detected_reg == "sen" and contact and contact.addressing_style == "siz":
            inst = (contact.custom_instructions or "").lower()
            if not any(w in inst for w in ["qat'iy siz", "faqat siz", "rasmiy", "sizlash shart"]):
                logger.info(f"Dynamic register detected 'sen' for {peer_repr}. Updating contact addressing style.")
                contact.addressing_style = "sen"
                try:
                    await self.db_manager.update_addressing_style(user_id=contact.user_id, addressing_style="sen")
                except Exception as e:
                    logger.warning(f"Failed to persist addressing style update for {peer_repr}: {type(e).__name__}")

        # Step 5: Generate reply with existing AI logic and Contact Memory (Integration Point 2)
        history = await self.db_manager.get_recent_history(chat_id=sender_id, limit=15)
        memory_context = ""
        if self.memory_service and contact and getattr(contact, "user_id", None):
            try:
                memory_context = await self.memory_service.get_reply_context(
                    contact_user_id=contact.user_id,
                    incoming_text=combined_text,
                )
            except Exception as e:
                logger.warning(f"Failed to fetch memory context for {peer_repr}: {type(e).__name__}")
                memory_context = ""

        # Writing Style Hook (Phase 5, D1, D8)
        if getattr(self, "style_service", None) and contact and getattr(contact, "user_id", None):
            try:
                style_ctx = await self.style_service.get_style_context(
                    contact_user_id=contact.user_id,
                    incoming_text=combined_text,
                    timeout=getattr(settings, "STYLE_CONTEXT_TIMEOUT_SECONDS", 1.0),
                )
                if style_ctx:
                    memory_context = f"{memory_context}\n\n{style_ctx}" if memory_context else style_ctx
            except Exception as e:
                logger.warning(f"Failed to fetch style context for {peer_repr}: {type(e).__name__}")

        reply_text = await self.ai_engine.generate_reply(
            incoming_text=combined_text,
            sender_name=first_name,
            contact_profile=contact,
            history=history,
            memory_context=memory_context,
        )

        if not reply_text:
            if hasattr(self.ai_engine, "_dynamic_smart_fallback"):
                reply_text = self.ai_engine._dynamic_smart_fallback(
                    combined_text,
                    addressing=getattr(contact, "addressing_style", "siz")
                )
            if not reply_text:
                logger.warning(f"Empty AI reply generated for {peer_repr}; notifying owner.")
                if self._notification_callback:
                    try:
                        preview = combined_text[:150]
                        asyncio.create_task(
                            self._notification_callback(
                                f"⚠️ [OGOHLANTIRISH] AI javob yarata olmadi.\n"
                                f"👤 Suhbatdosh: {peer_repr} ({first_name})\n"
                                f"💬 Xabar: \"{preview}\"\n"
                                f"Iltimos, o'zingiz qo'lda yozing."
                            )
                        )
                    except Exception as e_n:
                        logger.warning(f"Failed to notify owner about empty AI reply: {type(e_n).__name__}")
                return

        # Step 6: Sensitive Guard check
        guard_result = await check_sensitive(
            incoming_text=combined_text,
            proposed_reply=reply_text,
            ai_engine=self.ai_engine,
        )
        is_sensitive = guard_result.is_sensitive
        sensitive_reason = guard_result.primary_reason

        # Tier 1 Guardrail: AI Intent Classification (SAFE_CHITCHAT vs ESCALATE_TO_BOSS)
        # Distinguish hard critical security boundaries from routine chitchat:
        intent_result = None
        try:
            intent_result = await self.ai_engine.classify_incoming_message(
                incoming_text=combined_text,
                sender_name=first_name,
                contact_profile=contact,
                history=history,
            )
            if intent_result.category == "ESCALATE_TO_BOSS":
                is_sensitive = True
                if not sensitive_reason:
                    sensitive_reason = intent_result.reason or "Xo'jayin qarori talab etiladi"
                logger.info(
                    f"🚨 [ESCALATE INTENT] Message from {peer_repr} requires Boss decision: {sensitive_reason}"
                )
            elif intent_result.category == "SAFE_CHITCHAT" and not getattr(guard_result, "hard_sensitive", False):
                # Routine conversational inquiry (e.g. casual "ko'rishamizmi?") with safe, non-binding reply
                is_sensitive = False
                sensitive_reason = None
                logger.info(
                    f"💬 [SAFE CHITCHAT AUTO] Routine inquiry from {peer_repr} safe to auto-reply (non-hard sensitive)."
                )
        except Exception as e:
            logger.warning(f"Error during intent classification for {peer_repr}: {type(e).__name__}")

        # AUTO MODE AUTONOMY GUARANTEE:
        # When contact is set to 'auto', routine coordination, casual meetings (library, cafe, meetup),
        # routine questions ("nega javob bermayapsan"), and everyday chatter must auto-reply smoothly
        # without bothering the Boss. Only true hard security risks (cards, money, passwords, lawsuits,
        # prompt injections) or explicit escalation to Boss ("ESCALATE_TO_BOSS") will trigger draft approval.
        if mode == "auto" and not getattr(guard_result, "hard_sensitive", False):
            if not (intent_result and intent_result.category == "ESCALATE_TO_BOSS"):
                if is_sensitive:
                    logger.info(
                        f"⚡ [AUTO MODE AUTONOMY] Overriding soft sensitivity ({sensitive_reason}) for {peer_repr} "
                        f"in auto mode. Auto-replying autonomously."
                    )
                    is_sensitive = False
                    sensitive_reason = None

        # Writing Style Honesty Guard (Phase 5, Rule D6)
        # If incoming message asks whether AI/bot or owner:
        # In draft mode -> route to draft with "Muhim - tasdiq kerak"
        # In auto mode -> let Rule 11 of AI Engine naturally answer with human wit ("E qanaqa bot jigar...")
        if getattr(self, "style_service", None) and contact and getattr(contact, "user_id", None):
            try:
                is_style_active = await self.style_service.is_style_enabled_for_contact(contact.user_id)
                if is_style_active and self.style_service.check_honesty_question(combined_text):
                    if mode != "auto":
                        is_sensitive = True
                        sensitive_reason = "Muhim - tasdiq kerak"
                        logger.info(f"Honesty check triggered for {peer_repr} in draft mode. Routing to draft.")
                    else:
                        logger.info(f"Honesty question detected for {peer_repr} in auto mode. Allowing AI Rule 11 natural human comeback.")
            except Exception as e:
                logger.warning(f"Error during style honesty check for {peer_repr}: {type(e).__name__}")

        # Step 7: Auto mode and NOT sensitive
        if mode == "auto" and not is_sensitive:
            if not settings.AUTO_REPLY_ENABLED:
                logger.info("Auto-reply is globally disabled. Storing as draft instead.")
                mode = "draft"
            else:
                # Check recent history: if owner replied manually after contact's message, cancel
                replied_manually = await self.check_owner_manually_replied(peer=chat_id, after_message_id=last_msg_id)
                if replied_manually:
                    logger.info(
                        f"Owner already manually replied to {peer_repr} (after msg {last_msg_id}). "
                        f"Auto-reply superseded / cancelled."
                    )
                    return

                # Typing action + random human-like delay
                delay = random.uniform(
                    getattr(settings, "HUMAN_DELAY_MIN", 3),
                    getattr(settings, "HUMAN_DELAY_MAX", 8),
                )
                try:
                    if self.client:
                        async with self.client.action(chat_id, "typing"):
                            await asyncio.sleep(delay)
                    else:
                        await asyncio.sleep(delay)
                except Exception as e:
                    logger.warning(f"Typing simulation failed for {chat_id}: {type(e).__name__}")
                    await asyncio.sleep(delay)

                # Send message safely with FloodWait handling
                sent_msg = await self._send_with_floodwait(chat_id, reply_text)

                # Log outgoing reply to DB
                await self.db_manager.log_message(
                    chat_id=sender_id,
                    sender_name="Men (AI Butler)",
                    role="assistant",
                    content=reply_text,
                )

                # Contact Memory Hook (Bot Auto Message)
                if self.memory_service and sent_msg:
                    await self.memory_service.log_message(
                        contact_user_id=sender_id,
                        tg_message_id=getattr(sent_msg, "id", 0),
                        sender="bot_auto",
                        text=reply_text,
                        contact_mode=mode,
                        username=username,
                    )

                logger.info(f"Auto-replied to {peer_repr} (chars={len(reply_text)})")
                if self.memory_service:
                    async def _bg_mem_auto(uid: int):
                        try:
                            await self.memory_service.extract_facts_and_items(contact_user_id=uid)
                            await self.memory_service.process_pending_embeddings(limit=10)
                        except Exception:
                            pass
                    asyncio.create_task(_bg_mem_auto(sender_id))
                return

        # Step 8: Draft mode OR Sensitive topic
        if mode == "draft" or is_sensitive:
            draft = await self.db_manager.create_draft(
                contact_username=contact.username,
                contact_user_id=contact.user_id,
                source_message_id=last_msg_id,
                incoming_text=combined_text,
                proposed_reply=reply_text,
                is_sensitive=is_sensitive,
                sensitive_reason=sensitive_reason,
            )
            if draft:
                if self._draft_approval_callback:
                    await self._draft_approval_callback(draft)
                logger.info(
                    f"Draft #{draft.id} created for {peer_repr} (sensitive={is_sensitive}, reason={sensitive_reason})."
                )
                if self.memory_service:
                    async def _bg_mem_draft(uid: int):
                        try:
                            await self.memory_service.extract_facts_and_items(contact_user_id=uid)
                            await self.memory_service.process_pending_embeddings(limit=10)
                        except Exception:
                            pass
                    asyncio.create_task(_bg_mem_draft(sender_id))
            else:
                logger.warning(f"Duplicate draft skipped for {peer_repr} (source msg {last_msg_id}).")

    async def _handle_feed_message(self, event):
        """Processes channel and group posts for executive digest summarization."""
        chat = await event.get_chat()
        title = getattr(chat, "title", "Noma'lum manba")
        text = (event.raw_text or "").strip()
        is_group = bool(event.is_group)

        if text and len(text) > 20:
            await self.digest_service.process_incoming_post(
                peer_id=chat.id,
                source_title=title,
                message_id=event.id,
                text=text,
                is_group=is_group,
            )

    async def send_direct_message(self, target_peer: str, message_text: str):
        """
        Sends message to target peer with human-like typing simulation.
        Can resolve @username, phone number, or chat_id.
        Safely handles FloodWait and retries.
        """
        if not self.client:
            raise RuntimeError("Userbot client is not initialized.")

        logger.info(f"Userbot sending direct message to {target_peer} (chars={len(message_text)})")
        peer_target = int(target_peer) if (isinstance(target_peer, str) and target_peer.lstrip("-").isdigit()) else target_peer
        try:
            entity = await self.client.get_entity(peer_target)
        except Exception:
            entity = await self.client.get_input_entity(peer_target)

        # Human-like typing delay
        typing_duration = self.rate_limiter.calculate_typing_duration(message_text)
        try:
            async with self.client.action(entity, "typing"):
                await asyncio.sleep(typing_duration)
        except Exception as e:
            logger.warning(f"Typing simulation failed for {target_peer}: {type(e).__name__}")
            await asyncio.sleep(typing_duration)

        sent_msg = await self._send_with_floodwait(entity, message_text)
        if not sent_msg:
            raise RuntimeError(f"Xabar '{target_peer}' ga yetkazilmadi (FloodWait yoki tarmoq xatosi).")

        # Log to DB
        chat_id = getattr(entity, "id", None) or getattr(entity, "user_id", 0) or 0
        await self.db_manager.log_message(
            chat_id=chat_id,
            sender_name="Men (AI Butler Reja)",
            role="assistant",
            content=message_text,
        )
        return sent_msg

    async def start(self):
        """Starts client session connection."""
        if not self.client:
            return
        await self.client.start(phone=settings.TELEGRAM_PHONE_NUMBER)
        self._is_running = True
        logger.info("Userbot MTProto Client successfully started and authorized.")
