"""
Asynchronous Database Manager and CRUD operations for Telegram AI Butler.
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
import aiosqlite
from .connection import get_db
from .models import Contact, ScheduledTask, ConversationMessage, MonitoredSource, Draft, SchemaVersion


class DatabaseManager:
    def __init__(self, db_path=None):
        self.db_path = db_path

    # ------------------ CONTACT CRM ------------------
    async def upsert_contact(
        self,
        user_id: int,
        full_name: str,
        username: Optional[str] = None,
        relationship_type: str = "general",
        addressing_style: str = "siz",
        mode: str = "draft",
        custom_instructions: Optional[str] = None,
        language_code: str = "uz",
    ) -> Contact:
        """Add or update contact relationship metadata and autonomy mode."""
        if username and username.startswith("@"):
            username = username[1:]

        if mode not in ("auto", "draft", "mute"):
            mode = "draft"

        query = """
        INSERT INTO contacts (user_id, username, full_name, relationship_type, addressing_style, mode, custom_instructions, language_code, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username=COALESCE(excluded.username, contacts.username),
            full_name=excluded.full_name,
            relationship_type=excluded.relationship_type,
            addressing_style=excluded.addressing_style,
            mode=COALESCE(excluded.mode, contacts.mode),
            custom_instructions=COALESCE(excluded.custom_instructions, contacts.custom_instructions),
            language_code=excluded.language_code,
            updated_at=CURRENT_TIMESTAMP;
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (user_id, username, full_name, relationship_type, addressing_style, mode, custom_instructions, language_code),
            )
            await db.commit()

        return await self.get_contact_by_id(user_id)

    async def update_contact_mode(self, user_id: int, mode: str) -> bool:
        """Update autonomy mode ('auto', 'draft', 'mute') for a contact by numeric user_id."""
        if mode not in ("auto", "draft", "mute"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'auto', 'draft', or 'mute'.")
        query = "UPDATE contacts SET mode = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (mode, user_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_contact_mode_by_username(self, username: str, mode: str) -> bool:
        """Update autonomy mode ('auto', 'draft', 'mute') for a contact by @username."""
        if mode not in ("auto", "draft", "mute"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'auto', 'draft', or 'mute'.")
        if username.startswith("@"):
            username = username[1:]
        query = "UPDATE contacts SET mode = ?, updated_at = CURRENT_TIMESTAMP WHERE LOWER(username) = LOWER(?)"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (mode, username))
            await db.commit()
            return cursor.rowcount > 0

    async def get_mode_counts(self) -> Dict[str, int]:
        """Fetch contact counts grouped by autonomy mode ('auto', 'draft', 'mute')."""
        query = "SELECT mode, COUNT(*) as cnt FROM contacts GROUP BY mode"
        counts = {"auto": 0, "draft": 0, "mute": 0}
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    m = r["mode"]
                    if m in counts:
                        counts[m] = r["cnt"]
        return counts

    async def update_addressing_style(self, user_id: int, addressing_style: str) -> bool:
        """Update only the addressing style ('siz' or 'sen') for a contact."""
        query = "UPDATE contacts SET addressing_style = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (addressing_style, user_id))
            await db.commit()
            return True

    async def delete_contact(self, user_id: int) -> bool:
        """Delete contact from CRM database."""
        query = "DELETE FROM contacts WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (user_id,))
            await db.commit()
            return True

    async def get_contact_by_id(self, user_id: int) -> Optional[Contact]:
        """Fetch contact profile by user_id."""
        query = "SELECT * FROM contacts WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Contact(**dict(row))

    async def get_contact_by_username(self, username: str) -> Optional[Contact]:
        """Fetch contact profile by @username."""
        if username.startswith("@"):
            username = username[1:]
        query = "SELECT * FROM contacts WHERE LOWER(username) = LOWER(?) ORDER BY updated_at DESC"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (username,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Contact(**dict(row))

    async def list_contacts(self) -> List[Contact]:
        """List all configured contacts in CRM."""
        query = "SELECT * FROM contacts ORDER BY updated_at DESC"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [Contact(**dict(r)) for r in rows]

    # ------------------ SCHEDULED TASKS ------------------
    async def create_scheduled_task(
        self,
        target_peer: str,
        message_text: str,
        scheduled_time: str,
    ) -> ScheduledTask:
        """Schedule a message task."""
        query = """
        INSERT INTO scheduled_tasks (target_peer, message_text, scheduled_time, status)
        VALUES (?, ?, ?, 'pending')
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (target_peer, message_text, scheduled_time))
            task_id = cursor.lastrowid
            await db.commit()

        return ScheduledTask(
            id=task_id,
            target_peer=target_peer,
            message_text=message_text,
            scheduled_time=scheduled_time,
            status="pending",
        )

    async def get_pending_tasks(self) -> List[ScheduledTask]:
        """Fetch all tasks ready or waiting for execution."""
        query = "SELECT * FROM scheduled_tasks WHERE status = 'pending' ORDER BY scheduled_time ASC"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [ScheduledTask(**dict(r)) for r in rows]

    async def update_task_status(
        self,
        task_id: int,
        status: str,
        error_message: Optional[str] = None,
    ):
        """Update task outcome status."""
        query = "UPDATE scheduled_tasks SET status = ?, error_message = ? WHERE id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (status, error_message, task_id))
            await db.commit()

    async def get_task_by_id(self, task_id: int) -> Optional[ScheduledTask]:
        """Fetch single task by its ID."""
        query = "SELECT * FROM scheduled_tasks WHERE id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (task_id,)) as cursor:
                row = await cursor.fetchone()
                return ScheduledTask(**dict(row)) if row else None

    async def get_task_history(self, limit: int = 10) -> List[ScheduledTask]:
        """Fetch recent completed or cancelled tasks."""
        query = "SELECT * FROM scheduled_tasks WHERE status != 'pending' ORDER BY id DESC LIMIT ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [ScheduledTask(**dict(r)) for r in rows]

    async def delete_task(self, task_id: int) -> bool:
        """Permanently delete a scheduled task."""
        query = "DELETE FROM scheduled_tasks WHERE id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (task_id,))
            await db.commit()
            return True

    # ------------------ CONVERSATION HISTORY ------------------
    async def log_message(
        self,
        chat_id: int,
        sender_name: str,
        role: str,
        content: str,
    ):
        """Save chat turn into history."""
        query = """
        INSERT INTO conversation_history (chat_id, sender_name, role, content)
        VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (chat_id, sender_name, role, content))
            await db.commit()

    async def get_recent_history(self, chat_id: int, limit: int = 10) -> List[ConversationMessage]:
        """Get the latest N messages from a conversation."""
        query = """
        SELECT * FROM conversation_history
        WHERE chat_id = ?
        ORDER BY id DESC LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (chat_id, limit)) as cursor:
                rows = await cursor.fetchall()
                messages = [ConversationMessage(**dict(r)) for r in rows]
                return list(reversed(messages))

    # ------------------ DIGEST / FEEDS ------------------
    async def save_feed_post(self, source_peer_id: int, source_title: str, message_id: int, post_text: str):
        query = """
        INSERT INTO raw_feed_posts (source_peer_id, source_title, message_id, post_text)
        VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (source_peer_id, source_title, message_id, post_text))
            await db.commit()

    async def get_unsummarized_posts(self, limit: int = 50) -> List[dict]:
        query = """
        SELECT * FROM raw_feed_posts
        WHERE is_summarized = 0
        ORDER BY id ASC LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def mark_posts_summarized(self, post_ids: List[int]):
        if not post_ids:
            return
        placeholders = ",".join("?" for _ in post_ids)
        query = f"UPDATE raw_feed_posts SET is_summarized = 1 WHERE id IN ({placeholders})"
        async with get_db(self.db_path) as db:
            await db.execute(query, post_ids)
            await db.commit()

    async def get_recent_posts(self, limit: int = 30) -> List[dict]:
        """Fetch the most recent posts regardless of summarized status."""
        query = """
        SELECT * FROM raw_feed_posts
        ORDER BY id DESC LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                # Return in chronological order
                return list(reversed([dict(r) for r in rows]))

    async def get_feed_statistics(self) -> dict:
        """Returns statistical counts about collected feed posts."""
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT count(*) FROM raw_feed_posts") as cur:
                total_posts = (await cur.fetchone())[0]
            async with db.execute("SELECT count(*) FROM raw_feed_posts WHERE is_summarized = 0") as cur:
                unsummarized = (await cur.fetchone())[0]
            async with db.execute("SELECT count(DISTINCT source_title) FROM raw_feed_posts") as cur:
                unique_sources = (await cur.fetchone())[0]
            return {
                "total_posts": total_posts,
                "unsummarized": unsummarized,
                "unique_sources": unique_sources,
            }

    # ------------------ DRAFTS & AUTONOMY MODES ------------------
    async def create_draft(
        self,
        source_message_id: int,
        incoming_text: str,
        proposed_reply: str,
        expires_at: str,
        contact_username: Optional[str] = None,
        contact_user_id: Optional[int] = None,
        is_sensitive: bool = False,
        sensitive_reason: Optional[str] = None,
    ) -> Optional[Draft]:
        """
        Creates a new pending draft.
        Enforces duplicate draft protection via DB unique index.
        Returns the created Draft object, or None if a duplicate draft already exists.
        """
        if contact_username and contact_username.startswith("@"):
            contact_username = contact_username[1:]

        query = """
        INSERT INTO drafts (
            contact_username, contact_user_id, source_message_id,
            incoming_text, proposed_reply, status, is_sensitive,
            sensitive_reason, expires_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            try:
                cursor = await db.execute(
                    query,
                    (
                        contact_username,
                        contact_user_id,
                        source_message_id,
                        incoming_text,
                        proposed_reply,
                        1 if is_sensitive else 0,
                        sensitive_reason,
                        expires_at,
                    ),
                )
                draft_id = cursor.lastrowid
                await db.commit()
                return await self.get_draft_by_id(draft_id)
            except aiosqlite.IntegrityError:
                # Prevent duplicate draft for the same contact message
                return None

    async def get_draft_by_id(self, draft_id: int) -> Optional[Draft]:
        """
        Fetch draft record by primary key ID.
        Returns Draft object if found, otherwise None.
        """
        query = "SELECT * FROM drafts WHERE id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (draft_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                data = dict(row)
                data["is_sensitive"] = bool(data.get("is_sensitive", 0))
                return Draft(**data)

    async def list_pending_drafts(self, limit: int = 20) -> List[Draft]:
        """
        Fetch pending, unexpired drafts ordered chronologically by created_at DESC.
        """
        now_iso = datetime.now().isoformat()
        query = """
        SELECT * FROM drafts 
        WHERE status = 'pending' AND expires_at > ? 
        ORDER BY created_at DESC 
        LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (now_iso, limit)) as cursor:
                rows = await cursor.fetchall()
                drafts = []
                for r in rows:
                    data = dict(r)
                    data["is_sensitive"] = bool(data.get("is_sensitive", 0))
                    drafts.append(Draft(**data))
                return drafts

    async def count_pending_drafts(self) -> int:
        """
        Counts active pending, unexpired drafts.
        """
        now_iso = datetime.now().isoformat()
        query = "SELECT COUNT(*) as cnt FROM drafts WHERE status = 'pending' AND expires_at > ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (now_iso,)) as cursor:
                row = await cursor.fetchone()
                return row["cnt"] if row else 0

    async def resolve_draft_atomic(
        self,
        draft_id: int,
        new_status: str,
        edited_reply: Optional[str] = None,
    ) -> bool:
        """
        Atomically transitions a pending draft to new_status ('sent', 'rejected', 'edited_sent', 'superseded', 'expired').
        Guarantees concurrency and double-press safety.
        Returns True if exactly one row transitioned, False otherwise.
        """
        if edited_reply is not None:
            query = """
            UPDATE drafts 
            SET status = ?, proposed_reply = ?, resolved_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND status = 'pending'
            """
            params = (new_status, edited_reply, draft_id)
        else:
            query = """
            UPDATE drafts 
            SET status = ?, resolved_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND status = 'pending'
            """
            params = (new_status, draft_id)

        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount == 1

    async def expire_old_drafts(self) -> int:
        """
        Marks pending drafts older than their expires_at timestamp as 'expired'.
        Returns count of expired drafts.
        """
        now_iso = datetime.now().isoformat()
        query = """
        UPDATE drafts 
        SET status = 'expired', resolved_at = CURRENT_TIMESTAMP 
        WHERE status = 'pending' AND expires_at <= ?
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (now_iso,))
            await db.commit()
            return cursor.rowcount

    async def get_schema_version(self) -> int:
        """
        Returns the current maximum schema version from schema_versions table.
        """
        query = "SELECT MAX(version) as max_v FROM schema_versions"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                row = await cursor.fetchone()
                return row["max_v"] if row and row["max_v"] is not None else 0
