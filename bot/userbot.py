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
    ):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self.rate_limiter = rate_limiter
        self.digest_service = digest_service

        self.session_path = Path("data") / settings.TELEGRAM_SESSION_NAME
        self.client: Optional[TelegramClient] = None
        self._escalate_callback: Optional[Callable] = None
        self._draft_approval_callback: Optional[Callable] = None
        self._notification_callback: Optional[Callable] = None

        # Debounce state: buffers per sender_id and background timer tasks
        self._debounce_buffers: Dict[int, List[Dict[str, Any]]] = {}
        self._debounce_tasks: Dict[int, asyncio.Task] = {}

        self._is_running = False

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
            logger.warning(f"Could not check manual reply history for {peer}: {e}")
        return False

    async def _send_with_floodwait(self, peer: Any, text: str, max_retries: int = 3) -> Optional[Any]:
        """
        Sends message to peer with Telethon FloodWaitError safety.
        Safely waits out the flood restriction and retries, never crashing.
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
                logger.error(f"Error sending message to {peer}: {e}", exc_info=True)
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
        if is_media or not incoming_text:
            logger.info(f"📎 Non-text message from {peer_repr}. Notifying owner.")
            if self._notification_callback:
                media_type = "ovozli xabar / rasm / fayl"
                if getattr(event.message, "voice", False):
                    media_type = "ovozli xabar (voice)"
                elif getattr(event.message, "photo", False):
                    media_type = "rasm"
                elif getattr(event.message, "video_note", False):
                    media_type = "dumaloq video (video note)"
                elif getattr(event.message, "document", False):
                    media_type = "hujjat / fayl"

                safe_name = html.escape(first_name)
                safe_peer = html.escape(peer_repr)
                await self._notification_callback(
                    f"📎 <b>{safe_name}</b> (<code>{safe_peer}</code>) sizga <i>{media_type}</i> yubordi.\n"
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
            logger.error(f"Error in debounce timer for sender {sender_id}: {e}")
            return

        try:
            await self._process_debounced_batch(sender_id)
        except Exception as e:
            logger.error(f"Error processing debounced batch for sender {sender_id}: {e}", exc_info=True)
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
        first_name = last_item["first_name"]
        username = last_item["username"]
        peer_repr = last_item["peer_repr"]
        contact = last_item["contact"]
        mode = getattr(contact, "mode", "draft") or "draft"

        logger.info(
            f"Processing debounced batch ({len(batch)} messages) for {peer_repr}: '{combined_text[:100]}...'"
        )

        # Step 5: Generate reply with existing AI logic
        history = await self.db_manager.get_recent_history(chat_id=sender_id, limit=6)
        reply_text = await self.ai_engine.generate_reply(
            incoming_text=combined_text,
            sender_name=first_name,
            contact_profile=contact,
            history=history,
        )

        if not reply_text:
            logger.warning(f"Empty AI reply generated for {peer_repr}; skipping.")
            return

        # Step 6: Sensitive Guard check
        guard_result = await check_sensitive(
            incoming_text=combined_text,
            proposed_reply=reply_text,
            ai_engine=self.ai_engine,
        )
        is_sensitive = guard_result.is_sensitive
        sensitive_reason = guard_result.primary_reason

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
                    logger.warning(f"Typing simulation failed for {chat_id}: {e}")
                    await asyncio.sleep(delay)

                # Send message safely with FloodWait handling
                await self._send_with_floodwait(chat_id, reply_text)

                # Log outgoing reply to DB
                await self.db_manager.log_message(
                    chat_id=sender_id,
                    sender_name="Men (AI Butler)",
                    role="assistant",
                    content=reply_text,
                )
                logger.info(f"Auto-replied to {peer_repr}: '{reply_text}'")
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

        logger.info(f"Userbot sending direct message to {target_peer}: '{message_text}'")
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
            logger.warning(f"Typing simulation failed for {target_peer}: {e}")
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

    async def start(self):
        """Starts client session connection."""
        if not self.client:
            return
        await self.client.start(phone=settings.TELEGRAM_PHONE_NUMBER)
        self._is_running = True
        logger.info("Userbot MTProto Client successfully started and authorized.")
