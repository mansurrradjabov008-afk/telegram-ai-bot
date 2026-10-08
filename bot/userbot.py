"""
MTProto Userbot client implementation using Telethon.
Connects directly to the user's personal Telegram account, listens to chats, channels,
and executes AI-powered actions with anti-ban safeguards.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Callable
from telethon import TelegramClient, events
from telethon.tl.types import User, Channel, Chat

from config import settings
from database.crud import DatabaseManager
from services.ai_engine import AIEngine
from services.rate_limiter import HumanRateLimiter
from services.digest_service import DigestService

logger = logging.getLogger(__name__)


class UserbotService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        ai_engine: AIEngine,
        rate_limiter: HumanRateLimiter,
        digest_service: DigestService,
    ):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self.rate_limiter = rate_limiter
        self.digest_service = digest_service

        self.session_path = Path("data") / settings.TELEGRAM_SESSION_NAME
        self.client: Optional[TelegramClient] = None
        self._escalate_callback: Optional[Callable] = None
        self._is_running = False

    def set_escalation_callback(self, callback: Callable):
        """Callback to notify Admin Bot Cockpit when an escalation happens."""
        self._escalate_callback = callback

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
                logger.error(f"Error handling private message: {e}", exc_info=True)

        @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_channel or e.is_group))
        async def on_feed_message(event):
            try:
                await self._handle_feed_message(event)
            except Exception as e:
                logger.error(f"Error handling feed message: {e}")

    async def _handle_private_message(self, event):
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.is_self or sender.bot:
            return

        sender_id = sender.id
        username = sender.username or ""
        first_name = sender.first_name or "Foydalanuvchi"
        incoming_text = event.raw_text.strip()
        peer_repr = f"@{username}" if username else str(sender_id)

        logger.info(f"Incoming private message from {first_name} ({peer_repr}): '{incoming_text}'")

        # 1. Log incoming message to DB
        await self.db_manager.log_message(
            chat_id=sender_id,
            sender_name=first_name,
            role="contact",
            content=incoming_text,
        )

        # 2. Get contact CRM profile
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
                )
                if old_id != sender_id:
                    await self.db_manager.delete_contact(old_id)

        # 3. STRICT CONTACT WHITELIST CHECK
        # The AI Butler is ONLY permitted to converse with contacts explicitly listed in /contacts (CRM).
        # For ANY unlisted sender, the bot MUST NOT auto-reply on Boss's behalf.
        # Instead, it forwards/escalates the message to Boss via Admin Bot.
        if not contact:
            logger.info(
                f"🛡 [WHITELIST ENFORCED] Sender {peer_repr} ({first_name}) is NOT in /contacts. "
                f"Auto-reply blocked. Escalating incoming message to Boss."
            )

            # Generate suggested reply using standard polite addressing for Boss's 1-click convenience
            suggested_reply = None
            try:
                suggested_reply = await self.ai_engine.generate_reply(
                    incoming_text=incoming_text,
                    sender_name=first_name,
                    contact_profile=None,
                    history=None,
                )
            except Exception as e:
                logger.warning(f"Could not generate AI suggestion for unlisted contact: {e}")

            if self._escalate_callback:
                await self._escalate_callback(
                    peer=peer_repr,
                    sender_name=first_name,
                    incoming_message=incoming_text,
                    suggested_reply=suggested_reply,
                    reason="NOT_IN_CONTACTS",
                    is_unknown_contact=True,
                )
            return

        # 4. Sender IS an approved contact: Get recent conversation history
        history = await self.db_manager.get_recent_history(chat_id=sender_id, limit=6)

        # 5. Check if auto-reply is disabled globally
        if not settings.AUTO_REPLY_ENABLED:
            logger.info("Auto-reply is globally disabled. Skipping automatic processing.")
            return

        # 6. Two-tier AI Classification for approved contact
        classification = await self.ai_engine.classify_incoming_message(
            incoming_text=incoming_text,
            sender_name=first_name,
            contact_profile=contact,
            history=history,
        )

        logger.info(
            f"Classification for approved contact {peer_repr}: {classification.category} "
            f"(Confidence: {classification.confidence:.2f}, Reason: {classification.reason})"
        )

        if classification.category == "ESCALATE_TO_BOSS":
            # Escalate directly to Boss via Admin Bot
            if self._escalate_callback:
                await self._escalate_callback(
                    peer=peer_repr,
                    sender_name=first_name,
                    incoming_message=incoming_text,
                    suggested_reply=classification.suggested_reply,
                    reason=classification.reason,
                    is_unknown_contact=False,
                )
        else:
            # Safe Chit-chat for approved contact: Generate persona-aligned response
            reply_text = await self.ai_engine.generate_reply(
                incoming_text=incoming_text,
                sender_name=first_name,
                contact_profile=contact,
                history=history,
            )

            # Human-like delay and typing simulation
            typing_duration = self.rate_limiter.calculate_typing_duration(reply_text)
            async with self.client.action(event.chat_id, "typing"):
                await asyncio.sleep(typing_duration)

            await event.reply(reply_text)

            # Log outgoing reply to DB
            await self.db_manager.log_message(
                chat_id=sender_id,
                sender_name="Men (AI Butler)",
                role="assistant",
                content=reply_text,
            )
            logger.info(f"Auto-replied to approved contact {peer_repr}: '{reply_text}'")

    async def _handle_feed_message(self, event):
        chat = await event.get_chat()
        title = getattr(chat, "title", "Noma'lum manba")
        text = event.raw_text.strip()
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
        """
        if not self.client:
            raise RuntimeError("Userbot client is not initialized.")

        logger.info(f"Userbot sending direct message to {target_peer}: '{message_text}'")
        peer_target = int(target_peer) if (isinstance(target_peer, str) and target_peer.lstrip("-").isdigit()) else target_peer
        entity = await self.client.get_input_entity(peer_target)

        # Human-like typing delay
        typing_duration = self.rate_limiter.calculate_typing_duration(message_text)
        async with self.client.action(entity, "typing"):
            await asyncio.sleep(typing_duration)

        await self.client.send_message(entity, message_text)

        # Log to DB
        chat_id = getattr(entity, "user_id", 0) or 0
        await self.db_manager.log_message(
            chat_id=chat_id,
            sender_name="Men (AI Butler Reja)",
            role="assistant",
            content=message_text,
        )

    async def start(self):
        """Starts client session connection."""
        if not self.client:
            return
        await self.client.start(phone=settings.TELEGRAM_PHONE_NUMBER)
        self._is_running = True
        logger.info("Userbot MTProto Client successfully started and authorized.")
