"""
Historical Writing Style Importer (D2, D9, Phase 2).
Scans owner's past outgoing messages strictly older than BOT_CUTOFF - STYLE_CUTOFF_MARGIN_HOURS
from private chats with saved, non-muted contacts, applies redaction and hygiene filtering,
and saves verified style samples without marking messages as read.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Optional, Dict, Any, List, Set
from config import settings
from database.connection import get_db
from database.crud import DatabaseManager
from services.memory_redactor import redact_text
from services.style_sampler import (
    is_hygienic_sample,
    normalize_sample_text,
    detect_language_script,
    detect_register,
)

logger = logging.getLogger(__name__)


def _parse_db_timestamp(ts: Optional[str]) -> Optional[datetime]:
    """Safely parses SQLite timestamp strings into naive UTC datetime."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(ts.split("+")[0].strip(), fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=None)
    except Exception:
        return None


async def determine_bot_cutoff(db_manager: DatabaseManager) -> Optional[datetime]:
    """
    Computes earliest timestamp of any bot-sent message across:
    1. conversation_history (role IN ('assistant', 'model'))
    2. drafts (status IN ('sent', 'edited_sent'))
    3. scheduled_tasks (status = 'executed')
    4. mem_messages (sender IN ('bot_auto', 'bot_approved', 'owner_edited'))
    Subtracts STYLE_CUTOFF_MARGIN_HOURS (default 24h).
    Returns None if no bot activity timestamp can be found.
    """
    candidates: List[datetime] = []

    async with get_db(db_manager.db_path) as db:
        # 1. conversation_history
        try:
            async with db.execute(
                "SELECT MIN(timestamp) FROM conversation_history WHERE role IN ('assistant', 'model')"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    dt = _parse_db_timestamp(row[0])
                    if dt:
                        candidates.append(dt)
        except Exception as e:
            logger.warning(f"Error checking conversation_history cutoff: {type(e).__name__}")

        # 2. drafts
        try:
            async with db.execute(
                "SELECT MIN(created_at) FROM drafts WHERE status IN ('sent', 'edited_sent')"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    dt = _parse_db_timestamp(row[0])
                    if dt:
                        candidates.append(dt)
        except Exception as e:
            logger.warning(f"Error checking drafts cutoff: {type(e).__name__}")

        # 3. scheduled_tasks
        try:
            async with db.execute(
                "SELECT MIN(created_at) FROM scheduled_tasks WHERE status = 'executed'"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    dt = _parse_db_timestamp(row[0])
                    if dt:
                        candidates.append(dt)
        except Exception as e:
            logger.warning(f"Error checking scheduled_tasks cutoff: {type(e).__name__}")

        # 4. mem_messages
        try:
            async with db.execute(
                "SELECT MIN(sent_at) FROM mem_messages WHERE sender IN ('bot_auto', 'bot_approved', 'owner_edited')"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    dt = _parse_db_timestamp(row[0])
                    if dt:
                        candidates.append(dt)
        except Exception as e:
            logger.warning(f"Error checking mem_messages cutoff: {type(e).__name__}")

    if not candidates:
        return None

    earliest_bot = min(candidates)
    margin_hours = getattr(settings, "STYLE_CUTOFF_MARGIN_HOURS", 24)
    cutoff = earliest_bot - timedelta(hours=margin_hours)
    return cutoff


class StyleImporter:
    def __init__(self, db_manager: DatabaseManager, telethon_client: Any, owner_id: int):
        self.db_manager = db_manager
        self.client = telethon_client
        self.owner_id = owner_id

    async def import_history(
        self,
        caller_user_id: int,
        custom_cutoff: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Executes historical import strictly adhering to D9 and D2 rules.
        """
        if caller_user_id != self.owner_id:
            logger.warning(f"Unauthorized style import invocation by user_id={caller_user_id}")
            raise PermissionError("Only the owner may invoke style import.")

        cutoff = custom_cutoff
        if cutoff is None:
            cutoff = await determine_bot_cutoff(self.db_manager)
            if cutoff is None:
                raise ValueError("BOT_CUTOFF cannot be determined from database. Import aborted.")

        # Ensure cutoff is naive UTC for comparison
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

        contacts = await self.db_manager.list_contacts()
        eligible_contacts = [c for c in contacts if getattr(c, "mode", "draft") != "mute"]

        logger.info(
            f"Starting style import: {len(eligible_contacts)} eligible contacts, cutoff={cutoff.isoformat()}"
        )

        max_per_contact = getattr(settings, "STYLE_IMPORT_MAX_PER_CONTACT", 200)
        contacts_scanned = 0
        messages_evaluated = 0
        samples_imported = 0
        skipped_counts: Dict[str, int] = {}

        for contact in eligible_contacts:
            contacts_scanned += 1
            contact_id = contact.user_id

            # Gather known bot-sent texts for this contact
            bot_texts = await self.db_manager.get_bot_sent_texts_for_contact(contact_id)
            bot_sent_normalized = {normalize_sample_text(t) for t in bot_texts if t}

            # Seed seen_normalized from already stored samples
            existing_samples = await self.db_manager.get_style_samples(contact_user_id=contact_id)
            seen_normalized = {normalize_sample_text(s["text_redacted"]) for s in existing_samples if s.get("text_redacted")}

            contact_imported = 0

            try:
                msg_generator = self.client.iter_messages(contact_id, limit=max_per_contact)
                async for msg in msg_generator:
                    messages_evaluated += 1

                    # 1. Must be outgoing
                    if not getattr(msg, "out", False):
                        skipped_counts["incoming"] = skipped_counts.get("incoming", 0) + 1
                        continue

                    # 2. Forwarded check
                    if getattr(msg, "fwd_from", None) is not None:
                        skipped_counts["forwarded"] = skipped_counts.get("forwarded", 0) + 1
                        continue

                    # 3. Via bot check
                    if getattr(msg, "via_bot_id", None) is not None:
                        skipped_counts["via_bot"] = skipped_counts.get("via_bot", 0) + 1
                        continue

                    # 4. Service message check
                    if getattr(msg, "action", None) is not None:
                        skipped_counts["service"] = skipped_counts.get("service", 0) + 1
                        continue

                    # 5. Date older than cutoff check
                    msg_date = msg.date
                    if msg_date.tzinfo is not None:
                        msg_date = msg_date.astimezone(timezone.utc).replace(tzinfo=None)

                    if msg_date >= cutoff:
                        skipped_counts["post_cutoff"] = skipped_counts.get("post_cutoff", 0) + 1
                        continue

                    # 6. Raw text & media check
                    raw_text = (getattr(msg, "raw_text", None) or getattr(msg, "text", "") or "").strip()
                    if getattr(msg, "media", None) is not None and not raw_text:
                        skipped_counts["media_only"] = skipped_counts.get("media_only", 0) + 1
                        continue

                    if not raw_text:
                        skipped_counts["empty_text"] = skipped_counts.get("empty_text", 0) + 1
                        continue

                    # 7. Redaction before storage
                    redacted = redact_text(raw_text)

                    # 8. Hygiene check
                    hygiene = is_hygienic_sample(
                        text=redacted,
                        sent_at=msg_date,
                        seen_normalized=seen_normalized,
                        bot_sent_normalized=bot_sent_normalized,
                    )
                    if not hygiene.passed:
                        reason_key = f"hygiene_{hygiene.reason}"
                        skipped_counts[reason_key] = skipped_counts.get(reason_key, 0) + 1
                        continue

                    # 9. Language and Register
                    lang = detect_language_script(redacted)
                    register = detect_register(redacted) or getattr(contact, "addressing_style", None)

                    token_len = len(redacted.split())
                    char_len = len(redacted)

                    # 10. Store sample
                    sample_id = await self.db_manager.insert_style_sample(
                        contact_user_id=contact_id,
                        tg_message_id=getattr(msg, "id", None),
                        text_redacted=redacted,
                        char_len=char_len,
                        token_len=token_len,
                        lang=lang,
                        register=register,
                        sent_at=msg_date.isoformat(),
                        source="import",
                    )
                    if sample_id:
                        samples_imported += 1
                        contact_imported += 1
                        seen_normalized.add(normalize_sample_text(redacted))

            except Exception as e:
                logger.error(f"Error scanning contact {contact_id}: {type(e).__name__}")
                skipped_counts["error"] = skipped_counts.get("error", 0) + 1

        logger.info(
            f"Style import finished: contacts={contacts_scanned}, evaluated={messages_evaluated}, imported={samples_imported}"
        )
        return {
            "contacts_scanned": contacts_scanned,
            "messages_evaluated": messages_evaluated,
            "samples_imported": samples_imported,
            "skipped_counts": skipped_counts,
        }
