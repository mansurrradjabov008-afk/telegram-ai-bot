"""
Contact Memory Service for Antigravity Butler.
Coordinates message capture, contact identity binding, redaction,
sender labeling, media marker handling, and message edits.
"""

import logging
from typing import Optional, Tuple
from config import settings
from database.crud import DatabaseManager
from database.models import MemContact

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    async def verify_and_bind_identity(
        self,
        contact_user_id: int,
        username: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Enforces Identity Rule H2:
        Key all memory strictly by numeric contact_user_id, NOT username.
        If a saved contact's username now resolves to a different user_id than
        the one memory is bound to: PAUSE memory for that contact, store nothing new,
        and flag for owner notification.
        """
        if not getattr(settings, "MEMORY_ENABLED", True):
            return False, "memory_disabled"

        if username:
            clean_username = username.lstrip("@").strip()
            # Check if this username was previously bound to another user_id
            existing_by_username = await self.db_manager.get_mem_contact_by_username(clean_username)
            if existing_by_username and existing_by_username.contact_user_id != contact_user_id:
                reason = f"username_reassigned_to_{contact_user_id}"
                logger.warning(
                    f"⚠️ [H2 IDENTITY ALERT] Username @{clean_username} previously bound to user "
                    f"{existing_by_username.contact_user_id}, now seen on user {contact_user_id}! "
                    f"Pausing memory for both."
                )
                await self.db_manager.pause_mem_contact(existing_by_username.contact_user_id, reason)
                return False, reason

        # Bind or retrieve for contact_user_id
        mem_contact = await self.db_manager.bind_mem_contact(contact_user_id, username)
        if not mem_contact.memory_enabled:
            return False, mem_contact.paused_reason or "contact_memory_paused"

        return True, None

    async def log_message(
        self,
        contact_user_id: int,
        tg_message_id: int,
        sender: str,
        text: str,
        is_media: bool = False,
        media_type: Optional[str] = None,
        contact_mode: str = "draft",
        username: Optional[str] = None,
    ) -> Optional[int]:
        """
        Allowed Integration Point 1: Message logging hook.
        Logs message to mem_messages with sender attribution (H8) and redaction (H5).
        Wrapped in try/except; failure NEVER affects core reply pipeline (P15).
        """
        try:
            if not getattr(settings, "MEMORY_ENABLED", True):
                return None

            # H3: Muted contacts store nothing unless MEMORY_LOG_MUTED is True
            if contact_mode == "mute" and not getattr(settings, "MEMORY_LOG_MUTED", False):
                return None

            # H2: Identity check & verification
            allowed, reason = await self.verify_and_bind_identity(contact_user_id, username)
            if not allowed:
                logger.debug(f"Memory logging skipped for user {contact_user_id}: {reason}")
                return None

            # Media marker formatting
            final_text = (text or "").strip()
            if is_media:
                marker = f"[{media_type}]" if media_type else "[fayl]"
                if not marker.startswith("["):
                    marker = f"[{marker}]"
                final_text = f"{marker} {final_text}".strip() if final_text else marker

            msg_id = await self.db_manager.log_mem_message(
                contact_user_id=contact_user_id,
                tg_message_id=tg_message_id,
                sender=sender,
                text=final_text,
                is_media=is_media,
            )
            return msg_id
        except Exception as e:
            logger.warning(f"Failed to log message to mem_messages for user {contact_user_id}: {e}")
            return None

    async def handle_message_edit(
        self,
        contact_user_id: int,
        tg_message_id: int,
        new_text: str,
    ) -> bool:
        """
        Handles message edit events: re-redacts text and updates edited_at.
        """
        try:
            if not getattr(settings, "MEMORY_ENABLED", True):
                return False

            return await self.db_manager.update_mem_message_edit(
                contact_user_id=contact_user_id,
                tg_message_id=tg_message_id,
                new_text=new_text,
            )
        except Exception as e:
            logger.warning(f"Failed to update edited message for user {contact_user_id}: {e}")
            return False

    async def purge_retention(self) -> int:
        """
        Purges messages older than MEM_MESSAGE_RETENTION_DAYS (default 180).
        """
        try:
            days = getattr(settings, "MEM_MESSAGE_RETENTION_DAYS", 180)
            deleted_count = await self.db_manager.purge_old_mem_messages(days)
            if deleted_count > 0:
                logger.info(f"Purged {deleted_count} expired messages from mem_messages (older than {days} days).")
            return deleted_count
        except Exception as e:
            logger.warning(f"Error during memory retention purge: {e}")
            return 0
